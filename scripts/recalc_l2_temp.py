#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""强制重算所有 L2 行业的温度（用于公式变更后的全量覆写）。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
import pandas as pd
from datetime import date
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src import db
from src.models import DailyIndicator
from src.indicators.temperature import classify_temperature

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('recalc_l2')

BATCH_SIZE = 30

def main():
    # 1. 获取所有 L2 行业
    with db.get_session() as s:
        rows = s.execute(text(
            "SELECT symbol_id, name FROM symbols WHERE node_type='industry_l2'"
        )).fetchall()
    l2_ids = [r[0] for r in rows]
    logger.info("L2 行业数量: %d", len(l2_ids))

    # 2. 获取最新交易日期
    with db.get_session() as s:
        latest = s.execute(text(
            "SELECT MAX(trade_date) FROM daily_price"
        )).scalar()
    logger.info("最新交易日: %s", latest)

    total_upserted = 0
    total_sids = 0

    for i in range(0, len(l2_ids), BATCH_SIZE):
        batch = l2_ids[i:i + BATCH_SIZE]
        placeholders = ','.join(['?'] * len(batch))

        df = pd.read_sql_query(
            f"SELECT symbol_id, trade_date, open, high, low, close "
            f"FROM daily_price WHERE symbol_id IN ({placeholders}) "
            f"ORDER BY symbol_id, trade_date",
            db._engine, params=tuple(batch)
        )
        if df.empty:
            continue

        # Convert trade_date to datetime
        df['trade_date'] = pd.to_datetime(df['trade_date'])

        for sid in batch:
            sub = df[df['symbol_id'] == sid].copy()
            if sub.empty or len(sub) < 60:
                continue
            sub = sub.sort_values('trade_date')

            try:
                close = pd.Series(sub['close'].values, index=sub['trade_date'])
                high = pd.Series(sub['high'].values, index=sub['trade_date'])
                low = pd.Series(sub['low'].values, index=sub['trade_date'])
                result = classify_temperature(close, high, low)
            except Exception as e:
                logger.warning("%s 温度计算失败: %s", sid, e)
                continue

            last_n = result.dropna(subset=['temperature']).tail(60)
            if last_n.empty:
                continue

            records = []
            for idx, row in last_n.iterrows():
                tdate = idx.date() if hasattr(idx, 'date') else pd.Timestamp(idx).date()
                temp_val = row['temperature']
                if pd.isna(temp_val) or temp_val is None:
                    continue
                score = row['temperature_score']
                records.append({
                    'symbol_id': str(sid),
                    'trade_date': tdate,
                    'temperature': str(temp_val),
                    'temperature_score': float(score) if not pd.isna(score) else None,
                })

            if records:
                with db.get_session() as s:
                    for rec in records:
                        stmt = sqlite_insert(DailyIndicator).values(rec)
                        stmt = stmt.on_conflict_do_update(
                            index_elements=['symbol_id', 'trade_date'],
                            set_={
                                'temperature_score': stmt.excluded.temperature_score,
                                'temperature': stmt.excluded.temperature,
                            },
                        )
                        s.execute(stmt)
                total_upserted += len(records)
                total_sids += 1

        logger.info("进度: %d/%d (已更新 %d 行业, %d 行)",
                    min(i + BATCH_SIZE, len(l2_ids)), len(l2_ids), total_sids, total_upserted)

    logger.info("完成! 共更新 %d 行业, %d 行", total_sids, total_upserted)

    # 4. 打印温度分布
    with db.get_session() as s:
        rows = s.execute(text("""
            SELECT di.temperature, COUNT(*) as cnt
            FROM daily_indicator di
            JOIN symbols sym ON di.symbol_id = sym.symbol_id
            WHERE sym.node_type = 'industry_l2'
              AND di.trade_date = :dt
            GROUP BY di.temperature
            ORDER BY cnt DESC
        """), {'dt': latest}).fetchall()
    logger.info("===== L2 行业温度分布 (%s) =====", latest)
    total = 0
    for r in rows:
        logger.info("  %s: %d", r[0], r[1])
        total += r[1]
    logger.info("  合计: %d", total)


if __name__ == '__main__':
    main()
