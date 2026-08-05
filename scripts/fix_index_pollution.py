#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性修复指数脏数据 + 给 symbols 加 data_status。

解决的问题：
- 7 个指数（除 IDX_000001 已修）7-06 之前是 sina 缩放值（个位数），
  7-06 之后是 akshare 真实点位。混交导致 ROC 飙到 +100%、温度错为"沸"。
- 8 个 SW L2 行业 akshare 拉取为空，需标记 no_data。
- symbols 表新增 data_status 字段（'ok' | 'no_data'）。

执行流程：
1. 用 sina 拉 7 个指数近 2 年日线覆盖（同一数据源）
2. 用 sina 拉 8 个缺失 L2 行业（一般 sina 也无 SW L2 指数，试 baostock 备用）
3. 删除全部 161 个 SW+index 的脏指标（trade_date >= '2026-07-06'）
4. 全量重算指标
5. 给 symbols 加 data_status 字段，标记缺失品种
6. 重跑 export
"""

import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from src import config, db  # noqa: E402
from src.daily_sync import DailyPriceSync  # noqa: E402
from src.data_source import AkShareFetcher  # noqa: E402
from src.indicators.engine import run_indicator_update  # noqa: E402
from src.models import DailyIndicator, Symbol  # noqa: E402

logger = logging.getLogger(__name__)

# 7 个宽基指数的 sina 原始代码
INDEX_CODES = {
    "IDX_000001": "000001",
    "IDX_399001": "399001",
    "IDX_399006": "399006",
    "IDX_000688": "000688",
    "IDX_000016": "000016",
    "IDX_000300": "000300",
    "IDX_000905": "000905",
}

# 8 个 akshare 拉取为空的 L2 行业
MISSING_L2 = {
    "SW_801011": "801011",
    "SW_801019": "801019",
    "SW_801117": "801117",
    "SW_801156": "801156",
    "SW_801207": "801207",
    "SW_801216": "801216",
    "SW_801961": "801961",
    "SW_801983": "801983",
}


def step1_refetch_indices(fetcher: AkShareFetcher, days: int = 730) -> int:
    """用 sina 拉 7 个指数近 N 天日线覆盖。"""
    end = date.today()
    start = end - timedelta(days=days)
    sync = DailyPriceSync(fetcher)
    total = 0
    for sid, code in INDEX_CODES.items():
        try:
            df = fetcher.get_index_daily_sina(code, start, end)
            if not df.empty:
                df["symbol_id"] = sid
                n = sync._upsert_daily(df)
                total += n
                logger.info("指数 %s 重拉: %s 行", sid, n)
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("指数 %s 重拉失败: %s", sid, exc)
    return total


def step2_try_missing_l2_via_sina(fetcher: AkShareFetcher) -> int:
    """sina 也支持申万行业指数，试一次。"""
    sync = DailyPriceSync(fetcher)
    end = date.today()
    start = end - timedelta(days=400)
    total = 0
    for sid, code in MISSING_L2.items():
        if sync._upsert_daily.__doc__:  # 不触发，仅占位
            pass
        try:
            # sina 用 sz<6位> / sh<6位>，申万 801xxx 多数以 8 开头，按 sh 试
            symbol = f"sh{code}"
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df is None or df.empty:
                continue
            # 标准化列名
            rename = {"date": "trade_date", "日期": "trade_date",
                      "open": "open", "开盘": "open", "high": "high", "最高": "high",
                      "low": "low", "最低": "low", "close": "close", "收盘": "close",
                      "volume": "volume", "成交量": "volume"}
            df = df.rename(columns=rename)
            if "trade_date" not in df.columns:
                logger.warning("L2 %s sina 返回缺 trade_date", sid)
                continue
            import pandas as pd
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)]
            if df.empty:
                continue
            df["symbol_id"] = sid
            df["amount"] = df.get("volume", 0) * df.get("close", 0)
            df["pct_chg"] = df["close"].pct_change() * 100
            n = sync._upsert_daily(df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]])
            total += n
            logger.info("L2 %s sina 拉取: %s 行", sid, n)
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("L2 %s sina 拉取失败: %s", sid, exc)
    return total


def step3_clean_polluted_indicators() -> int:
    """删除 7-06 及之后所有 SW+index 的脏指标，等待重算。"""
    from sqlalchemy import delete
    with db.get_session() as session:
        r = session.execute(
            delete(DailyIndicator).where(
                DailyIndicator.trade_date >= date(2026, 7, 6),
                DailyIndicator.symbol_id.like("IDX_%") | DailyIndicator.symbol_id.like("SW_%"),
            )
        )
        n = r.rowcount
        logger.info("删除 7-06 及之后 SW+index 脏指标: %s 行", n)
    return n


def step4_recalculate() -> dict:
    """全量重算 161 个 SW+index 指标。"""
    from sqlalchemy import distinct, select
    from src.models import DailyPrice
    with db.get_session() as session:
        rows = session.execute(
            select(distinct(DailyPrice.symbol_id))
            .join(Symbol, Symbol.symbol_id == DailyPrice.symbol_id)
            .where(Symbol.node_type.in_(["index", "industry_l1", "industry_l2"]))
        ).all()
        sids = [r[0] for r in rows]
    logger.info("待重算 symbol: %s 个", len(sids))
    return run_indicator_update(symbol_ids=sids, end_date=date.today())


def step5_add_data_status_column():
    """给 symbols 加 data_status 字段，标记 8 个缺失 L2 为 no_data。"""
    with db._engine.begin() as conn:
        # 1) 加列（用 text 安全）
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(symbols)")).all()]
        if "data_status" not in cols:
            conn.execute(text("ALTER TABLE symbols ADD COLUMN data_status VARCHAR(16) DEFAULT 'ok'"))
            logger.info("symbols 新增 data_status 列")
        # 2) 标记 8 个缺失
        for sid in MISSING_L2.keys():
            r = conn.execute(
                text("UPDATE symbols SET data_status='no_data' WHERE symbol_id=:sid"),
                {"sid": sid},
            )
            if r.rowcount:
                logger.info("标记 %s 为 no_data", sid)
        # 3) 标记没有任何日线的全部
        no_price_rows = conn.execute(text("""
            SELECT s.symbol_id FROM symbols s
            LEFT JOIN daily_price dp ON dp.symbol_id = s.symbol_id
            WHERE s.node_type IN ('index','industry_l1','industry_l2')
            GROUP BY s.symbol_id
            HAVING COUNT(dp.trade_date) = 0
        """)).all()
        for (sid,) in no_price_rows:
            conn.execute(text("UPDATE symbols SET data_status='no_data' WHERE symbol_id=:sid"), {"sid": sid})


def main() -> int:
    config.ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fetcher = AkShareFetcher(sleep_min=0.5, sleep_max=1.0)

    logger.info("===== [1/5] 重拉 7 个指数 =====")
    n1 = step1_refetch_indices(fetcher)
    logger.info("===== [2/5] 试 sina 拉 8 个缺失 L2 =====")
    n2 = step2_try_missing_l2_via_sina(fetcher)
    logger.info("===== [3/5] 删脏指标 =====")
    n3 = step3_clean_polluted_indicators()
    logger.info("===== [4/5] 重算 161 指标 =====")
    res = step4_recalculate()
    logger.info("重算结果: %s", res)
    logger.info("===== [5/5] 加 data_status 字段 =====")
    step5_add_data_status_column()
    logger.info("===== 完成 n1=%s n2=%s n3=%s =====", n1, n2, n3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
