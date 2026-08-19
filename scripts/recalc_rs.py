#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RS 口径切换后的回灌：按当前 relative_strength 默认窗口重算最近 N 自然日的
RS 并重写 daily_indicator（rs_score / rs_score_prev_1d / rs_score_prev_5d / rs_score_trend）。

横截面排名必须在全品种汇总后统一进行（与 engine 生产语义一致），
因此先分块算加权收益率、合并后全局排名，再按块 upsert。

用法：
    python scripts/recalc_rs.py [--days 60]
"""
import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src import db
from src.indicators.engine import (
    _CALC_BATCH_SYMBOLS,
    _RS_READ_BUFFER_DAYS,
    _chunked,
    _compute_weighted_returns,
    _get_existing_indicator_for_rs,
    _get_max_date,
    _rank_rs,
    _read_prices_with_node_type,
)
from src.models import DailyIndicator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("recalc_rs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="回灌最近 N 自然日（默认 60）")
    args = ap.parse_args()

    rs_end = _get_max_date("SELECT MAX(trade_date) AS max_date FROM daily_price")
    rs_upsert_start = rs_end - timedelta(days=args.days)
    rs_start = rs_upsert_start - timedelta(days=_RS_READ_BUFFER_DAYS)
    logger.info("回灌窗口: %s ~ %s（读取起点 %s）", rs_upsert_start, rs_end, rs_start)

    symbol_ids = pd.read_sql("SELECT DISTINCT symbol_id FROM daily_price", db._engine)["symbol_id"].astype(str).tolist()
    logger.info("品种数: %d", len(symbol_ids))

    # 1) 分块计算加权收益率，汇总后全局横截面排名
    chunks = []
    for chunk in _chunked(symbol_ids, _CALC_BATCH_SYMBOLS):
        rs_df = _read_prices_with_node_type(chunk, rs_start, rs_end)
        if rs_df.empty:
            continue
        chunks.append(_compute_weighted_returns(rs_df))
    if not chunks:
        logger.warning("无价格数据，退出")
        return
    returns_df = pd.concat(chunks, ignore_index=True)
    del chunks
    rs_result = _rank_rs(returns_df)
    del returns_df
    rs_result = rs_result[rs_result["trade_date"] >= rs_upsert_start]
    logger.info("待回灌行数: %d", len(rs_result))

    # 2) 分块 merge 温度（满足 NOT NULL 约束）并 upsert
    total = 0
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
                "symbol_id", "trade_date", "temperature_score", "temperature",
                "rs_score", "rs_score_prev_1d", "rs_score_prev_5d", "rs_score_trend",
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
        total += len(records)
        logger.info("进度: 已回灌 %d 行", total)

    logger.info("完成! 共回灌 %d 行（窗口 %s ~ %s）", total, rs_upsert_start, rs_end)


if __name__ == "__main__":
    main()
