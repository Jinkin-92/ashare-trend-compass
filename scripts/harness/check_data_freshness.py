#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据新鲜度 harness（门禁）：日线与指标必须推进到最近交易日。

约束：
- C1: daily_price 各节点类型（index/industry_l1/industry_l2/concept/stock）的
      最大 trade_date == 最近交易日（XSHG 日历）
- C2: daily_indicator 温度 / RS / 右侧三列的最大 trade_date == 最近交易日
- C3: 导出 JSON（web/data/index-l1.json）的 trade_date == 最近交易日

用法：python scripts/harness/check_data_freshness.py [--json-only]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import sqlite3
from datetime import date, datetime, timedelta

import exchange_calendars as xcals
import pandas as pd

from src.config import LOCAL_DB_PATH, WEB_DATA_DIR

failures = 0


def expected_data_date(now: datetime = None) -> date:
    """数据应推进到的日期：最近一个已收盘的交易日。

    日历上的当日 session 在收盘前（15:30 前）数据尚未发布，期望值回退一日。
    """
    now = now or datetime.now()
    cal = xcals.get_calendar("XSHG")
    today = now.date()
    if cal.is_session(today) and now.hour * 60 + now.minute >= 15 * 60 + 30:
        return today
    prev = cal.date_to_session(today - timedelta(days=1), "previous")
    return pd.Timestamp(prev).date()


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"PASS  {name}")
    else:
        failures += 1
        print(f"FAIL  {name}  {detail}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json-only', action='store_true', help='只查导出 JSON，不查库')
    args = parser.parse_args()

    latest = expected_data_date()
    print(f"数据应推进到（最近已收盘交易日）: {latest}")
    latest_s = latest.isoformat()

    if not args.json_only:
        con = sqlite3.connect(str(LOCAL_DB_PATH))
        rows = con.execute(
            """
            SELECT s.node_type, MAX(dp.trade_date)
            FROM daily_price dp JOIN symbols s ON s.symbol_id = dp.symbol_id
            GROUP BY s.node_type
            """
        ).fetchall()
        for node_type, max_d in sorted(rows):
            check(f"C1 daily_price[{node_type}] 到 {latest_s}", max_d == latest_s, f"实际 {max_d}")

        for label, col in [
            ("温度", "temperature"),
            ("RS", "rs_score"),
            ("右侧", "is_right_side"),
        ]:
            max_d = con.execute(
                f"SELECT MAX(trade_date) FROM daily_indicator WHERE {col} IS NOT NULL"
            ).fetchone()[0]
            check(f"C2 daily_indicator[{label}] 到 {latest_s}", max_d == latest_s, f"实际 {max_d}")
        con.close()

    top = WEB_DATA_DIR / "index-l1.json"
    if top.exists():
        data = json.loads(top.read_text(encoding="utf-8"))
        check(f"C3 index-l1.json 截面日期 {latest_s}", data.get("trade_date") == latest_s,
              f"实际 {data.get('trade_date')}")
    else:
        check("C3 index-l1.json 存在", False, f"未找到 {top}")

    print("\n全部通过" if failures == 0 else f"\n{failures} 项约束失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
