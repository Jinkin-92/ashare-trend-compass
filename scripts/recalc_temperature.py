#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""温度公式变更后的全品种回灌：按当前 temperature 公式重算最近 N 自然日
并重写 daily_indicator（temperature_score / temperature）。

注意：本脚本只回写温度两列。右侧状态机依赖温度历史，窗口内的右侧状态
由下一次 daily_update 的增量机制（21 自然日重写窗口 + 种子回滚）自愈。

用法：
    python scripts/recalc_temperature.py [--days 60]
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
    _chunked,
    _compute_temperature,
    _get_max_date,
    _read_daily_prices,
)
from src.models import DailyIndicator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("recalc_temperature")

# 读取窗口在回灌窗口前再前推的自然日数：
# 需要覆盖 250 个交易日滚动窗口 + EMA 平滑暖机（span ≤ 12），700 自然日足够
_READ_BUFFER_DAYS = 700


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="回灌最近 N 自然日（默认 60）")
    args = ap.parse_args()

    end_date = _get_max_date("SELECT MAX(trade_date) AS max_date FROM daily_price")
    cutoff = end_date - timedelta(days=args.days)
    read_start = cutoff - timedelta(days=_READ_BUFFER_DAYS)
    logger.info("回灌窗口: %s ~ %s（读取起点 %s）", cutoff, end_date, read_start)

    symbol_ids = pd.read_sql("SELECT DISTINCT symbol_id FROM daily_price", db._engine)["symbol_id"].astype(str).tolist()
    logger.info("品种数: %d", len(symbol_ids))

    total = 0
    chunks = list(_chunked(symbol_ids, _CALC_BATCH_SYMBOLS))
    for ci, chunk in enumerate(chunks, 1):
        logger.info("chunk %d/%d 开始（%d 品种）", ci, len(chunks), len(chunk))
        df = _read_daily_prices(chunk, read_start, end_date)
        if df.empty:
            continue
        temp_result = _compute_temperature(df)
        if temp_result.empty:
            continue
        temp_result = temp_result[temp_result["trade_date"] > cutoff]
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
        total += len(records)
        logger.info("进度: 已回灌 %d 行", total)

    logger.info("完成! 共回灌 %d 行（窗口 %s ~ %s）", total, cutoff, end_date)


if __name__ == "__main__":
    main()
