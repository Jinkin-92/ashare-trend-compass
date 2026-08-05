#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通过 tinyshare 拉全市场 stock → 申万 L1/L2/L3 编码映射，写入 symbols 表。

数据源：tinyshare index_member_all（一次调用返回全市场 stock 行业映射，~3000 行）
映射规则：把 l1_code/l2_code 去掉 .SI 后缀作为新 SW_xxx id 匹配 symbols.symbol_id

用法：
    python scripts/build_stock_l2_mapping.py
"""
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

import tinyshare as ts
from sqlalchemy import select, update

from src.db import get_session
from src.models import Symbol

logger = logging.getLogger(__name__)


def setup_logging():
    log_path = ROOT / 'data' / 'logs' / 'build_stock_l2_mapping.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def strip_si(code: str) -> str:
    """'801010.SI' -> 'SW_801010'。"""
    if not code:
        return None
    code = code.replace('.SI', '').replace('.si', '')
    return f'SW_{code}'


def main():
    logger.info('=== build_stock_l2_mapping ===')
    if not os.environ.get('TINYSHARE_TOKEN'):
        logger.error('TINYSHARE_TOKEN 未设置（.env 缺失？）')
        return 1

    ts.set_token(os.environ['TINYSHARE_TOKEN'])
    pro = ts.pro_api()

    # 用任意 SW 指数 code 调 index_member_all（接口特性：返回全市场映射）
    logger.info('拉取全市场 stock → L1/L2/L3 映射...')
    df = pro.index_member_all(index_code='801010.SI')  # 用 801010 触发，参数不影响结果
    if df is None or df.empty:
        logger.error('tinyshare 返回空，请检查 token / 积分')
        return 1
    logger.info('拉取到 %d 行 stock 行业映射', len(df))
    logger.info('  unique l1=%d, l2=%d, ts_code=%d',
                df['l1_code'].nunique(), df['l2_code'].nunique(), df['ts_code'].nunique())

    # 转 ts_code 000001.SZ -> 000001
    df['ts_code'] = df['ts_code'].str.replace(r'\.(SZ|SH|BJ)$', '', regex=True)
    df['l2_sw_id'] = df['l2_code'].apply(strip_si)
    df['l1_sw_id'] = df['l1_code'].apply(strip_si)

    # 加载 symbols 表（找 stock 和 l2 的 symbol_id）
    with get_session() as session:
        stock_rows = session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == 'stock')
        ).all()
        stock_ids = {r[0] for r in stock_rows}
        l2_rows = session.execute(
            select(Symbol.symbol_id, Symbol.parent_id).where(Symbol.node_type == 'industry_l2')
        ).all()
        # l2_id -> l1_id
        l2_to_l1 = {r[0]: r[1] for r in l2_rows}
        # l1_id 集合（用于校验）
        l1_ids = {r[0] for r in session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == 'industry_l1')
        ).all()}
    logger.info('symbols 表: stock=%d, l2=%d, l1=%d', len(stock_ids), len(l2_to_l1), len(l1_ids))

    # 统计匹配情况
    df_in_db = df[df['ts_code'].isin(stock_ids)]
    logger.info('tinyshare 拉到的 stock 在本表: %d / %d', len(df_in_db), len(stock_ids))

    # l2 也对齐：检查 tinyshare 的 l2_sw_id 是否在 symbols 表
    l2_in_db = df_in_db[df_in_db['l2_sw_id'].isin(l2_to_l1.keys())]
    logger.info('L2 行业匹配 symbols: %d 行', len(l2_in_db))

    # 对每只 stock，验证 tinyshare 报的 l2.parent_id == tinyshare 报的 l1_sw_id
    mismatch = 0
    for _, r in l2_in_db.iterrows():
        l2_id = r['l2_sw_id']
        expected_l1 = l2_to_l1.get(l2_id)
        if expected_l1 != r['l1_sw_id']:
            mismatch += 1
    if mismatch:
        logger.warning('L2 → L1 不一致: %d 行（tinyshare 与 symbols 表定义冲突）', mismatch)
        logger.warning('将以 symbols.l2.parent_id 为准')

    # 更新 symbols.l2_industry_id + parent_id
    # 策略：每只 stock 用 tinyshare 报的 l2_sw_id
    update_pairs = []
    for _, r in df_in_db.iterrows():
        l2_id = r['l2_sw_id']
        if l2_id in l2_to_l1:
            update_pairs.append((r['ts_code'], l2_id))

    logger.info('将更新 %d 只 stock 的 parent_id / l2_industry_id', len(update_pairs))

    with get_session() as session:
        for stock_id, l2_id in update_pairs:
            session.execute(
                update(Symbol)
                .where(Symbol.symbol_id == stock_id)
                .values(parent_id=l2_id, l2_industry_id=l2_id)
            )
    logger.info('=== 完成 ===')
    return 0


if __name__ == '__main__':
    setup_logging()
    sys.exit(main())
