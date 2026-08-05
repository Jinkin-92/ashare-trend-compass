#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补 7-13 指数 + 申万行业 + 概念 的 daily_price（用 tinyshare）。

stock 5,530 只已用 fill_stock_gap_tinyshare.py 补完。
指数/行业/概念没补，exporter 显示 close=None。

数据源：tinyshare pro.index_daily(ts_code='list', start_date, end_date)
- 7 个指数 + 31 L1 + 123 L2 + 375 概念 = 536 个 ts_code
- 单次最多 5000 行，预计 536 × 12 = 6,432 行 = 1 次 API

用法：python scripts/fill_index_l2_concept_7_13.py
"""
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

import pandas as pd
import tinyshare as ts
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.db import get_session
from src.models import DailyPrice, Symbol

logger = logging.getLogger(__name__)


def setup_logging():
    log_path = ROOT / 'data' / 'logs' / 'fill_index_l2_concept.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def get_all_target_symbols() -> list:
    """取指数 / L1 / L2 / concept 共 536 个 ts_code 形式。"""
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id).where(
                Symbol.node_type.in_(['index', 'industry_l1', 'industry_l2', 'concept'])
            )
        ).all()
    ts_codes = []
    for r in rows:
        sid = r[0]
        if sid.startswith('IDX_'):
            code = sid.replace('IDX_', '')
            if code.startswith(('0', '3', '9')):
                ts_codes.append(f'{code}.SH')
            else:
                ts_codes.append(f'{code}.SZ')
        elif sid.startswith('SW_'):
            ts_codes.append(f'{sid.replace("SW_", "")}.SI')
        elif sid.startswith('CONCEPT_'):
            # 概念: tinyshare 用同花顺代码，需要从 name 拿（之前 fill_stock_gap_tinyshare 用 ts_code 表）
            # 但概念是 ths 接口，tinyshare 的 index_daily 不支持
            continue
    return ts_codes


def upsert_daily(records: list) -> int:
    if not records:
        return 0
    n = 0
    with get_session() as session:
        for r in records:
            stmt = sqlite_insert(DailyPrice).values(r)
            stmt = stmt.on_conflict_do_update(
                index_elements=['symbol_id', 'trade_date'],
                set_={
                    'open': stmt.excluded.open,
                    'high': stmt.excluded.high,
                    'low': stmt.excluded.low,
                    'close': stmt.excluded.close,
                    'volume': stmt.excluded.volume,
                    'amount': stmt.excluded.amount,
                    'pct_chg': stmt.excluded.pct_chg,
                },
            )
            session.execute(stmt)
            n += 1
    return n


def main():
    logger.info('=== fill_index_l2_concept_7_13 ===')
    if not os.environ.get('TINYSHARE_TOKEN'):
        logger.error('TINYSHARE_TOKEN 未设置')
        return 1
    ts.set_token(os.environ['TINYSHARE_TOKEN'])
    pro = ts.pro_api()

    ts_codes = get_all_target_symbols()
    logger.info('目标 ts_code: %d（指数+L1+L2，不含概念）', len(ts_codes))

    # 拉 7-13 一天
    start_date = '20260713'
    end_date = '20260713'

    BATCH = 80
    total_rows = 0
    import time
    t_start = time.time()
    for i in range(0, len(ts_codes), BATCH):
        batch = ts_codes[i:i + BATCH]
        ts_str = ','.join(batch)
        logger.info(f'  batch {i // BATCH + 1}/{(len(ts_codes) + BATCH - 1) // BATCH}: {len(batch)}')
        try:
            df = pro.index_daily(ts_code=ts_str, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                logger.info(f'    batch {i // BATCH + 1} 无数据')
                continue
            df['ts_code'] = df['ts_code'].astype(str)
            df['symbol_id'] = df['ts_code'].apply(lambda x: (
                f'IDX_{x.replace(".SH", "").replace(".SZ", "")}' if '.SH' in x or '.SZ' in x
                else f'SW_{x.replace(".SI", "")}'
            ))
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
            df = df.rename(columns={'vol': 'volume', 'change': 'close_change'})
            df = df[['symbol_id', 'trade_date', 'open', 'high', 'low', 'close',
                     'volume', 'amount', 'pct_chg']]
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            records = df.to_dict(orient='records')
            n = upsert_daily(records)
            total_rows += n
            logger.info(f'    batch {i // BATCH + 1} 写入 {n} 行')
        except Exception as exc:
            logger.warning(f'    batch {i // BATCH + 1} FAIL: {exc}')

    elapsed = time.time() - t_start
    logger.info('=== 完成：写入 %d 行，耗时 %.1f 秒 ===', total_rows, elapsed)
    return 0


if __name__ == '__main__':
    setup_logging()
    from sqlalchemy import select
    sys.exit(main())
