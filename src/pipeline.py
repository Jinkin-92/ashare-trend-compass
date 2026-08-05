# -*- coding: utf-8 -*-
"""主计算管道。"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src import config
from src.classification import NODE_TYPE_STOCK, ClassificationBuilder
from src.daily_sync import DailyPriceSync
from src.data_source import AkShareFetcher, DSAReader
from src.db import get_session, init_db
from src.indicators.engine import run_indicator_update
from src.models import Symbol

logger = logging.getLogger(__name__)


def setup_logging(log_dir: Optional[Path] = None) -> None:
    """配置日志。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config.ensure_dirs()
    log_path = (log_dir or config.LOG_DIR) / "pipeline.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


class TrendPipeline:
    """趋势罗盘每日批处理管道。"""

    def __init__(self):
        self.fetcher = AkShareFetcher()
        self.dsa_reader = DSAReader()
        self.classification = ClassificationBuilder(self.fetcher)
        self.daily_sync = DailyPriceSync(self.fetcher, self.dsa_reader)

    def init_schema(self) -> None:
        """初始化数据库表。"""
        init_db()

    def sync_symbols(self) -> int:
        """同步品种分类树到本地数据库。"""
        df = self.classification.build()
        if df.empty:
            logger.warning("未获取到任何品种信息")
            return 0

        records = df.to_dict(orient="records")
        with get_session() as session:
            for record in records:
                stmt = sqlite_insert(Symbol).values(record)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol_id"],
                    set_={
                        "name": stmt.excluded.name,
                        "node_type": stmt.excluded.node_type,
                        "parent_id": stmt.excluded.parent_id,
                        "is_leaf": stmt.excluded.is_leaf,
                        "market_cap_float": stmt.excluded.market_cap_float,
                    },
                )
                session.execute(stmt)
        # 个股 parent_id 以 L2 挂载（build_stock_l2_mapping_sw 写入的 l2_industry_id）为准：
        # 分类树对个股只能给到 L1/IND_UNKNOWN，上面的 upsert 每次都会冲掉 L2 挂载，
        # 这里立即调和回来，保证 L2 页面成分股不丢。
        result = session.execute(
            update(Symbol)
            .where(Symbol.node_type == NODE_TYPE_STOCK)
            .where(Symbol.l2_industry_id.like("SW_%"))
            .where(Symbol.parent_id != Symbol.l2_industry_id)
            .values(parent_id=Symbol.l2_industry_id)
        )
        if result.rowcount:
            logger.info("调和个股 parent_id ← l2_industry_id: %s 条", result.rowcount)
        logger.info("同步品种树完成: %s 条", len(records))
        return len(records)

    def sync_daily_prices(
        self,
        max_stocks: Optional[int] = None,
        incremental_days: Optional[int] = None,
    ) -> dict:
        """同步日线行情。"""
        return self.daily_sync.run(
            max_stocks=max_stocks,
            incremental_days=incremental_days,
        )

    def calculate_indicators(self) -> dict:
        """计算并写入温度/RS/右侧状态等指标。"""
        logger.info("开始计算指标（温度/RS/右侧状态）...")
        result = run_indicator_update()
        logger.info("指标计算完成: %s", result)
        return result

    def run(self, max_stocks: Optional[int] = None, skip_indicators: bool = False) -> None:
        """运行完整管道。"""
        logger.info("===== 趋势罗盘管道启动 =====")
        self.sync_symbols()
        counts = self.sync_daily_prices(max_stocks=max_stocks)
        logger.info("日线同步统计: %s", counts)
        if not skip_indicators:
            self.calculate_indicators()
        logger.info("===== 趋势罗盘管道结束 =====")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A-Share Trend Compass 数据管道")
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="仅初始化数据库表，不运行完整管道",
    )
    parser.add_argument(
        "--sync-symbols",
        action="store_true",
        help="仅同步品种分类树",
    )
    parser.add_argument(
        "--max-stocks",
        type=int,
        default=None,
        help="限制本次同步的个股数量（用于测试）",
    )
    parser.add_argument(
        "--skip-daily",
        action="store_true",
        help="跳过日线同步",
    )
    parser.add_argument(
        "--skip-symbols",
        action="store_true",
        help="跳过品种树同步（仅日线）",
    )
    parser.add_argument(
        "--skip-indicators",
        action="store_true",
        help="同步日线后不计算温度指标",
    )
    parser.add_argument(
        "--calc-only",
        action="store_true",
        help="仅重新计算指标，不执行品种/日线同步",
    )
    parser.add_argument(
        "--incremental-days",
        type=int,
        default=None,
        help="个股日线只补近 N 天（默认按数据库已有基线拉 2 年）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()

    pipeline = TrendPipeline()

    if args.init_only:
        pipeline.init_schema()
        logger.info("数据库初始化完成")
        return 0

    if args.sync_symbols:
        pipeline.init_schema()
        pipeline.sync_symbols()
        return 0

    if args.calc_only:
        pipeline.init_schema()
        pipeline.calculate_indicators()
        return 0

    pipeline.init_schema()
    if not args.skip_symbols:
        pipeline.sync_symbols()
    if not args.skip_daily:
        pipeline.sync_daily_prices(max_stocks=args.max_stocks, incremental_days=args.incremental_days)
    if not args.skip_indicators:
        pipeline.calculate_indicators()
    return 0


if __name__ == "__main__":
    sys.exit(main())
