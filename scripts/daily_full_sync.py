#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日增量 + 历史补齐（对每个 stock 拉缺失的日线段）。

对每个 stock：
- 起始 = max(daily_price 最末日 + 1, 1 年前 earliest_needed)
- 终止 = today

历史补齐：现有数据 < 1 年的，把缺失部分补到 1 年。
增量：拉当前日期 new 数据。

用法：
    python scripts/daily_full_sync.py          # stock 5,533
    python scripts/daily_full_sync.py --days 7 # 限制 7 天增量（不补齐）
"""
import argparse
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

import pandas as pd
import tinyshare as ts
from sqlalchemy import select

from src.db import get_session
from src.models import DailyPrice, Symbol

logger = logging.getLogger('daily_full_sync')


def setup_logging():
    log_path = ROOT / 'data' / 'logs' / 'daily_full_sync.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def get_existing_max(symbol_ids: list) -> dict:
    """查询每个 stock daily_price 的最大日期。"""
    with get_session() as session:
        rows = session.execute(
            select(DailyPrice.symbol_id, DailyPrice.trade_date)
            .where(DailyPrice.symbol_id.in_(symbol_ids))
            .order_by(DailyPrice.symbol_id, DailyPrice.trade_date.desc())
        ).all()
    # 每个 symbol 只保留 max
    from collections import defaultdict
    out = {}
    for sid, d in rows:
        if sid not in out or d > out[sid]:
            out[sid] = d
    return out


def get_existing_min(symbol_ids: list) -> dict:
    """查询每个 stock daily_price 的最小日期。"""
    with get_session() as session:
        rows = session.execute(
            select(DailyPrice.symbol_id, DailyPrice.trade_date)
            .where(DailyPrice.symbol_id.in_(symbol_ids))
            .order_by(DailyPrice.symbol_id, DailyPrice.trade_date.asc())
        ).all()
    out = {}
    for sid, d in rows:
        if sid not in out or d < out[sid]:
            out[sid] = d
    return out


def upsert(records: list) -> int:
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
                    'open': stmt.excluded.open, 'high': stmt.excluded.high,
                    'low': stmt.excluded.low, 'close': stmt.excluded.close,
                    'volume': stmt.excluded.volume, 'amount': stmt.excluded.amount,
                    'pct_chg': stmt.excluded.pct_chg,
                },
            )
            session.execute(stmt)
            n += 1
    return n


def to_ts_codes(symbol_ids: list) -> tuple:
    """6 位 code -> tinyshare ts_code。返回 (ts_codes, sid_to_tscode)。"""
    ts_codes = []
    sid_to_tscode = {}
    for sid in symbol_ids:
        if not sid or not sid.isdigit() or len(sid) != 6:
            continue
        if sid.startswith(('43', '83', '87', '92', '89')):
            tscode = f'{sid}.BJ'
        elif sid.startswith(('6', '5', '68', '9', '11')):
            tscode = f'{sid}.SH'
        else:
            tscode = f'{sid}.SZ'
        ts_codes.append(tscode)
        sid_to_tscode[sid] = tscode
    return ts_codes, sid_to_tscode


def fetch_batch(pro, ts_codes, start_date, end_date) -> int:
    """单次拉一批 stock 的 start_date~end_date 日线。"""
    if not ts_codes:
        return 0
    BATCH = 80
    total = 0
    for i in range(0, len(ts_codes), BATCH):
        batch = ts_codes[i:i + BATCH]
        ts_str = ','.join(batch)
        try:
            df = pro.daily(ts_code=ts_str, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                continue
            df['ts_code'] = df['ts_code'].astype(str)
            # ts_code -> symbol_id (去掉 .SH/.SZ/.BJ 后缀)
            df['symbol_id'] = df['ts_code'].str.replace(r'\.(SH|SZ|BJ)$', '', regex=True)
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
            df = df.rename(columns={'vol': 'volume', 'change': 'close_change'})
            df = df[['symbol_id', 'trade_date', 'open', 'high', 'low', 'close',
                     'volume', 'amount', 'pct_chg']]
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            records = df.to_dict(orient='records')
            n = upsert(records)
            total += n
        except Exception as exc:
            logger.warning(f'batch FAIL: {str(exc)[:100]}')
    return total


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description='每日增量 + 历史补齐')
    parser.add_argument('--days', type=int, default=None,
                        help='只拉近 N 天增量（不补齐历史）')
    parser.add_argument('--fill-years', type=float, default=1.0,
                        help='补齐到 N 年（默认 1 年）')
    args = parser.parse_args()

    if not os.environ.get('TINYSHARE_TOKEN'):
        logger.error('TINYSHARE_TOKEN 未设置')
        return 1
    ts.set_token(os.environ['TINYSHARE_TOKEN'])
    pro = ts.pro_api()

    # 拉所有 stock
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == 'stock')
        ).all()
        all_symbols = [r[0] for r in rows]
    logger.info('总 stock: %d', len(all_symbols))

    ts_codes, sid_to_tscode = to_ts_codes(all_symbols)
    skipped = [sid for sid in all_symbols if sid not in sid_to_tscode]
    if skipped:
        logger.info('跳过非 6 位 stock: %d (%s...)', len(skipped), skipped[:5])
    logger.info('有效 stock: %d', len(ts_codes))

    # 已有日期范围
    existing_min = get_existing_min(all_symbols)
    existing_max = get_existing_max(all_symbols)
    logger.info('已有 daily_price: %d symbol', len(existing_max))

    today = date.today()
    start_year = today - timedelta(days=int(args.fill_years * 365))
    # 增量终点 = 今天
    end_date = today.strftime('%Y%m%d')

    # 对每个 stock 算"缺什么段"，合并成 (start, end) 区间
    from collections import defaultdict
    gaps = defaultdict(list)  # symbol_id -> list of (start, end)
    for sid in all_symbols:
        cur_min = existing_min.get(sid)  # date or None
        cur_max = existing_max.get(sid)
        # 补齐历史：start_year ~ cur_min - 1（如果有）
        if args.days is None:
            if cur_min is None or cur_min > start_year:
                gaps[sid].append((start_year, cur_min - timedelta(days=1) if cur_min else today))
        # 增量：cur_max + 1 ~ today
        if cur_max is None or cur_max < today:
            inc_start = cur_max + timedelta(days=1) if cur_max else start_year
            gaps[sid].append((inc_start, today))

    # 统计
    total_gaps = sum(len(g) for g in gaps.values())
    total_rows = 0
    logger.info('待补: %d symbol, %d 段区间', len(gaps), total_gaps)

    # 按段起点分组拉（同一段内多只 stock 可批拉）
    # 简化：每个 gap 都拉一次
    t0 = time.time()
    for i, (sid, gap_list) in enumerate(gaps.items(), start=1):
        ts_code = sid_to_tscode.get(sid)
        if not ts_code:
            continue
        for (s, e) in gap_list:
            s_str = s.strftime('%Y%m%d')
            e_str = e.strftime('%Y%m%d')
            n = fetch_batch(pro, [ts_code], s_str, e_str)
            total_rows += n
        if i % 200 == 0 or i == len(gaps):
            elapsed = time.time() - t0
            logger.info(f'  进度 {i}/{len(gaps)}: 写入 {total_rows} 行, 耗时 {elapsed:.1f}s')

    elapsed = time.time() - t0
    logger.info('=== 完成: 写入 %d 行, 耗时 %.1f 分钟 ===', total_rows, elapsed / 60)
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)