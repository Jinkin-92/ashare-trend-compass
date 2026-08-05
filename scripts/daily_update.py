#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日盘后自动更新：日线同步 + 指标重算 + 静态导出。

设计目标：
- 跑得快：增量只拉最近 1 个交易日
- 自动重试：单只 stock / 单个概念失败不阻塞
- 一键完成：从 raw data 到前端可访问的 JSON 全流程

典型用法（Windows Task Scheduler / cron）：
    # 每个交易日 16:30 跑（盘后 30 分钟）
    python scripts/daily_update.py

    # 也可手动跑（指定日期，默认今天）
    python scripts/daily_update.py
    python scripts/daily_update.py --date 2026-07-11
"""
import argparse
import logging
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.pipeline import TrendPipeline


def setup_logging():
    config.ensure_dirs()
    log_path = ROOT / 'data' / 'logs' / 'daily_update.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def run(args):
    """每日更新全流程。"""
    logger = logging.getLogger('daily_update')
    t_start = time.time()

    # 1. 同步品种树（轻量，5-10 秒）
    logger.info('===== 阶段 1/3：同步品种分类树 =====')
    pipeline = TrendPipeline()
    pipeline.init_schema()
    n = pipeline.sync_symbols()
    logger.info('品种树同步完成: %s 条', n)

    # 2. 同步日线（增量 1 天）
    logger.info('===== 阶段 2/3：同步日线（增量） =====')
    counts = pipeline.sync_daily_prices(incremental_days=1)
    logger.info('日线同步: %s', counts)

    # 3. 指标重算 + 静态导出
    logger.info('===== 阶段 3/3：指标重算 + 静态导出 =====')
    if not args.skip_indicators:
        pipeline.calculate_indicators()
        logger.info('指标重算完成')
    else:
        logger.info('跳过指标重算（--skip-indicators）')

    logger.info('===== 导出静态 JSON =====')
    cmd = [sys.executable, str(ROOT / 'scripts' / 'export_static.py')]
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        logger.error('export_static.py 退出码 %d', rc)
        return rc

    elapsed = time.time() - t_start
    logger.info('===== 每日更新完成：%.1f 秒 =====', elapsed)
    return 0


def parse_args():
    p = argparse.ArgumentParser(description='每日盘后数据更新')
    p.add_argument('--date', type=str, default=None, help='目标交易日（YYYY-MM-DD），默认今天')
    p.add_argument('--skip-indicators', action='store_true', help='跳过指标重算（仅同步日线）')
    return p.parse_args()


if __name__ == '__main__':
    setup_logging()
    sys.exit(run(parse_args()))
