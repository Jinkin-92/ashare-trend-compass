#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 akshare 补 7-13 指数 + 申万行业 + 概念 daily_price（akshare 7-10 已发布）。

用法：python scripts/fill_7_13_akshare.py
"""
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须用代理 7897
os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7897'
os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7897'

import warnings
warnings.filterwarnings('ignore')
import logging
logging.disable(logging.CRITICAL)

import pandas as pd
import akshare as ak
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import select

from src.db import get_session
from src.models import DailyPrice, Symbol

logger = logging.getLogger('fill_7_13_akshare')


def setup_logging():
    log_path = ROOT / 'data' / 'logs' / 'fill_7_13_akshare.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding='utf-8')],
    )


def upsert(records):
    if not records:
        return 0
    n = 0
    with get_session() as session:
        for r in records:
            stmt = sqlite_insert(DailyPrice).values(r)
            stmt = stmt.on_conflict_do_update(
                index_elements=['symbol_id', 'trade_date'],
                set_={
                    'open': stmt.excluded.open, 'high': stmt.excluded.high,
                    'low': stmt.excluded.low, 'close': stmt.excluded.close,
                    'volume': stmt.excluded.volume, 'amount': stmt.excluded.amount,
                    'pct_chg': stmt.excluded.pct_chg,
                },
            )
            session.execute(stmt)
            n += 1
    return n


def pull_indices():
    """7 个宽基指数。"""
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == 'index')
        ).all()
    codes = [r[0].replace('IDX_', '') for r in rows]
    total = 0
    for code in codes:
        try:
            df = ak.stock_zh_index_daily(symbol=f'sh{code}' if code.startswith(('0', '6', '9')) else f'sz{code}')
            if df is None or df.empty:
                continue
            df = df.rename(columns={'date': 'trade_date', '日期': 'trade_date'})
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
            df = df[(df['trade_date'] >= pd.to_datetime('2026-07-13').date()) & (df['trade_date'] <= pd.to_datetime('2026-07-13').date())]
            if df.empty:
                continue
            df['symbol_id'] = f'IDX_{code}'
            df['amount'] = df.get('volume', 0) * df['close']
            df['pct_chg'] = df['close'].pct_change() * 100
            for c in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']:
                if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df[['symbol_id', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']]
            df = df.dropna(subset=['close'])
            n = upsert(df.to_dict(orient='records'))
            total += n
            logger.info(f'  指数 {code} 7-13: {n} 行')
        except Exception as exc:
            logger.warning(f'  指数 {code} FAIL: {exc}')
        time.sleep(0.5)
    logger.info(f'指数 7-13 总行: {total}')


def pull_industries():
    """申万 31 L1 + 123 L2。pct_chg 直接用 daily_price 表 7-10 close 算（避免中文列名依赖）。"""
    from sqlalchemy import text
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type.in_(['industry_l1', 'industry_l2']))
        ).all()
        codes = [r[0].replace('SW_', '') for r in rows]
        # 取 7-10 close 作为基准（前一个交易日）
        prev_rows = session.execute(text(
            "SELECT symbol_id, close FROM daily_price WHERE trade_date='2026-07-10' AND symbol_id LIKE 'SW_%'"
        )).fetchall()
        prev_map = {r[0]: r[1] for r in prev_rows}

    total = 0
    for code in codes:
        try:
            df = ak.index_hist_sw(symbol=code, period='day')
            if df is None or df.empty:
                continue
            df = df.rename(columns={'日期': 'trade_date', '开盘': 'open', '收盘': 'close',
                                    '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount'})
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
            df = df[df['trade_date'] == pd.to_datetime('2026-07-13').date()]
            if df.empty:
                continue
            df['symbol_id'] = f'SW_{code}'
            # pct_chg: 直接用 daily_price 表 7-10 close 计算（避免依赖中文列名）
            prev = prev_map.get(f'SW_{code}')
            cur_close = float(df['close'].iloc[0]) if not df.empty else None
            if prev and cur_close and prev > 0:
                df['pct_chg'] = (cur_close / prev - 1) * 100
            else:
                df['pct_chg'] = None
            for c in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']:
                if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df[['symbol_id', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']]
            df = df.dropna(subset=['close'])
            n = upsert(df.to_dict(orient='records'))
            total += n
            if n: logger.info(f'  行业 {code} 7-13: {n} 行')
        except Exception as exc:
            logger.warning(f'  行业 {code} FAIL: {exc}')
        time.sleep(0.3)
    logger.info(f'行业 7-13 总行: {total}')


def pull_concepts():
    """375 概念（同花顺）。"""
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id, Symbol.name).where(Symbol.node_type == 'concept')
        ).all()
    sid_to_name = {r[0]: r[1] for r in rows}
    total = 0
    fail = 0
    for sid, name in sid_to_name.items():
        if sid == 'CONCEPT_ROOT': continue
        try:
            df = ak.stock_board_concept_index_ths(symbol=name, start_date='20260713', end_date='20260713')
            if df is None or df.empty:
                fail += 1
                continue
            df['trade_date'] = pd.to_datetime(df['日期']).dt.date
            df['symbol_id'] = sid
            df = df.rename(columns={'开盘': 'open', '收盘': 'close', '最高': 'high',
                                    '最低': 'low', '成交量': 'volume', '成交额': 'amount'})
            df['pct_chg'] = pd.to_numeric(df.get('涨跌幅', None), errors='coerce')
            for c in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']:
                if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df[['symbol_id', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']]
            df = df.dropna(subset=['close'])
            n = upsert(df.to_dict(orient='records'))
            total += n
        except Exception as exc:
            fail += 1
        time.sleep(0.05)
    logger.info(f'概念 7-13: 写入 {total} 行, 失败 {fail}')


def main():
    logger.info('=== fill_7_13_akshare ===')
    pull_indices()
    pull_industries()
    pull_concepts()
    logger.info('=== 完成 ===')


if __name__ == '__main__':
    setup_logging()
    sys.exit(main() or 0)
