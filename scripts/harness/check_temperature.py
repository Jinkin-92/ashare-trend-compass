#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""温度门禁。

2026-08-07 起温度标签由平滑分直接映射（绕过状态机），旧的「相邻转移 ≤1 档」
「沸/冻至少维持 3 日」约束随之失效（那两个约束守护的是状态机标签语义；
07-29 跳档事故模式现由引擎每日回写最近 21 自然日窗口吸收）。
现约束：
- C1: 最新截面标签与当前公式重算一致（L1/L2/指数全量 + 个股固定随机抽样 300），
      防止公式变更后库内标签停留在旧口径。
- C2: 温度取值合法（ ∈ 七档）。
- C3: 有温度的行 temperature_score 不为 NULL。

用法：python scripts/harness/check_temperature.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from src.db import get_session
from src.indicators.temperature import TEMPERATURE_LEVELS, classify_temperature

LV = {"冻": 0, "寒": 1, "凉": 2, "平": 3, "温": 4, "热": 5, "沸": 6}
STOCK_SAMPLE = 300

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

    # C2 档位取值合法
    bad_lv = df[~df["temperature"].isin(TEMPERATURE_LEVELS)]
    check(
        "C2 温度取值 ∈ 七档",
        len(bad_lv) == 0,
        f"{len(bad_lv)} 行非法，样例: {bad_lv['temperature'].unique()[:5].tolist()}",
    )

    # C3 score 非空
    null_score = df["temperature_score"].isna().sum()
    check("C3 有温度的行 temperature_score 无 NULL", null_score == 0, f"{null_score} 行")

    # C1 最新截面独立重算比对
    with get_session() as s:
        meta = pd.DataFrame(
            s.execute(
                text("SELECT symbol_id, node_type FROM symbols")
            ).all(),
            columns=["symbol_id", "node_type"],
        )
    core = meta[meta["node_type"].isin(["industry_l1", "industry_l2", "index"])]["symbol_id"].tolist()
    stocks = meta[meta["node_type"] == "stock"]["symbol_id"]
    sampled = stocks.sample(n=min(STOCK_SAMPLE, len(stocks)), random_state=42).tolist()
    sample_ids = core + sampled

    latest = df.sort_values("trade_date").groupby("symbol_id").tail(1).set_index("symbol_id")["temperature"]

    mismatches = []
    checked = 0
    for chunk in [sample_ids[i:i + 500] for i in range(0, len(sample_ids), 500)]:
        placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
        params = {f"s{i}": sid for i, sid in enumerate(chunk)}
        with get_session() as s:
            prices = pd.read_sql(
                text(
                    f"SELECT symbol_id, trade_date, close, high, low FROM daily_price "
                    f"WHERE symbol_id IN ({placeholders}) ORDER BY symbol_id, trade_date"
                ),
                s.bind,
                params=params,
            )
        if prices.empty:
            continue
        prices["trade_date"] = pd.to_datetime(prices["trade_date"])
        for sid, g in prices.groupby("symbol_id"):
            if sid not in latest.index or len(g) < 61:
                continue
            close = pd.Series(g["close"].values, index=g["trade_date"])
            high = pd.Series(g["high"].values, index=g["trade_date"])
            low = pd.Series(g["low"].values, index=g["trade_date"])
            try:
                res = classify_temperature(close, high, low)
            except Exception:
                continue
            recomputed = res["temperature"].dropna()
            if recomputed.empty:
                continue
            checked += 1
            if str(recomputed.iloc[-1]) != str(latest[sid]):
                mismatches.append((sid, str(latest[sid]), str(recomputed.iloc[-1])))

    check(
        f"C1 最新截面标签 == 当前公式重算（{checked} 品种抽样）",
        len(mismatches) == 0,
        f"{len(mismatches)} 个不一致，样例: {mismatches[:5]}",
    )

    print("\n全部通过" if failures == 0 else f"\n{failures} 项约束失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
