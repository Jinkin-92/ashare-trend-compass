#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""温度状态机门禁（全历史）。

约束：
- C1: 相邻转移——同一品种相邻两个交易日的温度档位差 ≤ 1（全历史逐行检查）。
      2026-07-29 事故：补缺改价后只写新日期，1297 个品种在单日跳 2-4 档。
- C2: 沸/冻 3 日缓冲——沸/冻 连续段长度 ≥ 3；长度 < 3 的段只允许出现在
      该品种序列末尾（缓冲刚开始，数据截断）。
- C3: 有温度的行 temperature_score 不为 NULL。

用法：python scripts/harness/check_temperature.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from src.db import get_session

LV = {"冻": 0, "寒": 1, "凉": 2, "平": 3, "温": 4, "热": 5, "沸": 6}

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"PASS  {name}")
    else:
        failures += 1
        print(f"FAIL  {name}  {detail}")


def main():
    with get_session() as s:
        df = pd.read_sql(
            text("SELECT symbol_id, trade_date, temperature, temperature_score FROM daily_indicator WHERE temperature IS NOT NULL"),
            s.bind,
        )
    print(f"载入有温度的行: {len(df):,}")
    df["lv"] = df["temperature"].map(LV)
    df = df.sort_values(["symbol_id", "trade_date"])

    # C1 相邻转移
    prev_lv = df.groupby("symbol_id")["lv"].shift(1)
    jump = (df["lv"] - prev_lv).abs()
    viol = df[jump > 1]
    check(
        "C1 温度相邻转移（全历史，违例=跳变 >1 档）",
        len(viol) == 0,
        f"{len(viol)} 行违例，样例: {viol[['symbol_id','trade_date','temperature']].head(5).values.tolist()}",
    )

    # C2 沸/冻最短持续 3 段
    is_extreme = df["lv"].isin([0, 6])
    grp = (is_extreme != is_extreme.groupby(df["symbol_id"]).shift(1, fill_value=False)).groupby(df["symbol_id"]).cumsum()
    runs = df[is_extreme].groupby([df["symbol_id"], grp[is_extreme]]).agg(
        n=("lv", "size"), last_date=("trade_date", "max"), lv=("lv", "first")
    )
    max_dates = df.groupby("symbol_id")["trade_date"].max()
    runs["is_tail"] = runs["last_date"] == runs.index.get_level_values(0).map(max_dates)
    bad_runs = runs[(runs["n"] < 3) & (~runs["is_tail"])]
    check(
        "C2 沸/冻至少维持 3 个交易日（末段除外）",
        len(bad_runs) == 0,
        f"{len(bad_runs)} 段违例，样例: {bad_runs.head(5).index.tolist()}",
    )

    # C3 score 非空
    null_score = df["temperature_score"].isna().sum()
    check("C3 有温度的行 temperature_score 无 NULL", null_score == 0, f"{null_score} 行")

    print("\n全部通过" if failures == 0 else f"\n{failures} 项约束失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
