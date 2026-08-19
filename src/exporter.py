# -*- coding: utf-8 -*-
"""静态 JSON 导出器。

为前端只读视图导出三份数据：
- dashboard.json ：最近一日温度分布（柱图用）
- symbols.json  ：最近一日全市场品种截面 + 温度 + RS + 右侧状态（列表页用）
- prices-N.json ：近 1 年日线（详情页用），分片输出避免单文件过大

设计原则：
- 不写死日期，管道跑完自动取 daily_indicator.max(trade_date)
- 价格序列只输出日线收盘 / pct_chg（前端对数收益图只需要这两列）
- 分片大小：每片约 2000 个 symbol，控制在 1~2 MB
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from numbers import Real
from pathlib import Path
from typing import Iterable, List, Optional

import numpy as np
import pandas as pd

from src import db
from src.config import WEB_DATA_DIR

logger = logging.getLogger(__name__)

TEMPERATURE_LEVELS = ["沸", "热", "温", "平", "凉", "寒", "冻"]


@dataclass(frozen=True)
class ExportResult:
    """导出结果摘要。"""

    trade_date: Optional[date]
    symbol_count: int
    price_chunks: int
    bytes_total: int


def _to_jsonable(value):
    """将 numpy / pandas 标量转为 Python 原生类型，便于 json.dump。"""
    if value is None:
        return None
    # 用 numbers.Real 兼容 float / numpy.float64 等所有"实数"类型
    if isinstance(value, Real) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):  # numpy scalar
        try:
            v = value.item()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
        except Exception:  # noqa: BLE001
            return str(value)
    return value


def _records_to_json(rows: Iterable[dict]) -> list:
    return [{k: _to_jsonable(v) for k, v in r.items()} for r in rows]


def _write_json(path: Path, payload) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False 强制 json 拒绝 NaN/Infinity（不合法 JSON），暴露上游数据问题
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _get_latest_trade_date() -> Optional[date]:
    df = pd.read_sql("SELECT MAX(trade_date) AS d FROM daily_indicator", db._engine)
    if df.empty or df["d"].isna().all():
        return None
    return pd.to_datetime(df["d"].iloc[0]).date()


_HISTORY_CACHE: dict[str, list] = {}


def _load_indicator_history(symbol_id: str, end_date: date, lookback_days: int = 90) -> list:
    """加载单个 symbol 近 lookback_days 天的 daily_indicator 历史。

    Returns:
        [{"date": "2026-05-01", "temperature": "热", "temperature_score": 28.5,
          "rs_score": 78, "is_right_side": True, "right_side_days": 4}, ...]
    """
    cache_key = f"{symbol_id}|{end_date.isoformat()}|{lookback_days}"
    if cache_key in _HISTORY_CACHE:
        return _HISTORY_CACHE[cache_key]
    start_date = end_date - timedelta(days=lookback_days)
    df = pd.read_sql(
        """
        SELECT trade_date, temperature, temperature_score, rs_score,
               is_right_side, right_side_days
        FROM daily_indicator
        WHERE symbol_id = :sid
          AND trade_date BETWEEN :start AND :end
        ORDER BY trade_date
        """,
        db._engine,
        params={"sid": symbol_id, "start": start_date.isoformat(), "end": end_date.isoformat()},
    )
    if df.empty:
        _HISTORY_CACHE[cache_key] = []
        return []
    rows = []
    for r in df.to_dict(orient="records"):
        rec = {
            "date": pd.to_datetime(r["trade_date"]).strftime("%Y-%m-%d"),
            "temperature": r["temperature"],
            "temperature_score": _to_jsonable(r["temperature_score"]),
            "rs_score": int(r["rs_score"]) if pd.notna(r["rs_score"]) else None,
            "is_right_side": bool(r["is_right_side"]) if pd.notna(r["is_right_side"]) else None,
            "right_side_days": int(r["right_side_days"]) if pd.notna(r["right_side_days"]) else None,
        }
        rows.append(rec)
    _HISTORY_CACHE[cache_key] = rows
    return rows


def _reset_history_cache() -> None:
    """导出完成后清空缓存，释放内存。"""
    _HISTORY_CACHE.clear()


def export_dashboard(out_dir: Path, trade_date: date) -> int:
    """导出最近一日的七档温度分布。"""
    df = pd.read_sql(
        "SELECT temperature, COUNT(*) AS cnt FROM daily_indicator WHERE trade_date = :d GROUP BY temperature",
        db._engine,
        params={"d": trade_date.isoformat()},
    )
    count_map = {row["temperature"]: int(row["cnt"]) for _, row in df.iterrows()}
    distribution = [{"temperature": t, "count": count_map.get(t, 0)} for t in TEMPERATURE_LEVELS]

    payload = {
        "trade_date": trade_date.isoformat(),
        "total": sum(item["count"] for item in distribution),
        "distribution": distribution,
    }
    path = out_dir / "dashboard.json"
    return _write_json(path, payload)


def _prev_trade_date_sql(date_iso: str) -> str:
    """返回 daily_indicator 中严格早于 date_iso 的最大交易日的 SQL 表达式片段。"""
    return (
        "(SELECT temperature FROM daily_indicator di2 "
        "WHERE di2.symbol_id = s.symbol_id "
        f"AND di2.trade_date < '{date_iso}' "
        "ORDER BY di2.trade_date DESC LIMIT 1)"
    )


def export_hotlist(out_dir: Path, trade_date: date) -> int:
    """导出热点看板：4 组温档跃迁信号（按 L2 / 个股 各两组）+ 已确认强势 2 组。

    用户交易策略：
    - 买入：温→热（任意品种首次突破热档）
    - 卖出：热→温 且仍处于右侧（说明经历过温→热的进入，现在回到温档是止盈位）
    - 观察：热→沸（趋势更强但警惕过热）
    - 警惕/减仓：沸→热（顶部回落一档）

    输出 8 个分组（每组均按 L2 / stock 各一份）：
    - warm_to_hot：温→热（**买入信号**）
    - hot_to_warm：热→温，且右侧中（**卖出信号**）
    - hot_to_boil：热→沸（**观察**）
    - boil_to_hot：沸→热（**警惕/减仓**）
    - hot_or_boil：今日温度 ∈ {热, 沸}（已确认强势池）

    每条记录含：symbol_id, name, parent_name, temperature, prev_temperature,
    rs_score, rs_score_trend, is_right_side, right_side_days, close, pct_chg,
    effective_trade_date, prev_trade_date。

    行业指数日线发布滞后（L2 行业常晚个股 1 天），所以按品种分别取
    「≤ 截止日的最大交易日」作为该品种的 cur，再找严格小于 cur 的最大交易日
    作为 prev。
    """
    cur_iso = trade_date.isoformat()

    sql = """
        WITH max_dates AS (
            SELECT
                di.symbol_id,
                MAX(di.trade_date) AS cur,
                (SELECT MAX(trade_date) FROM daily_indicator di2
                  WHERE di2.symbol_id = di.symbol_id
                    AND di2.trade_date < MAX(di.trade_date)) AS prev
            FROM daily_indicator di
            WHERE di.trade_date <= :cur
            GROUP BY di.symbol_id
        )
        SELECT
            s.symbol_id,
            s.name,
            s.node_type,
            s.parent_id,
            p.name AS parent_name,
            md.cur AS effective_trade_date,
            md.prev AS prev_trade_date,
            di.temperature,
            di.temperature_score,
            di_prev.temperature AS prev_temperature,
            di.rs_score,
            di.rs_score_trend,
            di.is_right_side,
            di.right_side_days,
            dp.close,
            dp.pct_chg
        FROM max_dates md
        JOIN symbols s ON s.symbol_id = md.symbol_id
        LEFT JOIN symbols p ON p.symbol_id = s.parent_id
        JOIN daily_indicator di
            ON di.symbol_id = md.symbol_id AND di.trade_date = md.cur
        LEFT JOIN daily_indicator di_prev
            ON di_prev.symbol_id = md.symbol_id AND di_prev.trade_date = md.prev
        LEFT JOIN daily_price dp
            ON dp.symbol_id = md.symbol_id AND dp.trade_date = md.cur
        WHERE s.node_type IN ('industry_l2', 'stock')
          AND di.temperature IN ('温', '热', '沸')
          AND di_prev.temperature IS NOT NULL
          AND di_prev.temperature IN ('温', '热', '沸')
          AND (
            -- 温→热（买入）
            (di_prev.temperature = '温' AND di.temperature = '热')
            -- 热→温（卖出候选，需配合右侧过滤）
            OR (di_prev.temperature = '热' AND di.temperature = '温')
            -- 热→沸（观察）
            OR (di_prev.temperature = '热' AND di.temperature = '沸')
            -- 沸→热（警惕/减仓）
            OR (di_prev.temperature = '沸' AND di.temperature = '热')
          )
    """
    df = pd.read_sql(sql, db._engine, params={"cur": cur_iso})
    df = df.replace({np.nan: None})
    rows = _records_to_json(df.to_dict(orient="records"))
    for r in rows:
        if "is_right_side" in r and r["is_right_side"] is not None:
            r["is_right_side"] = bool(r["is_right_side"])
        if "effective_trade_date" in r and r["effective_trade_date"] is not None:
            r["effective_trade_date"] = str(r["effective_trade_date"])
        if "prev_trade_date" in r and r["prev_trade_date"] is not None:
            r["prev_trade_date"] = str(r["prev_trade_date"])

    def _pick(node_type: str, kind: str) -> list:
        """按 node_type + 信号类型筛选。

        kind ∈ {warm_to_hot, hot_to_warm, hot_to_boil, boil_to_hot, hot_or_boil}
        """
        sel = [r for r in rows if r.get("node_type") == node_type]
        cur_t = lambda r: r.get("temperature")  # noqa: E731
        prev_t = lambda r: r.get("prev_temperature")  # noqa: E731
        if kind == "warm_to_hot":
            sel = [r for r in sel if prev_t(r) == "温" and cur_t(r) == "热"]
        elif kind == "hot_to_warm":
            # 热→温 且 仍在右侧（说明经历过温→热进入）
            sel = [r for r in sel if prev_t(r) == "热" and cur_t(r) == "温" and r.get("is_right_side")]
        elif kind == "hot_to_boil":
            sel = [r for r in sel if prev_t(r) == "热" and cur_t(r) == "沸"]
        elif kind == "boil_to_hot":
            sel = [r for r in sel if prev_t(r) == "沸" and cur_t(r) == "热"]
        elif kind == "hot_or_boil":
            sel = [r for r in sel if cur_t(r) in ("热", "沸")]
        else:
            return []
        # 排序：沸 > 热 > 温；RS 高 → 低；名称兜底
        rank = {"沸": 2, "热": 1, "温": 0}
        sel.sort(key=lambda r: (-(rank.get(cur_t(r), -1)), -(r.get("rs_score") or 0), r.get("name") or ""))
        return sel

    # 取每个分组的最大 effective_trade_date（让前端能显示「L2 截至 08-12 / 个股截至 08-13」）
    l2_rows = [r for r in rows if r.get("node_type") == "industry_l2"]
    stock_rows = [r for r in rows if r.get("node_type") == "stock"]
    l2_effective = max((r.get("effective_trade_date") for r in l2_rows), default=None)
    stock_effective = max((r.get("effective_trade_date") for r in stock_rows), default=None)

    payload = {
        "trade_date": cur_iso,
        "l2_effective_date": l2_effective,
        "stock_effective_date": stock_effective,
        "l2_warm_to_hot": _pick("industry_l2", "warm_to_hot"),
        "l2_hot_to_warm": _pick("industry_l2", "hot_to_warm"),
        "l2_hot_to_boil": _pick("industry_l2", "hot_to_boil"),
        "l2_boil_to_hot": _pick("industry_l2", "boil_to_hot"),
        "l2_hot_or_boil": _pick("industry_l2", "hot_or_boil"),
        "stock_warm_to_hot": _pick("stock", "warm_to_hot"),
        "stock_hot_to_warm": _pick("stock", "hot_to_warm"),
        "stock_hot_to_boil": _pick("stock", "hot_to_boil"),
        "stock_boil_to_hot": _pick("stock", "boil_to_hot"),
        "stock_hot_or_boil": _pick("stock", "hot_or_boil"),
    }

    path = out_dir / "hotlist.json"
    return _write_json(path, payload)


def export_symbols(out_dir: Path, trade_date: date) -> int:
    """导出最近一日的全市场品种截面（列表页用）。

    相比上一版：
    - 新增 parent_name：父节点的品种名称（用于二级行业分组表头显示）
    - 新增 prev_temperature：上一交易日温度（用于显示"温→平"类副标签）
    """
    date_iso = trade_date.isoformat()
    prev_temp_expr = _prev_trade_date_sql(date_iso)
    sql = f"""
        SELECT
            s.symbol_id,
            s.name,
            s.node_type,
            s.parent_id,
            p.name AS parent_name,
            s.is_leaf,
            s.market_cap_float,
            s.data_status,
            dp.close,
            dp.pct_chg,
            dp.amount,
            di.temperature,
            di.temperature_score,
            di.rs_score,
            di.rs_score_trend,
            di.is_right_side,
            di.right_side_days,
            di.right_side_entry_temp,
            {prev_temp_expr} AS prev_temperature
        FROM symbols s
        LEFT JOIN symbols p ON p.symbol_id = s.parent_id
        LEFT JOIN daily_price dp
          ON dp.symbol_id = s.symbol_id AND dp.trade_date = :d
        LEFT JOIN daily_indicator di
          ON di.symbol_id = s.symbol_id AND di.trade_date = :d
        WHERE COALESCE(s.data_status, 'ok') != 'no_data'
        ORDER BY s.node_type, s.symbol_id
    """
    df = pd.read_sql(sql, db._engine, params={"d": date_iso})
    if df.empty:
        logger.warning("导出 symbols: 当日 %s 无数据", trade_date)
        payload = {"trade_date": date_iso, "rows": []}
    else:
        df = df.replace({np.nan: None})
        rows = _records_to_json(df.to_dict(orient="records"))
        for r in rows:
            if "is_leaf" in r and r["is_leaf"] is not None:
                r["is_leaf"] = bool(r["is_leaf"])
            if "is_right_side" in r and r["is_right_side"] is not None:
                r["is_right_side"] = bool(r["is_right_side"])
            # 温度副标签：仅当今日有温度且与昨日不同才生成
            cur = r.get("temperature")
            prev = r.get("prev_temperature")
            if cur and prev and cur != prev:
                r["temperature_change"] = f"{prev}→{cur}"
            else:
                r["temperature_change"] = None
        payload = {"trade_date": date_iso, "rows": rows}

    path = out_dir / "symbols.json"
    return _write_json(path, payload)


def export_top_card(out_dir: Path, trade_date: date, lookback_days: int = 365 * 6) -> int:
    """导出顶部详情卡数据。

    由于没有 A 股根节点的合成日线，这里用上证指数 (IDX_000001) 作为代理。
    卡片展示：最新价、日涨幅、4 个窗口（3M/1Y/3Y/6Y）累计、成交额、温度、RS。

    指数日线发布滞后于个股时（如个股已到 T 日、指数只到 T-4），
    卡片 fallback 到 IDX_000001 自身最近有数据的交易日，避免全空。

    series 同时返回 4 个区间：3M / 1Y / 3Y / 6Y，前端按按钮切换显示。
    每个区间独立做"断崖剔除"：相邻日 close 比例 > 5x 视为数据源切换，丢弃该点及之前数据。
    """
    # 指数数据可能滞后：fallback 到自身最近有数据的交易日
    eff_df = pd.read_sql(
        "SELECT MAX(trade_date) AS d FROM daily_price WHERE symbol_id = 'IDX_000001' AND trade_date <= :d",
        db._engine,
        params={"d": trade_date.isoformat()},
    )
    if eff_df.empty or eff_df["d"].isna().all():
        return 0
    effective_date = pd.to_datetime(eff_df["d"].iloc[0]).date()

    sql = """
        SELECT
            s.symbol_id, s.name, s.node_type,
            dp.close, dp.pct_chg, dp.amount,
            di.temperature, di.temperature_score,
            di.rs_score, di.rs_score_trend
        FROM symbols s
        LEFT JOIN daily_price dp
          ON dp.symbol_id = s.symbol_id AND dp.trade_date = :d
        LEFT JOIN daily_indicator di
          ON di.symbol_id = s.symbol_id AND di.trade_date = :d
        WHERE s.symbol_id = 'IDX_000001'
    """
    df = pd.read_sql(sql, db._engine, params={"d": effective_date.isoformat()})
    if df.empty:
        return 0
    df = df.replace({np.nan: None})
    row = df.iloc[0].to_dict()

    start_date = effective_date - timedelta(days=lookback_days)
    prices_df = pd.read_sql(
        """
        SELECT trade_date, close
        FROM daily_price
        WHERE symbol_id = 'IDX_000001'
          AND trade_date BETWEEN :start AND :end
          AND close IS NOT NULL
        ORDER BY trade_date
        """,
        db._engine,
        params={"start": start_date.isoformat(), "end": effective_date.isoformat()},
    )

    def _clean_break(df_in: pd.DataFrame) -> pd.DataFrame:
        if df_in.empty:
            return df_in
        df_in = df_in.copy()
        df_in["trade_date"] = pd.to_datetime(df_in["trade_date"])
        df_in = df_in.reset_index(drop=True)
        keep_from = 0
        for i in range(len(df_in) - 1, 0, -1):
            cur = float(df_in["close"].iloc[i])
            prev = float(df_in["close"].iloc[i - 1])
            if prev > 0 and (cur / prev > 5 or cur / prev < 0.2):
                keep_from = i
                break
        return df_in.iloc[keep_from:].reset_index(drop=True)

    cleaned = _clean_break(prices_df)

    def _build_window(window_days: int) -> dict:
        if cleaned.empty:
            return {"dates": [], "closes": [], "cum_pct": [], "high_pct": None}
        start = effective_date - timedelta(days=window_days)
        sub = cleaned[cleaned["trade_date"] >= pd.to_datetime(start)].reset_index(drop=True)
        if sub.empty:
            return {"dates": [], "closes": [], "cum_pct": [], "high_pct": None}
        closes = [float(v) if pd.notna(v) else None for v in sub["close"].tolist()]
        dates = sub["trade_date"].dt.strftime("%Y-%m-%d").tolist()
        base = closes[0]
        cum_pct = [None if c is None or base in (None, 0) else (c / base - 1) * 100 for c in closes]
        high_pct = None
        if base not in (None, 0):
            high = max(c for c in closes if c is not None)
            high_pct = (high / base - 1) * 100
        return {"dates": dates, "closes": closes, "cum_pct": cum_pct, "high_pct": high_pct}

    windows = {
        "3M": _build_window(92),
        "1Y": _build_window(365),
    }

    payload = {
        "trade_date": effective_date.isoformat(),
        "requested_trade_date": trade_date.isoformat(),
        "title": "A 股",
        "proxy_symbol": "IDX_000001",
        "name": row.get("name"),
        "close": row.get("close"),
        "pct_chg": row.get("pct_chg"),
        "amount": row.get("amount"),
        "temperature": row.get("temperature"),
        "rs_score": row.get("rs_score"),
        "rs_score_trend": row.get("rs_score_trend"),
        "cumulative": {
            k: (windows[k]["cum_pct"][-1] if windows[k]["cum_pct"] and windows[k]["cum_pct"][-1] is not None else None)
            for k in windows
        },
        "windows": windows,
    }
    path = out_dir / "top.json"
    return _write_json(path, payload)


def export_indicator_history(out_dir: Path, trade_date: date, lookback_days: int = 365) -> int:
    """为每个有日线 + 指标的品种输出 indicator-{symbol_id}.json。

    payload 字段：
        symbol_id, name, node_type, parent_name
        dates: [str]
        closes: [float|None]
        temperature: [str|None]      # 七档
        temperature_score: [float|None]
        is_right_side: [bool|None]
        right_side_days: [int|None]
        rs_score: [int|None]

    实现：流式分块读取（避免 1.3M 行一次性装入内存）。
    """
    from sqlalchemy import text

    start_date = trade_date - timedelta(days=lookback_days)
    out_dir = out_dir / "indicators"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 一次查 200 个 symbol 的日线，写完一批再查下一批
    BATCH_SYMBOLS = 200
    total_bytes = 0
    n_files = 0
    cols = ["close", "temperature", "temperature_score", "is_right_side", "right_side_days", "rs_score"]

    # 拿所有有日线的 symbol_id
    with db._engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT symbol_id FROM daily_price "
            "WHERE trade_date BETWEEN :start AND :end ORDER BY symbol_id"
        ), {"start": start_date.isoformat(), "end": trade_date.isoformat()}).fetchall()
        all_symbols = [r[0] for r in rows]
    logger.info("indicator 历史: %d 个 symbol 待导出", len(all_symbols))

    for batch_start in range(0, len(all_symbols), BATCH_SYMBOLS):
        batch_syms = all_symbols[batch_start:batch_start + BATCH_SYMBOLS]
        in_clause = ",".join(f":s{i}" for i in range(len(batch_syms)))
        params = {f"s{i}": s for i, s in enumerate(batch_syms)}
        params["start"] = start_date.isoformat()
        params["end"] = trade_date.isoformat()

        with db._engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    s.symbol_id, s.name, s.node_type, p.name AS parent_name,
                    dp.trade_date,
                    dp.close,
                    di.temperature,
                    di.temperature_score,
                    di.is_right_side,
                    di.right_side_days,
                    di.rs_score
                FROM symbols s
                LEFT JOIN symbols p ON p.symbol_id = s.parent_id
                JOIN daily_price dp ON dp.symbol_id = s.symbol_id
                LEFT JOIN daily_indicator di
                  ON di.symbol_id = s.symbol_id AND di.trade_date = dp.trade_date
                WHERE dp.trade_date BETWEEN :start AND :end
                  AND dp.symbol_id IN ({in_clause})
                ORDER BY s.symbol_id, dp.trade_date
            """), params).fetchall()

        # group by symbol_id
        from collections import defaultdict
        groups = defaultdict(list)
        meta = {}
        for r in rows:
            sid = r[0]
            if sid not in meta:
                meta[sid] = (r[1], r[2], r[3])  # name, node_type, parent_name
            groups[sid].append(r)

        for sid, g_rows in groups.items():
            name, node_type, parent_name = meta[sid]
            # 兼容 datetime 和 str
            def _date_str(v):
                if hasattr(v, 'strftime'):
                    return v.strftime("%Y-%m-%d")
                return str(v)[:10]
            g_rows.sort(key=lambda x: _date_str(x[4]))
            dates = [_date_str(g[4]) for g in g_rows]
            closes = [_to_jsonable(g[5]) for g in g_rows]
            temperatures = [_to_jsonable(g[6]) for g in g_rows]
            temperature_scores = [_to_jsonable(g[7]) for g in g_rows]
            is_right_sides = [_to_jsonable(g[8]) for g in g_rows]
            right_side_dayss = [_to_jsonable(g[9]) for g in g_rows]
            rs_scores = [_to_jsonable(g[10]) for g in g_rows]

            # 断崖剔除
            keep_from = 0
            for i in range(len(closes) - 1, 0, -1):
                cur, prev = closes[i], closes[i - 1]
                if cur and prev and prev > 0 and (cur / prev > 5 or cur / prev < 0.2):
                    keep_from = i
                    break
            dates = dates[keep_from:]
            closes = closes[keep_from:]
            temperatures = temperatures[keep_from:]
            temperature_scores = temperature_scores[keep_from:]
            is_right_sides = is_right_sides[keep_from:]
            right_side_dayss = right_side_dayss[keep_from:]
            rs_scores = rs_scores[keep_from:]

            payload = {
                "symbol_id": sid,
                "name": name,
                "node_type": node_type,
                "parent_name": parent_name,
                "dates": dates,
                "close": closes,
                "temperature": temperatures,
                "temperature_score": temperature_scores,
                "is_right_side": is_right_sides,
                "right_side_days": right_side_dayss,
                "rs_score": rs_scores,
            }

            path = out_dir / f"indicator-{sid}.json"
            total_bytes += _write_json(path, payload)
            n_files += 1

    logger.info("导出 indicator 历史: %s 品种, 共 %s 字节", n_files, total_bytes)
    return n_files


def export_benchmark(out_dir: Path, trade_date: date, symbol: str = "IDX_000300", lookback_days: int = 365) -> int:
    """导出基准品种（默认沪深 300）的 close 序列，供详情页做对比。"""
    start_date = trade_date - timedelta(days=lookback_days)
    df = pd.read_sql(
        """
        SELECT trade_date, close
        FROM daily_price
        WHERE symbol_id = :sid
          AND trade_date BETWEEN :start AND :end
          AND close IS NOT NULL
        ORDER BY trade_date
        """,
        db._engine,
        params={"sid": symbol, "start": start_date.isoformat(), "end": trade_date.isoformat()},
    )
    if df.empty:
        return 0
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")

    # 断崖剔除
    df = df.reset_index(drop=True)
    keep_from = 0
    closes = df["close"].tolist()
    for i in range(len(closes) - 1, 0, -1):
        cur, prev = closes[i], closes[i - 1]
        if cur and prev and prev > 0 and (cur / prev > 5 or cur / prev < 0.2):
            keep_from = i
            break
    df = df.iloc[keep_from:].reset_index(drop=True)

    # 累计涨幅（基准 = 0% 起点）
    base = float(df["close"].iloc[0])
    cum_pct = [None if (c is None or base in (None, 0)) else (float(c) / base - 1) * 100
               for c in df["close"].tolist()]
    payload = {
        "symbol_id": symbol,
        "name": "沪深300",
        "dates": df["trade_date"].tolist(),
        "cum_pct": cum_pct,
    }
    path = out_dir / "benchmark.json"
    return _write_json(path, payload)


def export_prices(out_dir: Path, trade_date: date, lookback_days: int = 365, chunk_size: int = 500) -> int:
    """导出近 lookback_days 天日线（详情页用），分片输出为 prices-N.json。

    每片 payload: {"trade_date": str, "lookback_days": int, "symbols": [...], "dates": [...], "closes": [[...]]}
    这样前端一次 fetch 拿到全部分片数据即可渲染任意品种曲线。

    实现：流式分块读 + 分片写，避免 1.3M 行一次性装内存。
    chunk_size 取 500：2000 时在低内存机器上 fetchall 阶段会 OOM（~50 万行 × 4 列 Python 对象）。
    """
    from sqlalchemy import text

    start_date = trade_date - timedelta(days=lookback_days)

    # 拿所有 symbols（按 trade_date = end_date 当天有数据的）
    with db._engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT dp2.symbol_id FROM daily_price dp2
            WHERE dp2.trade_date = :d
            ORDER BY dp2.symbol_id
        """), {"d": trade_date.isoformat()}).fetchall()
        all_symbols = [r[0] for r in rows]

    if not all_symbols:
        logger.warning("导出 prices: %s 附近无日线数据", trade_date)
        return 0

    # 拿所有 dates
    with db._engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date BETWEEN :start AND :end
            ORDER BY trade_date
        """), {"start": start_date.isoformat(), "end": trade_date.isoformat()}).fetchall()
        def _date_str(v):
            if hasattr(v, 'strftime'):
                return v.strftime("%Y-%m-%d")
            return str(v)[:10]
        all_dates = [_date_str(r[0]) for r in rows]

    symbol_to_idx = {s: i for i, s in enumerate(all_symbols)}
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    logger.info("导出 prices: %d symbols × %d dates = %d cells", len(all_symbols), len(all_dates),
                len(all_symbols) * len(all_dates))

    total_chunks = (len(all_symbols) + chunk_size - 1) // chunk_size
    bytes_total = 0

    # 清掉旧分片（chunk_size 调整后会残留过期文件）
    for old in out_dir.glob("prices-*.json"):
        old.unlink()

    for chunk_idx in range(total_chunks):
        sl = slice(chunk_idx * chunk_size, (chunk_idx + 1) * chunk_size)
        chunk_symbols = all_symbols[sl]
        keep_rows = set(range(sl.start, sl.stop))

        # 仅查本批的 symbols 的日线
        in_clause = ",".join(f":s{i}" for i in range(len(chunk_symbols)))
        params = {f"s{i}": s for i, s in enumerate(chunk_symbols)}
        params["start"] = start_date.isoformat()
        params["end"] = trade_date.isoformat()

        sub_rows: List[int] = []
        sub_cols: List[int] = []
        sub_close: List[Optional[float]] = []
        sub_pct: List[Optional[float]] = []

        with db._engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT dp.symbol_id, dp.trade_date, dp.close, dp.pct_chg
                FROM daily_price dp
                WHERE dp.trade_date BETWEEN :start AND :end
                  AND dp.symbol_id IN ({in_clause})
            """), params).fetchall()

        for r in rows:
            sid = r[0]
            if sid not in keep_rows:
                continue
            date_str = _date_str(r[1])
            sub_rows.append(symbol_to_idx[sid] - sl.start)
            sub_cols.append(date_to_idx[date_str])
            sub_close.append(_to_jsonable(r[2]))
            sub_pct.append(_to_jsonable(r[3]))

        payload = {
            "trade_date": trade_date.isoformat(),
            "lookback_days": lookback_days,
            "chunk": chunk_idx,
            "total_chunks": total_chunks,
            "dates": all_dates,
            "symbols": chunk_symbols,
            "rows": sub_rows,
            "cols": sub_cols,
            "closes": sub_close,
            "pct_chg": sub_pct,
        }
        path = out_dir / f"prices-{chunk_idx}.json"
        bytes_total += _write_json(path, payload)
    logger.info("导出价格分片: %s 片，共 %s 字节", total_chunks, bytes_total)
    return total_chunks


def export_all(out_dir: Optional[Path] = None) -> ExportResult:
    """导出全部静态文件。"""
    out_dir = out_dir or WEB_DATA_DIR
    trade_date = _get_latest_trade_date()
    if trade_date is None:
        raise RuntimeError("daily_indicator 为空，请先运行指标计算管道")

    b1 = export_dashboard(out_dir, trade_date)
    b2 = export_symbols(out_dir, trade_date)
    b3 = export_top_card(out_dir, trade_date)
    b_hotlist = export_hotlist(out_dir, trade_date)
    n_files = export_indicator_history(out_dir, trade_date)
    b4 = export_benchmark(out_dir, trade_date)
    n_chunks = export_prices(out_dir, trade_date)
    # 历史快照（近 10 个交易日）
    b5 = export_history_snapshots(out_dir, history_days=10)
    # 三级页面视图（index / l1 / l2）
    l1_index_bytes = export_l1_index(out_dir, trade_date)
    l1_detail_bytes, l1_count = export_l1_details(out_dir, trade_date)
    l2_detail_bytes, l2_count = export_l2_details(out_dir, trade_date)
    # price chunk 字节数
    bytes_prices = 0
    for i in range(n_chunks):
        p = out_dir / f"prices-{i}.json"
        if p.exists():
            bytes_prices += p.stat().st_size

    result = ExportResult(
        trade_date=trade_date,
        symbol_count=6075,
        price_chunks=n_chunks,
        bytes_total=b1 + b2 + b3 + b4 + b5 + b_hotlist + l1_index_bytes + l1_detail_bytes + l2_detail_bytes + bytes_prices,
    )
    _reset_history_cache()
    return result


# ---------------------------------------------------------------------------
# 三级页面视图导出
# ---------------------------------------------------------------------------

def _l1_index_sql(date_iso: str) -> str:
    """一级页 index-l1.json 的 SQL：所有 index + industry_l1 节点 + 子聚合。"""
    prev_temp_expr = _prev_trade_date_sql(date_iso)
    return f"""
        SELECT
            s.symbol_id, s.name, s.node_type,
            dp.close, dp.pct_chg, dp.amount,
            di.temperature, di.temperature_score,
            di.rs_score, di.rs_score_trend,
            di.is_right_side, di.right_side_days,
            {prev_temp_expr} AS prev_temperature,
            (SELECT COUNT(*) FROM symbols c WHERE c.parent_id = s.symbol_id) AS children_count,
            (SELECT COUNT(*) FROM daily_indicator di2
              JOIN symbols c2 ON c2.symbol_id = di2.symbol_id
              WHERE c2.parent_id = s.symbol_id
                AND di2.trade_date = :d
                AND di2.is_right_side = 1) AS right_side_count
        FROM symbols s
        LEFT JOIN daily_price dp
          ON dp.symbol_id = s.symbol_id AND dp.trade_date = :d
        LEFT JOIN daily_indicator di
          ON di.symbol_id = s.symbol_id AND di.trade_date = :d
        WHERE s.node_type IN ('index', 'industry_l1')
          AND COALESCE(s.data_status, 'ok') != 'no_data'
        ORDER BY
          CASE s.node_type WHEN 'index' THEN 0 ELSE 1 END,
          s.symbol_id
    """


def export_l1_index(out_dir: Path, trade_date: date) -> int:
    """导出 index-l1.json：一级页列表用。"""
    date_iso = trade_date.isoformat()
    df = pd.read_sql(_l1_index_sql(date_iso), db._engine, params={"d": date_iso})
    df = df.replace({np.nan: None})
    rows = _records_to_json(df.to_dict(orient="records"))
    # Fallback：L1 / index / concept 的 akshare 数据可能比 stock 晚一天
    # （L1 行业指数 / 概念指数 akshare 7-10 还没发布），用各自最新有指标 + 价格的一天回填
    for r in rows:
        if r.get("is_right_side") is not None:
            r["is_right_side"] = bool(r["is_right_side"])
        if r.get("node_type") in ("index", "industry_l1", "concept"):
            if r.get("close") is None or r.get("temperature") is None:
                fb = _fallback_industry_row(r["symbol_id"])
                if fb:
                    if r.get("close") is None:
                        r["close"] = fb.get("close")
                        r["pct_chg"] = fb.get("pct_chg")
                        if fb.get("trade_date"):
                            r["trade_date"] = fb["trade_date"]
                    if r.get("temperature") is None:
                        r["temperature"] = fb.get("temperature")
                        r["temperature_score"] = fb.get("temperature_score")
                        r["rs_score"] = fb.get("rs_score")
                        r["rs_score_trend"] = fb.get("rs_score_trend")
                        r["is_right_side"] = fb.get("is_right_side")
                        r["right_side_days"] = fb.get("right_side_days")
        cur = r.get("temperature")
        prev = r.get("prev_temperature")
        if cur and prev and cur != prev:
            r["temperature_change"] = f"{prev}→{cur}"
        else:
            r["temperature_change"] = None
    payload = {"trade_date": date_iso, "groups": rows}
    return _write_json(out_dir / "index-l1.json", payload)


def _latest_indicator(symbol_id: str) -> Optional[dict]:
    """回退查询：取该 symbol 最新有指标的那天。"""
    df = pd.read_sql(
        """
        SELECT temperature, temperature_score, rs_score, rs_score_trend,
               is_right_side, right_side_days
        FROM daily_indicator
        WHERE symbol_id = :sid
          AND temperature IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
        """,
        db._engine,
        params={"sid": symbol_id},
    )
    if df.empty:
        return None
    return _to_jsonable_dict(df.iloc[0].to_dict())


def _fallback_industry_row(symbol_id: str) -> Optional[dict]:
    """行业 / 指数的最新可回退行（用于 akshare 行业指数 / 概念指数滞后场景）。

    字段：close / pct_chg / temperature / temperature_score / rs_score / rs_score_trend /
          is_right_side / right_side_days / trade_date
    """
    df = pd.read_sql(
        """
        SELECT dp.trade_date, dp.close, dp.pct_chg,
               di.temperature, di.temperature_score,
               di.rs_score, di.rs_score_trend,
               di.is_right_side, di.right_side_days
        FROM daily_price dp
        LEFT JOIN daily_indicator di
          ON di.symbol_id = dp.symbol_id AND di.trade_date = dp.trade_date
        WHERE dp.symbol_id = :sid
        ORDER BY dp.trade_date DESC LIMIT 1
        """,
        db._engine,
        params={"sid": symbol_id},
    )
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    row["trade_date"] = pd.to_datetime(row["trade_date"]).strftime("%Y-%m-%d") if row.get("trade_date") else None
    return _to_jsonable_dict(row)


def _to_jsonable_dict(d: dict) -> dict:
    return {k: _to_jsonable(v) for k, v in d.items()}


def _detail_sql(date_iso: str, child_filter_sql: str, parent_join: str) -> str:
    """child_filter_sql: 用于 children_count / right_side_count 子查询的 node_type 过滤。
    parent_join: 用于关联子节点的 SQL 条件。
        - L1 行业：l1_children 是 L2 → 用 c.parent_id = s.symbol_id
        - L2 行业：l2_children 是 stock → 用 c.l2_industry_id = s.symbol_id
    """
    prev_temp_expr = _prev_trade_date_sql(date_iso)
    return f"""
        SELECT
            s.symbol_id, s.name, s.node_type, s.parent_id, s.l2_industry_id,
            p.name AS parent_name,
            dp.close, dp.pct_chg, dp.amount,
            di.temperature, di.temperature_score,
            di.rs_score, di.rs_score_trend,
            di.is_right_side, di.right_side_days,
            {prev_temp_expr} AS prev_temperature,
            (SELECT COUNT(*) FROM symbols c
              WHERE {parent_join}
                AND c.node_type {child_filter_sql}) AS children_count,
            (SELECT COUNT(*) FROM daily_indicator di2
              WHERE di2.symbol_id = s.symbol_id
                AND di2.trade_date = :d
                AND di2.is_right_side = 1) AS right_side_count
        FROM symbols s
        LEFT JOIN symbols p ON p.symbol_id = s.parent_id
        LEFT JOIN daily_price dp
          ON dp.symbol_id = s.symbol_id AND dp.trade_date = :d
        LEFT JOIN daily_indicator di
          ON di.symbol_id = s.symbol_id AND di.trade_date = :d
        WHERE COALESCE(s.data_status, 'ok') != 'no_data'
    """


def export_l1_details(out_dir: Path, trade_date: date) -> tuple[int, int]:
    """导出 l1-{id}.json：每个 L1 行业一个文件。

    只输出该 L1 下的 l2_children（l1.html 已不显示 stock 列表，避免大 JSON）。
    """
    date_iso = trade_date.isoformat()
    # 主表（只查 l2）
    main_df = pd.read_sql(
        _detail_sql(date_iso, "= 'industry_l2'", "c.parent_id = s.symbol_id"),
        db._engine,
        params={"d": date_iso},
    )
    main_df = main_df.replace({np.nan: None})
    by_id = {r["symbol_id"]: r for r in main_df.to_dict(orient="records") if r.get("node_type") == "industry_l1"}
    children_df = main_df[main_df["node_type"] == "industry_l2"].copy()

    total_bytes = 0
    n_files = 0
    for l1_id, l1_row in by_id.items():
        sub = children_df[children_df["parent_id"] == l1_id]
        # to_dict 把 NaN 转 float('nan') 而非 None，先归一化
        l2_children_raw = sub.sort_values("symbol_id").to_dict(orient="records")
        l2_children = [{k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.items()} for r in l2_children_raw]
        # 统一过 _to_jsonable 过滤 NaN/Inf
        clean = {k: _to_jsonable(v) for k, v in dict(l1_row).items()}
        if clean.get("is_right_side") is not None:
            clean["is_right_side"] = bool(clean["is_right_side"])
        clean["temperature_change"] = _fmt_change(clean.get("temperature"), clean.pop("prev_temperature", None))
        # Fallback：L1 行业 akshare 行业指数可能滞后一天（缺 close / 缺指标都要回退）
        if clean.get("close") is None or clean.get("temperature") is None:
            fb = _fallback_industry_row(l1_id)
            if fb:
                if clean.get("close") is None:
                    clean["close"] = fb.get("close")
                    clean["pct_chg"] = fb.get("pct_chg")
                    if fb.get("trade_date"):
                        clean["trade_date"] = fb["trade_date"]
                if clean.get("temperature") is None:
                    clean["temperature"] = fb.get("temperature")
                    clean["temperature_score"] = fb.get("temperature_score")
                    clean["rs_score"] = fb.get("rs_score")
                    clean["rs_score_trend"] = fb.get("rs_score_trend")
                    clean["is_right_side"] = fb.get("is_right_side")
                    clean["right_side_days"] = fb.get("right_side_days")
        # 同样给每个 l2_children 做回退
        for child in l2_children:
            if child.get("close") is None or child.get("temperature") is None:
                fb = _fallback_industry_row(child["symbol_id"])
                if fb:
                    if child.get("close") is None:
                        child["close"] = fb.get("close")
                        child["pct_chg"] = fb.get("pct_chg")
                    if child.get("temperature") is None:
                        child["temperature"] = fb.get("temperature")
                        child["temperature_score"] = fb.get("temperature_score")
                        child["rs_score"] = fb.get("rs_score")
                        child["rs_score_trend"] = fb.get("rs_score_trend")
                        child["is_right_side"] = fb.get("is_right_side")
                        child["right_side_days"] = fb.get("right_side_days")
        payload = {
            "trade_date": clean.get("trade_date") or date_iso,
            "requested_trade_date": date_iso,
            "symbol_id": l1_id,
            "name": clean.get("name"),
            "node_type": clean.get("node_type"),
            "close": clean.get("close"),
            "pct_chg": clean.get("pct_chg"),
            "temperature": clean.get("temperature"),
            "temperature_change": clean.get("temperature_change"),
            "rs_score": clean.get("rs_score"),
            "rs_score_trend": clean.get("rs_score_trend"),
            "children_count": clean.get("children_count"),
            "right_side_count": clean.get("right_side_count"),
            "history": _load_indicator_history(l1_id, trade_date),
            "l2_children": [_to_record_json(r) for r in l2_children],
        }
        total_bytes += _write_json(out_dir / f"l1-{l1_id}.json", payload)
        n_files += 1
    logger.info("导出 l1 详情: %s 个, 共 %s 字节", n_files, total_bytes)
    return total_bytes, n_files


def export_l2_details(out_dir: Path, trade_date: date) -> tuple[int, int]:
    """导出 l2-{id}.json：每个 L2 行业一个文件，含下属个股。"""
    date_iso = trade_date.isoformat()
    main_df = pd.read_sql(
        _detail_sql(date_iso, "= 'stock'", "c.l2_industry_id = s.symbol_id"),
        db._engine,
        params={"d": date_iso},
    )
    main_df = main_df.replace({np.nan: None})

    total_bytes = 0
    n_files = 0
    for _, l2_row in main_df[main_df["node_type"] == "industry_l2"].iterrows():
        l2_id = l2_row["symbol_id"]
        # stock 的 l2_industry_id 指向 L2（不是 parent_id）
        sub = main_df[(main_df["node_type"] == "stock") & (main_df["l2_industry_id"] == l2_id)]
        sub_records = [_to_record_json(r) for r in sub.to_dict(orient="records")]
        clean = {k: _to_jsonable(v) for k, v in l2_row.to_dict().items()}
        # Fallback：L2 行业 akshare 行业指数 / 概念指数可能滞后一天
        if clean.get("close") is None or clean.get("temperature") is None:
            fb = _fallback_industry_row(l2_id)
            if fb:
                if clean.get("close") is None:
                    clean["close"] = fb.get("close")
                    clean["pct_chg"] = fb.get("pct_chg")
                    if fb.get("trade_date"):
                        clean["trade_date"] = fb["trade_date"]
                if clean.get("temperature") is None:
                    clean["temperature"] = fb.get("temperature")
                    clean["temperature_score"] = fb.get("temperature_score")
                    clean["rs_score"] = fb.get("rs_score")
                    clean["rs_score_trend"] = fb.get("rs_score_trend")
                    clean["is_right_side"] = fb.get("is_right_side")
                    clean["right_side_days"] = fb.get("right_side_days")
        payload = {
            "trade_date": clean.get("trade_date") or date_iso,
            "requested_trade_date": date_iso,
            "symbol_id": l2_id,
            "name": clean.get("name"),
            "node_type": clean.get("node_type"),
            "parent_id": clean.get("parent_id"),
            "parent_name": clean.get("parent_name"),
            "close": clean.get("close"),
            "pct_chg": clean.get("pct_chg"),
            "temperature": clean.get("temperature"),
            "temperature_change": _fmt_change(clean.get("temperature"), clean.pop("prev_temperature", None)),
            "rs_score": clean.get("rs_score"),
            "rs_score_trend": clean.get("rs_score_trend"),
            "children_count": clean.get("children_count"),
            "right_side_count": clean.get("right_side_count"),
            "history": _load_indicator_history(l2_id, trade_date),
            "stocks": sub_records,
        }
        total_bytes += _write_json(out_dir / f"l2-{l2_id}.json", payload)
        n_files += 1
    logger.info("导出 l2 详情: %s 个, 共 %s 字节", n_files, total_bytes)
    return total_bytes, n_files


def _to_record_json(r: dict) -> dict:
    """把单行 main_df dict 转 JSON 可序列化（补 temperature_change）。"""
    out = dict(r)
    if out.get("is_right_side") is not None:
        out["is_right_side"] = bool(out["is_right_side"])
    out["temperature_change"] = _fmt_change(out.get("temperature"), out.get("prev_temperature"))
    out.pop("prev_temperature", None)
    return {k: _to_jsonable(v) for k, v in out.items()}


def _fmt_change(cur, prev) -> Optional[str]:
    if cur and prev and cur != prev:
        return f"{prev}→{cur}"
    return None


def export_history_snapshots(out_dir: Path, history_days: int = 10) -> int:
    """导出近 history_days 个交易日的 symbols 快照 + dates.json 清单。

    适用于"前 N 天日期切换"功能：
        web/data/dates.json             -> ["2026-07-09", "2026-07-08", ...]
        web/data/snapshots/{date}.json  -> 同 symbols.json 结构
    """
    df = pd.read_sql(
        """
        SELECT DISTINCT trade_date
        FROM daily_indicator
        WHERE temperature IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT :n
        """,
        db._engine,
        params={"n": history_days},
    )
    if df.empty:
        return 0
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    dates = sorted(df["trade_date"].dt.strftime("%Y-%m-%d").tolist(), reverse=True)

    snap_dir = out_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    for d in dates:
        # 直接调 export_symbols 但写到 snapshots/{d}.json
        date_iso = d
        prev_temp_expr = (
            "(SELECT temperature FROM daily_indicator di2 "
            "WHERE di2.symbol_id = s.symbol_id "
            f"AND di2.trade_date < '{date_iso}' "
            "ORDER BY di2.trade_date DESC LIMIT 1)"
        )
        sql = f"""
            SELECT
                s.symbol_id, s.name, s.node_type, s.parent_id,
                p.name AS parent_name, s.is_leaf, s.market_cap_float, s.data_status,
                dp.close, dp.pct_chg, dp.amount,
                di.temperature, di.temperature_score,
                di.rs_score, di.rs_score_trend,
                di.is_right_side, di.right_side_days, di.right_side_entry_temp,
                {prev_temp_expr} AS prev_temperature
            FROM symbols s
            LEFT JOIN symbols p ON p.symbol_id = s.parent_id
            LEFT JOIN daily_price dp
              ON dp.symbol_id = s.symbol_id AND dp.trade_date = :d
            LEFT JOIN daily_indicator di
              ON di.symbol_id = s.symbol_id AND di.trade_date = :d
            WHERE COALESCE(s.data_status, 'ok') != 'no_data'
            ORDER BY s.node_type, s.symbol_id
        """
        df_day = pd.read_sql(sql, db._engine, params={"d": date_iso})
        if df_day.empty:
            continue
        df_day = df_day.replace({np.nan: None})
        rows = _records_to_json(df_day.to_dict(orient="records"))
        for r in rows:
            if r.get("is_leaf") is not None:
                r["is_leaf"] = bool(r["is_leaf"])
            if r.get("is_right_side") is not None:
                r["is_right_side"] = bool(r["is_right_side"])
            cur = r.get("temperature")
            prev = r.get("prev_temperature")
            if cur and prev and cur != prev:
                r["temperature_change"] = f"{prev}→{cur}"
            else:
                r["temperature_change"] = None
        payload = {"trade_date": date_iso, "rows": rows}
        path = snap_dir / f"{date_iso}.json"
        total_bytes += _write_json(path, payload)

    # 写 dates.json（按日期降序）
    total_bytes += _write_json(out_dir / "dates.json", dates)
    logger.info("导出历史快照: %s 个日期, 共 %s 字节", len(dates), total_bytes)
    return total_bytes
