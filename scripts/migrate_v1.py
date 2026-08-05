#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据库结构迁移：加 l2_industry_id / adj_factor / data_status 列。

使用方式：python scripts/migrate_v1.py
幂等：已加的列不会重复加。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / 'data' / 'trend_compass.db'


def has_column(con, table, col):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def add_column_if_missing(con, table, col, decl):
    if has_column(con, table, col):
        print(f'  [SKIP] {table}.{col} 已存在')
        return False
    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    print(f'  [ADD]  {table}.{col} {decl}')
    return True


def main():
    if not DB_PATH.exists():
        print(f'数据库不存在: {DB_PATH}')
        return 1
    con = sqlite3.connect(str(DB_PATH))
    print(f'=== migrate_v1 on {DB_PATH} ===')

    added = 0
    added += add_column_if_missing(con, 'symbols', 'l2_industry_id', 'VARCHAR(32)')
    added += add_column_if_missing(con, 'symbols', 'data_status', 'VARCHAR(16)')

    added += add_column_if_missing(con, 'daily_price', 'adj_factor', 'FLOAT')

    con.commit()

    # 补索引
    print('--- 索引 ---')
    cur_idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if 'ix_symbol_l2' not in cur_idx:
        con.execute('CREATE INDEX ix_symbol_l2 ON symbols (l2_industry_id)')
        print('  [ADD]  ix_symbol_l2')
        added += 1
    else:
        print('  [SKIP] ix_symbol_l2 已存在')

    con.commit()
    con.close()
    print(f'\n迁移完成，新增 {added} 个对象')
    return 0


if __name__ == '__main__':
    sys.exit(main())
