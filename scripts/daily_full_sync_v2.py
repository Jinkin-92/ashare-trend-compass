#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日增量 + 历史补齐（tinyshare 并发版）。

并发拉取，每只 stock 只拉缺失段（不重复拉已存在的日线）。

用法：
    python scripts/daily_full_sync_v2.py [--workers 30] [--years 1]
"""
import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

import pandas as pd
import tinyshare as ts
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.db import get_session
from src.models import DailyPrice, Symbol

logger = logging.getLogger('daily_full_sync_v2')


def setup_logging():
    log_path = ROOT / 'data' / 'logs' / 'daily_full_sync_v2.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def get_stock_info():
    """拉所有 stock + 已有 daily_price 范围。"""
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == 'stock')
        ).all()
        stock_ids = [r[0] for r in rows]

    # 过滤只 6 位数字的
    valid = [s for s in stock_ids if s and s.isdigit() and len(s) == 6]

    # 已有 daily_price 范围
    with get_session() as session:
        all_rows = session.execute(
            select(
                DailyPrice.symbol_id,
                DailyPrice.trade_date
            ).where(DailyPrice.symbol_id.in_(valid))
        ).all()
    from collections import defaultdict
    min_d = {}
    max_d = {}
    for sid, d in all_rows:
        if sid not in min_d or d < min_d[sid]:
            min_d[sid] = d
        if sid not in max_d or d > max_d[sid]:
            max_d[sid] = d

    return valid, min_d, max_d


def sid_to_tscode(sid: str) -> str:
    if sid.startswith(('43', '83', '87', '92', '89')):
        return f'{sid}.BJ'
    if sid.startswith(('6', '5', '68', '9', '11')):
        return f'{sid}.SH'
    return f'{sid}.SZ'


def fetch_one_stock(pro, sid: str, start_date: date, end_date: date, retries: int = 5) -> int:
    """拉单只 stock 的日线段，upsert。带限流退避（遇 429 等 30s）。"""
    tscode = sid_to_tscode(sid)
    s_str = start_date.strftime('%Y%m%d')
    e_str = end_date.strftime('%Y%m%d')
    for attempt in range(retries):
        try:
            df = pro.daily(ts_code=tscode, start_date=s_str, end_date=e_str)
            if df is None or df.empty:
                return 0
            df['ts_code'] = df['ts_code'].astype(str)
            df['symbol_id'] = df['ts_code'].str.replace(r'\.(SH|SZ|BJ)$', '', regex=True)
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
            df = df.rename(columns={'vol': 'volume', 'change': 'close_change'})
            df = df[['symbol_id', 'trade_date', 'open', 'high', 'low', 'close',
                     'volume', 'amount', 'pct_chg']]
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            records = df.to_dict(orient='records')
            # upsert
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
            return len(records)
        except Exception as exc:
            err = str(exc)[:100]
            is_rate_limit = '429' in err or '频次' in err
            if attempt == retries - 1:
                logger.warning(f'{sid} FAIL: {err}')
                return 0
            # 限流时退避 30s，否则 2s
            wait = 30 if is_rate_limit else 2
            time.sleep(wait)
    return 0


def main():
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=30)
    parser.add_argument('--years', type=float, default=1.0)
    parser.add_argument('--only-incremental', action='store_true',
                        help='只拉增量（不补齐历史）')
    args = parser.parse_args()

    if not os.environ.get('TINYSHARE_TOKEN'):
        logger.error('TINYSHARE_TOKEN 未设置')
        return 1
    ts.set_token(os.environ['TINYSHARE_TOKEN'])
    pro = ts.pro_api()

    valid, min_d, max_d = get_stock_info()
    logger.info('valid stock: %d', len(valid))

    today = date.today()
    start_year = today - timedelta(days=int(args.years * 365))
    end_dt = today

    # 对每个 stock 算缺口段
    tasks = []
    for sid in valid:
        cur_min = min_d.get(sid)
        cur_max = max_d.get(sid)
        if not args.only_incremental:
            # 补齐历史
            if cur_min is None or cur_min > start_year:
                hist_end = cur_min - timedelta(days=1) if cur_min else end_dt
                if hist_end >= start_year:
                    tasks.append((sid, start_year, hist_end))
        # 增量
        if cur_max is None or cur_max < end_dt:
            inc_start = cur_max + timedelta(days=1) if cur_max else start_year
            if inc_start <= end_dt:
                tasks.append((sid, inc_start, end_dt))

    logger.info('总任务: %d (历史补齐 + 增量)', len(tasks))

    total_rows = 0
    fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one_stock, pro, s, sd, ed): (s, sd, ed) for s, sd, ed in tasks}
        done = 0
        for fut in as_completed(futures):
            sid, sd, ed = futures[fut]
            try:
                n = fut.result()
                total_rows += n
            except Exception as exc:
                fail += 1
            done += 1
            if done % 200 == 0 or done == len(tasks):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(tasks) - done) / rate if rate > 0 else 0
                logger.info(f'  进度 {done}/{len(tasks)}: 写入 {total_rows} 行, 失败 {fail}, 速度 {rate:.1f}/秒, 剩余 {eta:.0f}s')

    elapsed = time.time() - t0
    logger.info('=== 完成: 写入 %d 行, 失败 %d, 耗时 %.1f 秒 ===', total_rows, fail, elapsed)
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)