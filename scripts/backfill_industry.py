#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补齐 162 个 SW 行业 + 指数 品种的日线与指标。

用途：
- daily_price 缺失的 8 个 L2 行业：拉近 1 年日线
- daily_indicator 最新两日只算了 10 个品种：跑指标补算
- 跑完后 daily_indicator 至少 162 个品种在 7-06 / 7-07 有数据

不在本脚本职责范围：全市场 5529 只个股（耗时太长，单独处理）。
"""

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402
from src.classification import (  # noqa: E402
    NODE_TYPE_INDEX,
    NODE_TYPE_INDUSTRY_L1,
    NODE_TYPE_INDUSTRY_L2,
)
from src.data_source import AkShareFetcher  # noqa: E402
from src.daily_sync import DailyPriceSync  # noqa: E402
from src.indicators.engine import run_indicator_update  # noqa: E402
from src.models import DailyPrice, Symbol  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

logger = logging.getLogger(__name__)


def find_missing_l2() -> list:
    """查 131 个 L2 中日线全为空的 8 个。"""
    with db.get_session() as session:
        rows = (
            session.execute(
                select(Symbol.symbol_id, Symbol.name)
                .where(Symbol.node_type == NODE_TYPE_INDUSTRY_L2)
            )
            .all()
        )
        all_l2 = [(r[0], r[1]) for r in rows]
        have = {
            r[0]
            for r in session.execute(
                select(DailyPrice.symbol_id)
                .where(DailyPrice.symbol_id.in_([s for s, _ in all_l2]))
                .group_by(DailyPrice.symbol_id)
            ).all()
        }
    return [(s, n) for s, n in all_l2 if s not in have]


def backfill_missing_l2(fetcher: AkShareFetcher, days: int = 365) -> int:
    """拉缺失 L2 行业的近 days 天日线。"""
    from src.daily_sync import strip_symbol_prefix

    missing = find_missing_l2()
    if not missing:
        logger.info("L2 行业日线已全部覆盖")
        return 0

    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    total = 0
    for i, (symbol_id, name) in enumerate(missing, start=1):
        raw_code, _ = strip_symbol_prefix(symbol_id)
        try:
            df = fetcher.get_industry_index_daily(raw_code, start_date, end_date)
            if not df.empty:
                df["symbol_id"] = symbol_id
                sync = DailyPriceSync(fetcher)
                n = sync._upsert_daily(df)
                total += n
                logger.info("L2 补齐 %s %s: %s 行", symbol_id, name, n)
            else:
                logger.warning("L2 %s %s: 拉取为空", symbol_id, name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("L2 %s %s 拉取失败: %s", symbol_id, name, exc)
        time.sleep(0.5)
    return total


def backfill_index_and_industry_prices(fetcher: AkShareFetcher, days: int = 400) -> int:
    """对所有 index / industry_l1 / industry_l2 跑增量日线（仅 7 指数 + 162 SW = 169 个）。"""
    with db.get_session() as session:
        rows = (
            session.execute(
                select(Symbol.symbol_id, Symbol.node_type)
                .where(
                    Symbol.node_type.in_(
                        [NODE_TYPE_INDEX, NODE_TYPE_INDUSTRY_L1, NODE_TYPE_INDUSTRY_L2]
                    )
                )
            ).all()
        )
        entries = [(r[0], r[1]) for r in rows]
    sync = DailyPriceSync(fetcher)
    end_date = date.today()
    counts = sync.run(symbol_entries=entries, end_date=end_date)
    total = sum(counts.values())
    logger.info("指数+行业 日线同步: %s", counts)
    return total


def backfill_indicators() -> dict:
    """对全量有日线的品种重算指标（不限制 symbol_ids）。"""
    return run_indicator_update()


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐 SW 行业+指数 数据")
    parser.add_argument("--skip-indicators", action="store_true", help="只补日线，不重算指标")
    args = parser.parse_args()

    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("===== 补齐 162 SW+指数 数据 =====")

    fetcher = AkShareFetcher(sleep_min=0.5, sleep_max=1.0)

    t0 = time.time()
    n1 = backfill_missing_l2(fetcher)
    logger.info("缺失 L2 补齐: %s 行, 累计耗时 %.1fs", n1, time.time() - t0)

    t1 = time.time()
    n2 = backfill_index_and_industry_prices(fetcher)
    logger.info("全量指数+行业 增量: %s 行, 累计耗时 %.1fs", n2, time.time() - t1)

    if not args.skip_indicators:
        t2 = time.time()
        result = backfill_indicators()
        logger.info("指标补算结果: %s, 累计耗时 %.1fs", result, time.time() - t2)

    logger.info("===== 完成 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
