# -*- coding: utf-8 -*-
"""指标计算引擎：批处理 + 增量更新。"""

import logging
from datetime import date, timedelta
from typing import List, Optional

import pandas as pd
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src import db
from src.daily_sync import get_latest_trade_date
from src.indicators.relative_strength import (
    calculate_rs_trend,
    rank_rs_by_node_type,
    weighted_return_series,
)
from src.indicators.right_side import compute_right_side_state
from src.indicators.temperature import classify_temperature
from src.models import DailyIndicator

logger = logging.getLogger(__name__)

# 为增量更新预留的额外历史长度（必须 >= 120，留一点安全垫）
_LOOKBACK_BUFFER_DAYS = 150

# RS 增量 upsert 最小窗口（自然日）：日更时只重写最近这段，历史不动。
# 取 14 自然日（约 10 个交易日），覆盖长假后首个日更的重写需求。
_RS_UPSERT_MIN_DAYS = 14

# RS 读取窗口在 upsert 窗口基础上额外前推的自然日数。
# weighted_return 最长窗口是 252 个交易日，读取必须再多留 ~400 自然日
# （约 273 个交易日），否则缺窗口 reweight 会让增量结果与全量口径不一致。
_RS_READ_BUFFER_DAYS = 400

# 温度增量重写窗口（自然日）：每天除写新日期外，回写最近这段历史。
# 温度状态机的输出依赖整条价格序列，日更补缺/源修订历史价格后，
# 只写新日期会让新行与旧行在边界处跳档（2026-07-29 事故：1297 个品种
# 在 07-28 单日跳 2-4 档）。右侧状态机同理，种子按同一窗口回滚。
# 若 daily_sync 记录了更早的价格改写断点（data/price_revisions.json），
# 窗口会进一步前推到断点之前。
_TEMP_REWRITE_DAYS = 21

# SQLite 占位符数量上限保守值
_BATCH_SIZE = 900

# 指标计算的品种分块大小：6000+ 品种一次性读入 daily_price 会 OOM
# （1.6M 行 × 9 列，SQLAlchemy fetchall 阶段 Python 对象开销巨大），
# 温度/RS/右侧三个步骤都按此分块流式处理。
_CALC_BATCH_SYMBOLS = 500


def _chunked(items: List[str], size: int = _BATCH_SIZE):
    """将列表分块，避免 SQL IN 占位符超限。"""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _load_price_revisions() -> dict:
    """读取 daily_sync 落盘的价格改写断点 {symbol_id: date}（最早被写入/修订的交易日）。"""
    import json

    from src import config

    path = config.LOCAL_DB_PATH.parent / "price_revisions.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for sid, ds in data.items():
        try:
            out[sid] = date.fromisoformat(ds)
        except ValueError:
            continue
    return out


def _clear_price_revisions(symbol_ids: List[str]) -> None:
    """指标重算完成后，从断点文件中移除已处理的品种。"""
    import json

    from src import config

    path = config.LOCAL_DB_PATH.parent / "price_revisions.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    for sid in symbol_ids:
        data.pop(sid, None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_daily_prices(
    symbol_ids: List[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """批量读取 daily_price，返回长表。"""
    if not symbol_ids:
        return pd.DataFrame(
            columns=["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
        )

    chunks = []
    for chunk in _chunked(symbol_ids):
        placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
        params = {f"s{i}": s for i, s in enumerate(chunk)}
        params["start_date"] = start_date.isoformat()
        params["end_date"] = end_date.isoformat()

        query = f"""
            SELECT symbol_id, trade_date, open, high, low, close, volume, amount, pct_chg
            FROM daily_price
            WHERE symbol_id IN ({placeholders})
              AND trade_date BETWEEN :start_date AND :end_date
            ORDER BY symbol_id, trade_date
        """
        df = pd.read_sql(query, db._engine, params=params)
        chunks.append(df)

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def _read_prices_with_node_type(
    symbol_ids: List[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """读取 daily_price 并带上 symbols.node_type，用于 RS 横截面排名。"""
    if not symbol_ids:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "close", "node_type"])

    chunks = []
    for chunk in _chunked(symbol_ids):
        placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
        params = {f"s{i}": s for i, s in enumerate(chunk)}
        params["start_date"] = start_date.isoformat()
        params["end_date"] = end_date.isoformat()

        query = f"""
            SELECT dp.symbol_id, dp.trade_date, dp.close, s.node_type
            FROM daily_price dp
            JOIN symbols s ON dp.symbol_id = s.symbol_id
            WHERE dp.symbol_id IN ({placeholders})
              AND dp.trade_date BETWEEN :start_date AND :end_date
            ORDER BY dp.symbol_id, dp.trade_date
        """
        df = pd.read_sql(query, db._engine, params=params)
        chunks.append(df)

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def _compute_temperature(df: pd.DataFrame) -> pd.DataFrame:
    """对 daily_price 长表分组计算温度，返回 indicator 长表。"""
    if df.empty:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "temperature_score", "temperature"])

    results = []
    for symbol_id, group in df.groupby("symbol_id", sort=False):
        group = group.sort_values("trade_date").reset_index(drop=True)
        if len(group) < 2:
            continue
        try:
            temp_df = classify_temperature(group["close"], group["high"], group["low"])
            valid = temp_df.dropna(subset=["temperature_score"]).copy()
            if valid.empty:
                continue
            valid["symbol_id"] = symbol_id
            valid["trade_date"] = group.loc[valid.index, "trade_date"].values
            results.append(valid[["symbol_id", "trade_date", "temperature_score", "temperature"]])
        except Exception as exc:
            logger.warning("计算 %s 温度失败: %s", symbol_id, exc)

    if not results:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "temperature_score", "temperature"])
    return pd.concat(results, ignore_index=True)


def _compute_weighted_returns(df: pd.DataFrame) -> pd.DataFrame:
    """对每个品种计算多周期加权收益率（per-symbol 操作，可分块调用）。

    输入列：symbol_id, trade_date, close, node_type
    输出列：symbol_id, trade_date, node_type, weighted_return
    """
    if df.empty:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "node_type", "weighted_return"])

    returns = []
    for symbol_id, group in df.groupby("symbol_id", sort=False):
        group = group.sort_values("trade_date").reset_index(drop=True)
        if len(group) < 2:
            continue
        group["weighted_return"] = weighted_return_series(group["close"])
        returns.append(group[["symbol_id", "trade_date", "node_type", "weighted_return"]])

    if not returns:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "node_type", "weighted_return"])
    return pd.concat(returns, ignore_index=True)


def _rank_rs(returns_df: pd.DataFrame) -> pd.DataFrame:
    """横截面排名 + 趋势箭头（必须全量截面，不可分块）。"""
    if returns_df.empty:
        return pd.DataFrame(
            columns=[
                "symbol_id",
                "trade_date",
                "weighted_return",
                "rs_score",
                "rs_score_prev_1d",
                "rs_score_prev_5d",
                "rs_score_trend",
            ]
        )
    ranked = rank_rs_by_node_type(returns_df)
    trended = calculate_rs_trend(ranked[["symbol_id", "trade_date", "rs_score"]])
    return ranked.merge(
        trended[["symbol_id", "trade_date", "rs_score_prev_1d", "rs_score_prev_5d", "rs_score_trend"]],
        on=["symbol_id", "trade_date"],
        how="left",
    )


def _compute_rs(df: pd.DataFrame) -> pd.DataFrame:
    """对 daily_price 长表分组计算 RS 分数与趋势箭头。"""
    return _rank_rs(_compute_weighted_returns(df))


def _get_right_side_seeds(before_dates: Optional[dict] = None) -> dict:
    """返回每个品种已算过右侧状态的最后一天及其状态，作为增量续算的种子。

    before_dates 非空时，对应品种（历史价格被改写的品种）改用"改写日之前
    最后一行右侧状态"作为种子，使状态机重算改写窗口；若改写日之前没有
    任何右侧行，则该品种不出现在返回值中（调用方应对其全历史冷启动）。

    Returns:
        {symbol_id: (trade_date, is_right_side, right_side_days, right_side_entry_temp)}
    """
    before_dates = before_dates or {}
    df = pd.read_sql(
        """
        SELECT di.symbol_id, di.trade_date, di.is_right_side, di.right_side_days, di.right_side_entry_temp
        FROM daily_indicator di
        JOIN (
            SELECT symbol_id, MAX(trade_date) AS md
            FROM daily_indicator
            WHERE is_right_side IS NOT NULL
            GROUP BY symbol_id
        ) t ON di.symbol_id = t.symbol_id AND di.trade_date = t.md
        """,
        db._engine,
    )
    seeds = {}
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        for r in df.itertuples():
            seeds[r.symbol_id] = (
                r.trade_date,
                bool(r.is_right_side),
                int(r.right_side_days),
                r.right_side_entry_temp,
            )

    rollback = {sid: d for sid, d in before_dates.items() if sid in seeds and d is not None}
    if rollback:
        # 只需"改写日前最后一行"：读改写窗口前一小段即可（指标行按交易日连续）
        min_bd = min(rollback.values())
        since = (min_bd - timedelta(days=45)).isoformat()
        frames = []
        for chunk in _chunked(list(rollback)):
            placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
            params = {f"s{i}": s for i, s in enumerate(chunk)}
            params["since"] = since
            frames.append(
                pd.read_sql(
                    f"""
                    SELECT symbol_id, trade_date, is_right_side, right_side_days, right_side_entry_temp
                    FROM daily_indicator
                    WHERE is_right_side IS NOT NULL AND trade_date >= :since AND symbol_id IN ({placeholders})
                    """,
                    db._engine,
                    params=params,
                )
            )
        rolled = set()
        df2 = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not df2.empty:
            df2["trade_date"] = pd.to_datetime(df2["trade_date"]).dt.date
            df2["_bd"] = df2["symbol_id"].map(rollback)
            df2 = df2[df2["trade_date"] < df2["_bd"]]
            if not df2.empty:
                last = df2.sort_values("trade_date").groupby("symbol_id").tail(1)
                for r in last.itertuples():
                    seeds[r.symbol_id] = (
                        r.trade_date,
                        bool(r.is_right_side),
                        int(r.right_side_days),
                        r.right_side_entry_temp,
                    )
                    rolled.add(r.symbol_id)
        # 改写日之前没有任何右侧行的品种：移除种子，走全历史冷启动重算
        for sid in rollback:
            if sid not in rolled:
                seeds.pop(sid, None)
    return seeds


def _compute_right_side(df: pd.DataFrame, seeds: Optional[dict] = None) -> pd.DataFrame:
    """对 daily_indicator 长表分组计算右侧状态。

    输入列：symbol_id, trade_date, temperature
    输出列增加：is_right_side, right_side_days, right_side_entry_temp

    seeds 非空时，对应品种只计算种子日期之后的新行（状态机从种子状态续算），
    避免冷启动篡改长期右侧品种的历史状态。
    """
    if df.empty or "temperature" not in df.columns:
        return pd.DataFrame(
            columns=["symbol_id", "trade_date", "is_right_side", "right_side_days", "right_side_entry_temp"]
        )

    seeds = seeds or {}
    results = []
    for symbol_id, group in df.groupby("symbol_id", sort=False):
        group = group.sort_values("trade_date").reset_index(drop=True)
        seed = seeds.get(symbol_id)
        try:
            if seed is not None:
                seed_date, in_right, days, entry_temp = seed
                group = group[group["trade_date"] > seed_date].reset_index(drop=True)
                if group.empty:
                    continue
                state_df = compute_right_side_state(
                    group["temperature"],
                    initial_in_right=in_right,
                    initial_days=days,
                    initial_entry_temp=entry_temp,
                    initial_entry_date=seed_date - timedelta(days=days - 1),
                    calendar_dates=group["trade_date"],
                )
            else:
                # 无种子：全历史冷启动。哪怕只有 1 行也要算（新上市个股首日），
                # 否则该行右侧列永远 NULL，下个交易日才被补齐（历史 bug，harness C5 抓到）。
                state_df = compute_right_side_state(
                    group["temperature"],
                    calendar_dates=group["trade_date"],
                )
            state_df["symbol_id"] = symbol_id
            state_df["trade_date"] = group["trade_date"].values
            results.append(state_df[["symbol_id", "trade_date", "is_right_side", "right_side_days", "right_side_entry_temp"]])
        except Exception as exc:
            logger.warning("计算 %s 右侧状态失败: %s", symbol_id, exc)

    if not results:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "is_right_side", "right_side_days", "right_side_entry_temp"])
    return pd.concat(results, ignore_index=True)


def _get_daily_price_date_range(symbol_ids: List[str]) -> pd.DataFrame:
    """返回每个 symbol 在 daily_price 中的最小/最大日期。"""
    if not symbol_ids:
        return pd.DataFrame(columns=["symbol_id", "min_date", "max_date"])

    chunks = []
    for chunk in _chunked(symbol_ids):
        placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
        params = {f"s{i}": s for i, s in enumerate(chunk)}
        query = f"""
            SELECT symbol_id, MIN(trade_date) AS min_date, MAX(trade_date) AS max_date
            FROM daily_price
            WHERE symbol_id IN ({placeholders})
            GROUP BY symbol_id
        """
        df = pd.read_sql(query, db._engine, params=params)
        chunks.append(df)

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    if not df.empty:
        df["min_date"] = pd.to_datetime(df["min_date"]).dt.date
        df["max_date"] = pd.to_datetime(df["max_date"]).dt.date
    return df


def _get_indicator_max_dates(symbol_ids: List[str]) -> dict:
    """返回每个 symbol 在 daily_indicator 中的最大日期。"""
    if not symbol_ids:
        return {}

    chunks = []
    for chunk in _chunked(symbol_ids):
        placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
        params = {f"s{i}": s for i, s in enumerate(chunk)}
        query = f"""
            SELECT symbol_id, MAX(trade_date) AS max_date
            FROM daily_indicator
            WHERE symbol_id IN ({placeholders})
            GROUP BY symbol_id
        """
        df = pd.read_sql(query, db._engine, params=params)
        chunks.append(df)

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    if df.empty:
        return {}
    df["max_date"] = pd.to_datetime(df["max_date"]).dt.date
    return dict(zip(df["symbol_id"], df["max_date"]))


def _get_max_date(query: str) -> Optional[date]:
    """通用：返回某列最大日期。"""
    df = pd.read_sql(query, db._engine)
    if df.empty or df["max_date"].isna().all():
        return None
    return pd.to_datetime(df["max_date"].iloc[0]).date()


def _get_existing_indicator_for_rs(start_date: date, end_date: date, symbol_ids: Optional[List[str]] = None) -> pd.DataFrame:
    """返回 daily_indicator 在指定日期范围内已有的行（含温度），用于 RS 增量更新。

    RS upsert 必须带上 temperature 才能满足 NOT NULL 约束；这里只更新已有温度记录的行。
    symbol_ids 给定时按品种分块过滤（控制内存）。
    """
    if symbol_ids is None:
        df = pd.read_sql(
            """
            SELECT symbol_id, trade_date, temperature_score, temperature
            FROM daily_indicator
            WHERE trade_date BETWEEN :start_date AND :end_date
            """,
            db._engine,
            params={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
    else:
        if not symbol_ids:
            return pd.DataFrame(columns=["symbol_id", "trade_date", "temperature_score", "temperature"])
        chunks = []
        for chunk in _chunked(symbol_ids):
            placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
            params = {f"s{i}": s for i, s in enumerate(chunk)}
            params["start_date"] = start_date.isoformat()
            params["end_date"] = end_date.isoformat()
            chunks.append(pd.read_sql(
                f"""
                SELECT symbol_id, trade_date, temperature_score, temperature
                FROM daily_indicator
                WHERE trade_date BETWEEN :start_date AND :end_date
                  AND symbol_id IN ({placeholders})
                """,
                db._engine,
                params=params,
            ))
        df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def _read_indicator_temps(symbol_ids: List[str], start_date: date, end_date: date) -> pd.DataFrame:
    """分块读取 daily_indicator 的温度字段（右侧状态机增量用）。"""
    if not symbol_ids:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "temperature_score", "temperature"])
    chunks = []
    for chunk in _chunked(symbol_ids):
        placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
        params = {f"s{i}": s for i, s in enumerate(chunk)}
        params["start_date"] = start_date.isoformat()
        params["end_date"] = end_date.isoformat()
        chunks.append(pd.read_sql(
            f"""
            SELECT symbol_id, trade_date, temperature_score, temperature
            FROM daily_indicator
            WHERE trade_date BETWEEN :start_date AND :end_date
              AND symbol_id IN ({placeholders})
            ORDER BY symbol_id, trade_date
            """,
            db._engine,
            params=params,
        ))
    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def calc_temperature_batch(
    symbol_ids: Optional[List[str]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> pd.DataFrame:
    """为指定品种计算温度（不写入数据库）。"""
    end_date = end_date or get_latest_trade_date()

    if symbol_ids is None:
        df_meta = pd.read_sql("SELECT DISTINCT symbol_id FROM daily_price", db._engine)
        symbol_ids = df_meta["symbol_id"].astype(str).tolist()

    if not symbol_ids:
        return pd.DataFrame(columns=["symbol_id", "trade_date", "temperature_score", "temperature"])

    if start_date is None:
        date_range = _get_daily_price_date_range(symbol_ids)
        if date_range.empty:
            return pd.DataFrame(columns=["symbol_id", "trade_date", "temperature_score", "temperature"])
        start_date = date_range["min_date"].min()

    df = _read_daily_prices(symbol_ids, start_date, end_date)
    return _compute_temperature(df)


def calc_rs_batch(
    symbol_ids: Optional[List[str]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> pd.DataFrame:
    """为指定品种计算 RS（不写入数据库）。"""
    end_date = end_date or get_latest_trade_date()

    if symbol_ids is None:
        df_meta = pd.read_sql("SELECT DISTINCT symbol_id FROM daily_price", db._engine)
        symbol_ids = df_meta["symbol_id"].astype(str).tolist()

    if not symbol_ids:
        return pd.DataFrame(
            columns=[
                "symbol_id",
                "trade_date",
                "weighted_return",
                "rs_score",
                "rs_score_prev_1d",
                "rs_score_prev_5d",
                "rs_score_trend",
            ]
        )

    if start_date is None:
        date_range = _get_daily_price_date_range(symbol_ids)
        if date_range.empty:
            return pd.DataFrame(
                columns=[
                    "symbol_id",
                    "trade_date",
                    "weighted_return",
                    "rs_score",
                    "rs_score_prev_1d",
                    "rs_score_prev_5d",
                    "rs_score_trend",
                ]
            )
        start_date = date_range["min_date"].min()

    df = _read_prices_with_node_type(symbol_ids, start_date, end_date)
    return _compute_rs(df)


def run_indicator_update(
    symbol_ids: Optional[List[str]] = None,
    end_date: Optional[date] = None,
) -> dict:
    """增量更新 daily_indicator 的温度与 RS 字段。"""
    end_date = end_date or get_latest_trade_date()

    if symbol_ids is None:
        df_meta = pd.read_sql("SELECT DISTINCT symbol_id FROM daily_price", db._engine)
        symbol_ids = df_meta["symbol_id"].astype(str).tolist()

    if not symbol_ids:
        logger.info("daily_price 为空，无需计算指标")
        return {"processed": 0, "temp_upserted": 0, "rs_upserted": 0, "skipped": 0}

    price_range = _get_daily_price_date_range(symbol_ids)
    if price_range.empty:
        return {"processed": 0, "temp_upserted": 0, "rs_upserted": 0, "skipped": len(symbol_ids)}

    existing_max = _get_indicator_max_dates(symbol_ids)

    # 价格改写断点：daily_sync 本次/历次同步写入的最早日期（历史被补缺/修订的痕迹）
    revisions = _load_price_revisions()

    # 温度重写窗口起点（每品种）：默认回写最近 _TEMP_REWRITE_DAYS 自然日；
    # 若该品种历史价格被改写且断点更早，则前推到断点之前。
    # 右侧状态机用同一窗口回滚种子（窗口内温度可能被重写，右侧必须跟着重算）。
    cutoff_map = {}
    for sid in symbol_ids:
        em = existing_max.get(sid)
        if em is None:
            continue
        c = em - timedelta(days=_TEMP_REWRITE_DAYS)
        rd = revisions.get(sid)
        if rd is not None and rd <= em:
            c = min(c, rd - timedelta(days=1))
        cutoff_map[sid] = c

    # ---- 温度增量（按品种分块，避免一次性读全表 OOM）----
    price_range = price_range.set_index("symbol_id")
    start_map = {}
    for sid in symbol_ids:
        min_d = price_range.at[sid, "min_date"]
        if sid in existing_max:
            start_map[sid] = min(existing_max[sid] - timedelta(days=_LOOKBACK_BUFFER_DAYS), min_d)
        else:
            start_map[sid] = min_d

    global_start = min(start_map.values())
    temp_upserted = 0
    temp_symbols = set()
    for chunk in _chunked(symbol_ids, _CALC_BATCH_SYMBOLS):
        chunk_start = min(start_map[s] for s in chunk)
        df = _read_daily_prices(chunk, chunk_start, end_date)
        if df.empty:
            continue
        df["_start"] = df["symbol_id"].map(start_map)
        df = df[df["trade_date"] >= df["_start"]].drop(columns=["_start"])

        temp_result = _compute_temperature(df)
        if temp_result.empty:
            continue
        # 写重写窗口内的行（新日期 + 最近窗口回写，吸收历史价格修订）
        co = temp_result["symbol_id"].map(cutoff_map)
        temp_result = temp_result[co.isna() | (temp_result["trade_date"] > co)]
        if temp_result.empty:
            continue
        records = temp_result.to_dict(orient="records")
        with db.get_session() as session:
            for record in records:
                stmt = sqlite_insert(DailyIndicator).values(record)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol_id", "trade_date"],
                    set_={
                        "temperature_score": stmt.excluded.temperature_score,
                        "temperature": stmt.excluded.temperature,
                    },
                )
                session.execute(stmt)
        temp_upserted += len(records)
        temp_symbols.update(temp_result["symbol_id"].unique())

    # ---- RS 增量 ----
    # 首次全量；后续每天只 upsert「RS 已写最大日期的次日」与「最近 _RS_UPSERT_MIN_DAYS 自然日」
    # 中更早的那个起的窗口（即：接续缺口 + 重写近两周，防止边缘修订残留）。
    # 注意：读取窗口要在 upsert 窗口前再前推 _RS_READ_BUFFER_DAYS 自然日，
    # 否则最长 252 交易日的收益率窗口缺数据，reweight 后口径与全量不一致，
    # 会把最近约 178 个交易日的正确 RS 用降级公式覆盖掉（历史 bug）。
    max_daily_price_date = _get_max_date("SELECT MAX(trade_date) AS max_date FROM daily_price") or end_date
    has_rs = pd.read_sql("SELECT 1 FROM daily_indicator WHERE rs_score IS NOT NULL LIMIT 1", db._engine).shape[0] > 0
    if not has_rs:
        rs_start = global_start
        rs_upsert_start = None  # 全量 upsert
    else:
        rs_max_date = _get_max_date(
            "SELECT MAX(trade_date) AS max_date FROM daily_indicator WHERE rs_score IS NOT NULL"
        )
        candidates = [max_daily_price_date - timedelta(days=_RS_UPSERT_MIN_DAYS)]
        if rs_max_date is not None:
            candidates.append(rs_max_date + timedelta(days=1))
        rs_upsert_start = max(min(candidates), global_start)
        rs_start = max(rs_upsert_start - timedelta(days=_RS_READ_BUFFER_DAYS), global_start)
    rs_end = max_daily_price_date

    # 1) 分块计算各品种加权收益率（per-symbol 操作），汇总后全局横截面排名
    returns_chunks = []
    for chunk in _chunked(symbol_ids, _CALC_BATCH_SYMBOLS):
        rs_df = _read_prices_with_node_type(chunk, rs_start, rs_end)
        if rs_df.empty:
            continue
        returns_chunks.append(_compute_weighted_returns(rs_df))
    rs_upserted = 0
    if returns_chunks:
        returns_df = pd.concat(returns_chunks, ignore_index=True)
        rs_result = _rank_rs(returns_df)
        del returns_df, returns_chunks
        if not rs_result.empty and rs_upsert_start is not None:
            # 只写 upsert 窗口内的行；窗口前的数据仅用于提供完整回看历史
            rs_result = rs_result[rs_result["trade_date"] >= rs_upsert_start]

        # 2) 分块 merge 温度（满足 NOT NULL 约束）并 upsert
        if not rs_result.empty:
            for chunk in _chunked(symbol_ids, _CALC_BATCH_SYMBOLS):
                sub = rs_result[rs_result["symbol_id"].isin(chunk)]
                if sub.empty:
                    continue
                existing = _get_existing_indicator_for_rs(rs_start, rs_end, chunk)
                if existing.empty:
                    continue
                sub = sub.merge(
                    existing[["symbol_id", "trade_date", "temperature_score", "temperature"]],
                    on=["symbol_id", "trade_date"],
                    how="inner",
                )
                if sub.empty:
                    continue
                records = sub[
                    [
                        "symbol_id",
                        "trade_date",
                        "temperature_score",
                        "temperature",
                        "rs_score",
                        "rs_score_prev_1d",
                        "rs_score_prev_5d",
                        "rs_score_trend",
                    ]
                ].to_dict(orient="records")
                with db.get_session() as session:
                    for record in records:
                        stmt = sqlite_insert(DailyIndicator).values(record)
                        stmt = stmt.on_conflict_do_update(
                            index_elements=["symbol_id", "trade_date"],
                            set_={
                                "rs_score": stmt.excluded.rs_score,
                                "rs_score_prev_1d": stmt.excluded.rs_score_prev_1d,
                                "rs_score_prev_5d": stmt.excluded.rs_score_prev_5d,
                                "rs_score_trend": stmt.excluded.rs_score_trend,
                            },
                        )
                        session.execute(stmt)
                rs_upserted += len(records)

    # ---- 右侧状态机增量 ----
    # 状态机依赖完整历史，窗口冷启动会把长期右侧品种的天数算错。
    # 这里按品种取"已算过右侧状态的最后一天"作为种子状态，只续算其后的新日期；
    # 从未算过右侧的品种则用全部温度历史冷启动。
    max_di_date = _get_max_date("SELECT MAX(trade_date) AS max_date FROM daily_indicator WHERE temperature IS NOT NULL")
    min_di_date = _get_max_date("SELECT MIN(trade_date) AS max_date FROM daily_indicator WHERE temperature IS NOT NULL")

    right_upserted = 0
    if max_di_date is not None and min_di_date is not None:
        # 种子回滚到温度重写窗口起点之前：窗口内温度被重写，右侧必须重算
        before_dates = {sid: c + timedelta(days=1) for sid, c in cutoff_map.items()}
        seeds = _get_right_side_seeds(before_dates=before_dates)
        for chunk in _chunked(symbol_ids, _CALC_BATCH_SYMBOLS):
            chunk_seeds = {s: seeds[s] for s in chunk if s in seeds}
            starts = [d for d, *_ in chunk_seeds.values()] + [min_di_date]
            right_df = _read_indicator_temps(chunk, min(starts), max_di_date)
            if right_df.empty:
                continue
            right_result = _compute_right_side(right_df, seeds=chunk_seeds)
            if right_result.empty:
                continue
            right_result = right_result.merge(
                right_df[["symbol_id", "trade_date", "temperature_score", "temperature"]],
                on=["symbol_id", "trade_date"],
                how="inner",
            )
            records = right_result[
                [
                    "symbol_id",
                    "trade_date",
                    "temperature_score",
                    "temperature",
                    "is_right_side",
                    "right_side_days",
                    "right_side_entry_temp",
                ]
            ].to_dict(orient="records")
            with db.get_session() as session:
                for record in records:
                    stmt = sqlite_insert(DailyIndicator).values(record)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["symbol_id", "trade_date"],
                        set_={
                            "is_right_side": stmt.excluded.is_right_side,
                            "right_side_days": stmt.excluded.right_side_days,
                            "right_side_entry_temp": stmt.excluded.right_side_entry_temp,
                        },
                    )
                    session.execute(stmt)
            right_upserted += len(records)

    logger.info(
        "指标更新完成: 处理 %s 个品种, 温度 %s 条, RS %s 条, 右侧 %s 条",
        len(symbol_ids),
        temp_upserted,
        rs_upserted,
        right_upserted,
    )
    # 指标已按断点重算，清理已处理的价格改写断点
    if revisions:
        _clear_price_revisions(symbol_ids)
    return {
        "processed": len(symbol_ids),
        "temp_upserted": temp_upserted,
        "rs_upserted": rs_upserted,
        "right_upserted": right_upserted,
        "skipped": len(symbol_ids) - len(temp_symbols),
    }
