#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 tinyshare 批量拉全市场 stock 近期日线，填补 daily_price 缺口。

适用：5,331 只 stock 中 1,524 只最后日是 2026-06-30，缺 7-01~7-10 增量。

数据源：tinyshare pro.daily(ts_code='list,of,ts_code', start_date, end_date)
- 单次最多 5000 行（多 ts_code 共享）
- 预计 1,524 × 8 天 = 12,192 行 = 3 次 API 调用，2-3 分钟完成

用法：python scripts/fill_stock_gap_tinyshare.py
"""
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

import tinyshare as ts
from sqlalchemy import select, tuple_

from src.db import get_session
from src.models import DailyPrice, Symbol

logger = logging.getLogger(__name__)


def setup_logging():
    log_path = ROOT / 'data' / 'logs' / 'fill_stock_gap_tinyshare.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def get_gap_stocks() -> list:
    """找 daily_price 最后日早于 7-10 的 stock（含 6-30 那批）。"""
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id)
            .where(Symbol.node_type == 'stock')
        ).all()
        stock_ids = [r[0] for r in rows]
    # 6 位 code 配 .SH/.SZ/.BJ 后缀给 tinyshare
    ts_codes = []
    for sid in stock_ids:
        if not sid or not sid.isdigit() or len(sid) != 6:
            continue
        if sid.startswith(('43', '83', '87', '92', '89')):
            ts_codes.append(f'{sid}.BJ')
        elif sid.startswith(('6', '5', '68', '9', '11')):
            ts_codes.append(f'{sid}.SH')
        else:
            ts_codes.append(f'{sid}.SZ')
    return stock_ids, ts_codes


def upsert_daily(records: list) -> int:
    if not records:
        return 0
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    logger.info('=== fill_stock_gap_tinyshare ===')
    if not os.environ.get('TINYSHARE_TOKEN'):
        logger.error('TINYSHARE_TOKEN 未设置')
        return 1

    ts.set_token(os.environ['TINYSHARE_TOKEN'])
    pro = ts.pro_api()

    stock_ids, ts_codes = get_gap_stocks()
    logger.info('将拉 %d 只 stock (ts_code 形式)', len(ts_codes))

    # 拉最新一天日线（7-13 收盘后）
    start_date = date.today().strftime('%Y%m%d')
    end_date = date.today().strftime('%Y%m%d')

    BATCH = 80  # 每次 80 只 ts_code（80 * 12 = 960 行，安全）
    total_rows = 0
    t_start = time.time()
    for i in range(0, len(ts_codes), BATCH):
        batch = ts_codes[i:i + BATCH]
        batch_codes = stock_ids[i:i + BATCH]
        ts_str = ','.join(batch)
        logger.info(f'  batch {i // BATCH + 1}/{(len(ts_codes) + BATCH - 1) // BATCH}: {len(batch)} 只')
        try:
            df = pro.daily(ts_code=ts_str, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                logger.info(f'    batch {i // BATCH + 1} 无数据')
                continue
            # 转换：ts_code -> symbol_id
            ts_to_sid = {f'{sid}.SH': sid for sid in stock_ids}
            ts_to_sid.update({f'{sid}.SZ': sid for sid in stock_ids})
            ts_to_sid.update({f'{sid}.BJ': sid for sid in stock_ids})
            df['symbol_id'] = df['ts_code'].map(ts_to_sid)
            df = df.dropna(subset=['symbol_id'])
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
    import pandas as pd
    sys.exit(main())
