#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全市场 A 股 stock 1 年日线一次性补齐（baostock 串行，可断点续拉）。

设计目标：
- 慢但稳：baostock 串行 1.5-3 秒/只，避免 akshare eastmoney 代理断流
- 进度可恢复：progress.json 记录每个 symbol 的最新拉取日，中断后继续
- 失败隔离：单只失败不阻塞整批
- 已有数据不覆盖：起点 = max(existing_max, requested_start)

用法：
    python scripts/backfill_stocks.py
    python scripts/backfill_stocks.py --resume        # 从 progress.json 恢复
    python scripts/backfill_stocks.py --max-stocks 100 # 限流测试
    python scripts/backfill_stocks.py --years 1        # 拉 1 年
"""
import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import select, func

from src import db
from src.config import ensure_dirs
from src.data_source import AkShareFetcher
from src.db import get_session
from src.models import DailyPrice, Symbol

logger = logging.getLogger(__name__)

PROGRESS_PATH = ROOT / 'data' / 'backfill_progress.json'


def setup_logging():
    ensure_dirs()
    log_path = ROOT / 'data' / 'logs' / 'backfill_stocks.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding='utf-8'),
        ],
    )


def load_progress() -> dict:
    """加载断点续拉进度。"""
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning('progress.json 解析失败: %s，忽略', e)
    return {
        'completed': {},   # {symbol_id: max_trade_date_iso}
        'failed': {},      # {symbol_id: error_count}
        'started_at': None,
        'updated_at': None,
    }


def save_progress(progress: dict) -> None:
    """落盘 progress.json（原子写：先写 .tmp 再 rename）。"""
    progress['updated_at'] = date.today().isoformat()
    tmp = PROGRESS_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(PROGRESS_PATH)


def get_existing_max(symbol_ids: list) -> dict:
    """查询每个 symbol 已有 daily_price 的最大日期。"""
    with get_session() as session:
        rows = session.execute(
            select(DailyPrice.symbol_id, func.max(DailyPrice.trade_date))
            .where(DailyPrice.symbol_id.in_(symbol_ids))
            .group_by(DailyPrice.symbol_id)
        ).all()
    return {sid: d for sid, d in rows}


def get_stock_symbols() -> list:
    """从 symbols 表取所有 stock 节点，按 symbol_id 排序。"""
    with get_session() as session:
        rows = session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == 'stock')
        ).all()
    return [r[0] for r in rows]


def upsert_daily(df) -> int:
    """写入 daily_price（on_conflict_do_update 保留已有数据）。"""
    if df is None or df.empty:
        return 0
    df = df.copy()
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'adj_factor']:
        if col not in df.columns:
            df[col] = None
    df = df[['symbol_id', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'adj_factor']]
    df = df.dropna(subset=['symbol_id', 'trade_date', 'close'])
    records = df.to_dict(orient='records')
    if not records:
        return 0
    with get_session() as session:
        for record in records:
            stmt = sqlite_insert(DailyPrice).values(record)
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
                    'adj_factor': stmt.excluded.adj_factor,
                },
            )
            session.execute(stmt)
    return len(records)


def run(args):
    fetcher = AkShareFetcher()
    start_default = date.today() - timedelta(days=int(args.years * 365))
    end_date = date.today()
    logger.info('拉取区间: %s ~ %s (%.1f 年)', start_default, end_date, args.years)

    # 取所有 stock
    all_symbols = get_stock_symbols()
    if args.max_stocks:
        all_symbols = all_symbols[:args.max_stocks]
    logger.info('本次目标: %d 只 stock', len(all_symbols))

    # 加载断点
    progress = load_progress() if args.resume else {
        'completed': {}, 'failed': {}, 'started_at': None, 'updated_at': None,
    }
    if not progress.get('started_at'):
        progress['started_at'] = time.strftime('%Y-%m-%d %H:%M:%S')

    # 已有数据基线
    existing_max = get_existing_max(all_symbols)
    logger.info('已有日线: %d 只', len(existing_max))

    # 跳过已"完成到目标 end_date"的；剩余按"未拉过的优先"排序
    needs_full = []   # 完全没有日线
    needs_inc = []    # 有日线但不到 end_date
    done = []
    for sid in all_symbols:
        last = existing_max.get(sid)
        if not last:
            needs_full.append(sid)
        elif last >= end_date:
            done.append(sid)
        else:
            needs_inc.append(sid)
    todo = needs_full + needs_inc
    logger.info(
        '待拉: %d 只（从未拉过 %d, 仅需增量 %d, 已最新 %d）',
        len(todo), len(needs_full), len(needs_inc), len(done),
    )
    # max-stocks 限流：从前 N 个里取（优先未拉过的）
    if args.max_stocks:
        todo = todo[:args.max_stocks]
        logger.info('限流: 只拉前 %d 只', args.max_stocks)

    # 串行拉取
    total_rows = 0
    failed_list = []
    t_start = time.time()
    for i, sid in enumerate(todo, start=1):
        # 起点：max(existing_max+1, start_default)
        last = existing_max.get(sid)
        if last:
            s = last + timedelta(days=1)
        else:
            s = start_default
        if s > end_date:
            continue

        # 限速（baostock 内部已有，外部额外 0.05s）
        time.sleep(0.05)
        try:
            df = fetcher.get_stock_daily_baostock(sid, s, end_date)
            if df.empty:
                # fallback: sina
                df = fetcher.get_stock_daily_sina(sid, s, end_date)
                if not df.empty:
                    df['symbol_id'] = sid
            n = upsert_daily(df)
            total_rows += n
            new_max = df['trade_date'].max() if not df.empty else s
            progress['completed'][sid] = new_max.isoformat() if hasattr(new_max, 'isoformat') else str(new_max)
            progress['failed'].pop(sid, None)
        except Exception as exc:
            progress['failed'][sid] = progress['failed'].get(sid, 0) + 1
            if progress['failed'][sid] >= 3:
                failed_list.append((sid, str(exc)[:80]))
                logger.warning('股票 %s 失败 ≥3 次: %s', sid, exc)
            else:
                logger.debug('股票 %s 失败 (第 %d 次): %s', sid, progress['failed'][sid], exc)

        # 进度日志 + 落盘
        if i % 100 == 0 or i == len(todo):
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(todo) - i) / rate if rate > 0 else 0
            logger.info(
                '进度 %d/%d (%.1f%%) 累计 %d 行  速度 %.1f 只/秒  预计剩余 %.0f 分钟  失败 %d',
                i, len(todo), 100 * i / len(todo), total_rows, rate, eta / 60, len(failed_list),
            )
            save_progress(progress)

    save_progress(progress)
    elapsed = time.time() - t_start
    logger.info('=== 拉取完成 ===')
    logger.info('总耗时: %.1f 分钟', elapsed / 60)
    logger.info('总行数: %d', total_rows)
    logger.info('失败: %d 只 (累计失败 ≥3 次)', len(failed_list))
    if failed_list:
        logger.info('失败列表: %s', failed_list[:20])


def parse_args():
    p = argparse.ArgumentParser(description='全市场 A 股 stock 1 年日线补齐')
    p.add_argument('--years', type=float, default=1.0, help='拉取年数（默认 1）')
    p.add_argument('--max-stocks', type=int, default=None, help='限流测试用')
    p.add_argument('--resume', action='store_true', help='从 progress.json 恢复')
    return p.parse_args()


if __name__ == '__main__':
    setup_logging()
    run(parse_args())
