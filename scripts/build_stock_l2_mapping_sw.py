#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 akshare index_component_sw 重建 申万 L2 → 个股 挂载关系。

背景：tinyshare index_member_all 接口一次只返回 ~3000 行，2544 只个股未覆盖，
导致多数 L2 页面成分股不全。akshare 的 index_component_sw 按单个 L2 指数返回
完整成分股列表，131 个 L2 各调一次即可全覆盖。

写入：symbols.parent_id / symbols.l2_industry_id（仅 stock 节点）。
配套门禁：scripts/harness/check_l2_coverage.py

用法：
    python scripts/build_stock_l2_mapping_sw.py            # 全量重建
    python scripts/build_stock_l2_mapping_sw.py --dry-run  # 只拉取比对，不写库
"""
import argparse
import logging
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update

from src.classification import NODE_TYPE_INDUSTRY_L2, NODE_TYPE_STOCK
from src.db import get_session
from src.models import Symbol

logger = logging.getLogger(__name__)

RETRIES = 3


def setup_logging():
    log_path = ROOT / 'data' / 'logs' / 'build_stock_l2_mapping_sw.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def fetch_l2_constituents(l2_code: str) -> list:
    """拉单个 L2 指数的成分股代码列表（6 位裸代码）。"""
    import akshare as ak

    last_exc = None
    for attempt in range(RETRIES):
        try:
            df = ak.index_component_sw(symbol=l2_code)
            if df is None or df.empty:
                return []
            # 列：序号, 证券代码, 证券名称, 最新权重, 计入日期（按名取，失败按位置取第 2 列）
            if '证券代码' in df.columns:
                codes = df['证券代码']
            else:
                codes = df.iloc[:, 1]
            return [str(c).strip().split('.')[0].zfill(6) for c in codes.tolist()]
        except Exception as exc:
            last_exc = exc
            logger.warning('L2 %s 第 %s/%s 次拉取失败: %s', l2_code, attempt + 1, RETRIES, exc)
            time.sleep(2 + attempt * 2)
    logger.error('L2 %s 拉取最终失败: %s', l2_code, last_exc)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='只拉取比对，不写库')
    args = parser.parse_args()

    logger.info('=== build_stock_l2_mapping_sw (akshare index_component_sw) ===')

    with get_session() as session:
        l2_rows = session.execute(
            select(Symbol.symbol_id, Symbol.name).where(Symbol.node_type == NODE_TYPE_INDUSTRY_L2)
        ).all()
        stock_ids = {r[0] for r in session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == NODE_TYPE_STOCK)
        ).all()}
    logger.info('symbols 表: l2=%d, stock=%d', len(l2_rows), len(stock_ids))

    # 逐 L2 拉成分股（限速，礼貌请求）
    stock_to_l2 = {}
    conflicts = 0
    failed_l2 = []
    for i, (l2_id, l2_name) in enumerate(l2_rows, start=1):
        l2_code = l2_id.replace('SW_', '', 1)
        codes = fetch_l2_constituents(l2_code)
        if codes is None:
            failed_l2.append(l2_id)
            continue
        for c in codes:
            if c in stock_to_l2 and stock_to_l2[c] != l2_id:
                conflicts += 1
            stock_to_l2[c] = l2_id
        if i % 20 == 0 or i == len(l2_rows):
            logger.info('进度 %s/%s（最新 %s %s: %d 只）', i, len(l2_rows), l2_id, l2_name, len(codes))
        time.sleep(random.uniform(0.6, 1.2))

    if failed_l2:
        logger.error('%d 个 L2 拉取失败: %s', len(failed_l2), failed_l2)
        return 1
    if conflicts:
        logger.warning('%d 只个股出现在多个 L2（以最后覆盖为准）', conflicts)

    in_db = {c: l2 for c, l2 in stock_to_l2.items() if c in stock_ids}
    not_in_db = len(stock_to_l2) - len(in_db)
    uncovered = stock_ids - set(in_db.keys())
    logger.info(
        '源侧映射 %d 只；命中本表 %d 只；源有而本表无 %d 只；本表个股未覆盖 %d 只',
        len(stock_to_l2), len(in_db), not_in_db, len(uncovered),
    )
    if uncovered:
        logger.info('未覆盖样例: %s', sorted(uncovered)[:20])

    if args.dry_run:
        logger.info('dry-run，不写库')
        return 0

    with get_session() as session:
        for stock_id, l2_id in in_db.items():
            session.execute(
                update(Symbol)
                .where(Symbol.symbol_id == stock_id)
                .values(parent_id=l2_id, l2_industry_id=l2_id)
            )
    logger.info('已写入 %d 只个股的 parent_id / l2_industry_id', len(in_db))
    logger.info('=== 完成 ===')
    return 0


if __name__ == '__main__':
    setup_logging()
    sys.exit(main())
