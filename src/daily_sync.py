# -*- coding: utf-8 -*-
"""日线行情同步：读取 DSA + 从 akshare 补全。"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import List, Optional, Tuple

import exchange_calendars as xcals
import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.config import (
    CONCEPT_SYNC_WORKERS,
    INDEX_SYNC_WORKERS,
    INDUSTRY_DAILY_SYNC_WORKERS,
    MIN_HISTORY_YEARS,
    STOCK_SYNC_MAX_WORKERS,
)
from src.data_source import AkShareFetcher, DSAReader
from src.db import get_session
from src.models import DailyPrice, Symbol
from src.classification import NODE_TYPE_CONCEPT, NODE_TYPE_INDEX, NODE_TYPE_INDUSTRY_L1, NODE_TYPE_INDUSTRY_L2, NODE_TYPE_STOCK

logger = logging.getLogger(__name__)

# 各数据源线程内 jitter 限速（秒）：概念=同花顺、行业=申万、指数/个股=新浪
PACE_CONCEPT = (1.0, 2.0)
PACE_INDUSTRY = (0.8, 1.6)
PACE_INDEX = (0.3, 0.8)
PACE_STOCK = (0.2, 0.6)


def get_latest_trade_date(target: date = None) -> date:
    """获取最近一个「已收盘」的 A 股交易日。

    日历优先（XSHG）：目标是"应该同步到哪天"，由交易日历决定；
    数据库最大日期仅作日历不可用时的兜底，避免库内旧数据锚定死同步进度。
    注意：当日是交易日但未到 15:30 收盘时，当日数据尚未发布，目标回退到前一交易日，
    否则全市场会对一个空交易日做无效拉取。
    """
    from datetime import datetime as _dt

    target = target or date.today()
    try:
        cal = xcals.get_calendar("XSHG")
        if cal.is_session(target):
            now = _dt.now()
            if target < now.date() or (now.hour * 60 + now.minute >= 15 * 60 + 30):
                return target
            prev = cal.date_to_session(target - timedelta(days=1), "previous")
            if prev is not None:
                return pd.Timestamp(prev).date()
            return target
        prev = cal.date_to_session(target, "previous")
        if prev is not None:
            return pd.Timestamp(prev).date()
    except Exception:
        pass
    # 兜底：数据库实际最大日期
    try:
        with get_session() as session:
            row = session.execute(text("SELECT MAX(trade_date) FROM daily_price")).fetchone()
        if row and row[0]:
            latest = pd.to_datetime(row[0]).date()
            if latest <= target:
                return latest
    except Exception:
        pass
    return target


def strip_symbol_prefix(symbol_id: str) -> Tuple[str, str]:
    """根据 symbol_id 前缀返回裸代码与 fetcher 类型。"""
    if symbol_id.startswith("IDX_"):
        return symbol_id.replace("IDX_", "", 1), "index"
    if symbol_id.startswith("SW_"):
        return symbol_id.replace("SW_", "", 1), "industry"
    if symbol_id.startswith("CONCEPT_"):
        return symbol_id.replace("CONCEPT_", "", 1), "concept"
    return symbol_id, "stock"


class DailyPriceSync:
    def __init__(self, fetcher: AkShareFetcher, dsa_reader: Optional[DSAReader] = None):
        self.fetcher = fetcher
        self.dsa_reader = dsa_reader or DSAReader()
        # 本次同步每个品种写入的最早交易日（{symbol_id: date}），
        # 用于识别"历史价格被补缺/修订"的品种，指标引擎据此扩大温度/右侧重写窗口
        self._price_revisions = {}

    def _get_existing_max_dates(self, symbol_ids: List[str]) -> dict:
        """查询本地 daily_price 中每个 symbol 的最大日期。"""
        with get_session() as session:
            rows = session.execute(
                select(DailyPrice.symbol_id, func.max(DailyPrice.trade_date))
                .where(DailyPrice.symbol_id.in_(symbol_ids))
                .group_by(DailyPrice.symbol_id)
            ).all()
        return {sid: d for sid, d in rows}

    def _date_range_for_symbol(
        self, symbol_id: str, end_date: date, existing_max: dict
    ) -> Tuple[Optional[date], date]:
        """返回该 symbol 需要拉取的起止日期。"""
        start_default = date(end_date.year - MIN_HISTORY_YEARS, end_date.month, end_date.day)
        max_date = existing_max.get(symbol_id)
        if max_date and max_date >= end_date:
            return None, end_date
        if max_date:
            start = max_date + timedelta(days=1)
        else:
            start = start_default
        return start, end_date

    def _upsert_daily(self, df: pd.DataFrame) -> int:
        """将 DataFrame 写入 daily_price 表。"""
        if df.empty:
            return 0
        df = df.copy()
        for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
            if col not in df.columns:
                df[col] = None
        df = df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]
        df = df.dropna(subset=["symbol_id", "trade_date", "close"])
        records = df.to_dict(orient="records")
        if not records:
            return 0

        # 记录每个品种本次写入的最早日期（补缺/修订历史价格的痕迹）
        for sid, min_d in df.groupby("symbol_id")["trade_date"].min().items():
            prev = self._price_revisions.get(sid)
            if prev is None or min_d < prev:
                self._price_revisions[sid] = min_d

        with get_session() as session:
            for record in records:
                stmt = sqlite_insert(DailyPrice).values(record)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol_id", "trade_date"],
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "volume": stmt.excluded.volume,
                        "amount": stmt.excluded.amount,
                        "pct_chg": stmt.excluded.pct_chg,
                    },
                )
                session.execute(stmt)
        return len(records)

    def sync_indices(self, symbol_entries: List[Tuple[str, str]], end_date: date) -> int:
        """同步宽基指数日线（新浪源，并发拉取 + 主线程写库）。"""
        tasks = []
        existing_max = self._get_existing_max_dates([sid for sid, _ in symbol_entries])
        for symbol_id, node_type in symbol_entries:
            if node_type != NODE_TYPE_INDEX or not symbol_id.startswith("IDX_"):
                continue
            raw_code, _ = strip_symbol_prefix(symbol_id)
            s, e = self._date_range_for_symbol(symbol_id, end_date, existing_max)
            if s is None:
                continue
            tasks.append((symbol_id, raw_code, s, e))

        def _fetch(task):
            symbol_id, raw_code, s, e = task
            try:
                df = self.fetcher.get_index_daily_sina(raw_code, s, e, pace=PACE_INDEX)
                if not df.empty:
                    df["symbol_id"] = symbol_id
                return symbol_id, df, None
            except Exception as exc:
                return symbol_id, None, exc

        total = 0
        if tasks:
            with ThreadPoolExecutor(max_workers=max(1, INDEX_SYNC_WORKERS)) as executor:
                for symbol_id, df, exc in executor.map(_fetch, tasks):
                    if exc is not None:
                        logger.warning("同步指数 %s 失败: %s", symbol_id, exc)
                        continue
                    total += self._upsert_daily(df)
        logger.info("同步指数完成: %s 条", total)
        return total

    def sync_concepts(
        self, symbol_entries: List[Tuple[str, str]], end_date: date
    ) -> int:
        """同步同花顺概念指数日线。

        symbol_id 形如 CONCEPT_<code>，对应数据库里 concept 类型节点的 name。
        ths 接口 symbol 形参接收的是中文名，所以先建立 code→name 映射。
        """
        from src.models import Symbol

        concept_entries = [
            (sid, t) for sid, t in symbol_entries
            if t == NODE_TYPE_CONCEPT and sid != "CONCEPT_ROOT"
        ]
        if not concept_entries:
            return 0

        with get_session() as session:
            rows = session.execute(
                select(Symbol.symbol_id, Symbol.name).where(
                    Symbol.node_type == NODE_TYPE_CONCEPT
                )
            ).all()
        code_to_name = {sid: name for sid, name in rows if sid != "CONCEPT_ROOT"}
        if not code_to_name:
            return 0

        existing_max = self._get_existing_max_dates([sid for sid, _ in concept_entries])
        tasks = []
        skipped = 0
        for symbol_id, _ in concept_entries:
            s, e = self._date_range_for_symbol(symbol_id, end_date, existing_max)
            if s is None:
                skipped += 1
                continue
            name = code_to_name.get(symbol_id)
            if not name:
                skipped += 1
                continue
            tasks.append((symbol_id, name, s, e))

        def _fetch(task):
            symbol_id, name, s, e = task
            try:
                df = self.fetcher.get_concept_index_daily(name, s, e, pace=PACE_CONCEPT)
                if not df.empty:
                    df["symbol_id"] = symbol_id
                return symbol_id, name, df, None
            except Exception as exc:
                return symbol_id, name, None, exc

        total = 0
        done = 0
        if tasks:
            with ThreadPoolExecutor(max_workers=max(1, CONCEPT_SYNC_WORKERS)) as executor:
                for symbol_id, name, df, exc in executor.map(_fetch, tasks):
                    done += 1
                    if exc is not None:
                        logger.warning("同步概念 %s(%s) 失败: %s", symbol_id, name, exc)
                    elif df is not None and not df.empty:
                        total += self._upsert_daily(df)
                    if done % 50 == 0:
                        logger.info("概念日线同步进度: %s/%s (累计 %s 条)", done, len(tasks), total)
        logger.info("同步概念完成: %s 条, 跳过 %s", total, skipped)
        return total

    def sync_industries(self, symbol_entries: List[Tuple[str, str]], end_date: date) -> int:
        """同步申万行业指数日线（并发拉取 + 主线程写库）。"""
        tasks = []
        existing_max = self._get_existing_max_dates([sid for sid, _ in symbol_entries])
        for symbol_id, node_type in symbol_entries:
            if node_type not in (NODE_TYPE_INDUSTRY_L1, NODE_TYPE_INDUSTRY_L2) or not symbol_id.startswith("SW_"):
                continue
            raw_code, _ = strip_symbol_prefix(symbol_id)
            s, e = self._date_range_for_symbol(symbol_id, end_date, existing_max)
            if s is None:
                continue
            tasks.append((symbol_id, raw_code, s, e))

        def _fetch(task):
            symbol_id, raw_code, s, e = task
            try:
                df = self.fetcher.get_industry_index_daily(raw_code, s, e, pace=PACE_INDUSTRY)
                if not df.empty:
                    df["symbol_id"] = symbol_id
                return symbol_id, df, None
            except Exception as exc:
                return symbol_id, None, exc

        total = 0
        if tasks:
            with ThreadPoolExecutor(max_workers=max(1, INDUSTRY_DAILY_SYNC_WORKERS)) as executor:
                for symbol_id, df, exc in executor.map(_fetch, tasks):
                    if exc is not None:
                        logger.warning("同步行业 %s 失败: %s", symbol_id, exc)
                        continue
                    total += self._upsert_daily(df)
        logger.info("同步行业指数完成: %s 条", total)
        return total

    def sync_stocks(
        self,
        symbol_entries: List[Tuple[str, str]],
        end_date: date,
        max_stocks: Optional[int] = None,
        use_dsa: bool = True,
        incremental_days: Optional[int] = None,
    ) -> int:
        """同步个股日线：优先读 DSA，DSA 未覆盖到 end_date 的缺口用新浪并发补、baostock 兜底。"""
        stock_entries = [(s, t) for s, t in symbol_entries if t == NODE_TYPE_STOCK]
        if max_stocks:
            stock_entries = stock_entries[:max_stocks]
            logger.info("限制同步前 %s 只个股", max_stocks)

        stock_ids = [s for s, _ in stock_entries]
        existing_max = self._get_existing_max_dates(stock_ids)
        need_fetch = []
        need_dsa = []
        for sid, _ in stock_entries:
            s, _ = self._date_range_for_symbol(sid, end_date, existing_max)
            if s is None:
                continue
            need_fetch.append(sid)
            if use_dsa:
                need_dsa.append(sid)

        total = 0
        dsa_max: dict = {}

        # 批量读 DSA；只有 DSA 数据真正覆盖到 end_date 的个股才算补齐
        if use_dsa and need_dsa:
            start_default = date(end_date.year - MIN_HISTORY_YEARS, end_date.month, end_date.day)
            try:
                df_dsa = self.dsa_reader.read_stock_daily(
                    codes=need_dsa,
                    start_date=start_default,
                    end_date=end_date,
                )
                if not df_dsa.empty:
                    total += self._upsert_daily(df_dsa)
                    dsa_max = (
                        df_dsa.groupby("symbol_id")["trade_date"].max().to_dict()
                    )
                    covered = {s for s, dm in dsa_max.items() if dm >= end_date}
                    need_fetch = [s for s in need_fetch if s not in covered]
                    logger.info(
                        "DSA 覆盖 %s 只个股（其中 %s 只已到 %s，%s 只存在缺口待补）",
                        len(dsa_max), len(covered), end_date, len(need_fetch),
                    )
            except Exception as exc:
                logger.warning("读取 DSA 个股日线失败: %s", exc)

        # 构造补拉任务：起点取 本地最大日期/DSA 最大日期 的较大者 +1，堵住中间缺口
        fetch_tasks = []
        incremental_start = end_date - timedelta(days=incremental_days) if incremental_days else None
        for symbol_id in need_fetch:
            raw_code, _ = strip_symbol_prefix(symbol_id)
            local_max = existing_max.get(symbol_id)
            dm = dsa_max.get(symbol_id)
            effective_max = max(d for d in (local_max, dm) if d) if (local_max or dm) else None
            if effective_max and effective_max >= end_date:
                continue
            if effective_max:
                s = effective_max + timedelta(days=1)
            else:
                s = date(end_date.year - MIN_HISTORY_YEARS, end_date.month, end_date.day)
                # incremental 模式：仅对完全没有历史的新股钳制窗口，避免全量回追
                if incremental_start and s < incremental_start:
                    s = incremental_start
            fetch_tasks.append((symbol_id, raw_code, s, end_date))

        # 补缺策略：
        # 1) tinyshare 全市场按日批量（快路径，几次调用覆盖全部缺口，需 TINYSHARE_TOKEN）
        # 2) 仍未覆盖的个股 baostock 串行兜底（东财/新浪 HTTP 源受本机代理限制，不作主力）
        if fetch_tasks:
            try:
                needed = {sid: s for sid, _, s, _ in fetch_tasks}
                min_start = min(needed.values())
                cal = xcals.get_calendar("XSHG")
                sessions = [
                    pd.Timestamp(x).date()
                    for x in cal.sessions_in_range(
                        pd.Timestamp(min_start), pd.Timestamp(end_date)
                    )
                ]
                logger.info(
                    "通过 tinyshare 全市场批量补缺 %s 只个股 × %s 个交易日",
                    len(fetch_tasks), len(sessions),
                )
                df_all = self.fetcher.get_market_daily_tinyshare(sessions)
                if not df_all.empty:
                    df_all = df_all[df_all["symbol_id"].isin(needed)].copy()
                    df_all["_start"] = df_all["symbol_id"].map(needed)
                    df_all = df_all[df_all["trade_date"] >= df_all["_start"]].drop(columns=["_start"])
                    total += self._upsert_daily(df_all)
                    # 覆盖判定锚定 tinyshare 实际返回的最大日期，而不是 end_date
                    # （end_date 当日数据可能尚未发布，拿它比会把全部个股误判为未覆盖）
                    expected_max = df_all["trade_date"].max()
                    ts_max = df_all.groupby("symbol_id")["trade_date"].max().to_dict()
                    before = len(fetch_tasks)
                    fetch_tasks = [
                        t for t in fetch_tasks
                        if ts_max.get(t[0], date.min) < expected_max
                    ]
                    logger.info(
                        "tinyshare 覆盖 %s 只，剩余 %s 只待兜底",
                        before - len(fetch_tasks), len(fetch_tasks),
                    )
            except Exception as exc:
                logger.warning("tinyshare 批量补缺失败（转 baostock 兜底）: %s", exc)

        if fetch_tasks:
            logger.info("baostock 兜底补拉 %s 只个股", len(fetch_tasks))
            for i, (symbol_id, raw_code, s, e) in enumerate(fetch_tasks, start=1):
                try:
                    df = self.fetcher.get_stock_daily_baostock(raw_code, s, e)
                    if not df.empty:
                        df["symbol_id"] = symbol_id
                        total += self._upsert_daily(df)
                except Exception as exc:
                    logger.warning("同步个股 %s 失败: %s", symbol_id, exc)
                if i % 500 == 0:
                    logger.info("baostock 兜底进度: %s/%s", i, len(fetch_tasks))
                time.sleep(0.02)

        logger.info("同步个股完成: %s 条", total)
        return total

    def run(
        self,
        symbol_entries: Optional[List[Tuple[str, str]]] = None,
        end_date: Optional[date] = None,
        max_stocks: Optional[int] = None,
        incremental_days: Optional[int] = None,
    ) -> dict:
        """运行全部日线同步。"""
        end_date = end_date or get_latest_trade_date()
        logger.info("日线同步目标截止日期: %s (增量天数=%s)", end_date, incremental_days)

        if symbol_entries is None:
            with get_session() as session:
                rows = session.execute(select(Symbol.symbol_id, Symbol.node_type)).all()
            symbol_entries = list(rows)

        # 指数/行业/概念分属不同数据源站点，三组并行；个股依赖 DSA 读取，单独执行
        counts = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            fut_indices = executor.submit(self.sync_indices, symbol_entries, end_date)
            fut_industries = executor.submit(self.sync_industries, symbol_entries, end_date)
            fut_concepts = executor.submit(self.sync_concepts, symbol_entries, end_date)
            counts["indices"] = fut_indices.result()
            counts["industries"] = fut_industries.result()
            counts["concepts"] = fut_concepts.result()
        counts["stocks"] = self.sync_stocks(
            symbol_entries, end_date, max_stocks=max_stocks,
            incremental_days=incremental_days,
        )
        self._flush_price_revisions()
        return counts

    def _flush_price_revisions(self) -> None:
        """把本次同步各品种写入的最早日期合并落盘（data/price_revisions.json）。

        指标引擎据此把温度/右侧的重写窗口前推到被改写日期之前，
        防止"补缺改价后新指标与旧指标在边界断裂"（2026-07-29 跳变事故的根因）。
        """
        if not self._price_revisions:
            return
        from src import config

        path = config.LOCAL_DB_PATH.parent / "price_revisions.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        for sid, d in self._price_revisions.items():
            ds = d.isoformat()
            if data.get(sid) is None or ds < data[sid]:
                data[sid] = ds
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("价格写入断点已记录: %s 个品种 -> %s", len(self._price_revisions), path)
