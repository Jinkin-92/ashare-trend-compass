#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""右侧状态机约束 harness：用「规则规格的独立实现」全量重算 daily_indicator 并比对。

门禁规则（来自 docs/jinkin-philosophy.txt + Jinkin 确认口径）：
- C1: 温度为 平/凉/寒/冻 的行 → is_right_side=0 且 right_side_days=0（左侧无右侧天数）
- C2: is_right_side=1 的行 → 温度 ∈ {温,热,沸} 且 right_side_days ≥ 1
- C3: 入场日（上一交易日不在右侧、当日在右侧）→ 温度 ∈ {热,沸}
- C4: 全量独立重算（状态机规格的独立实现，不 import src.indicators.right_side），
      每个品种每日的 (is_right_side, right_side_days) 必须与库中一致
- C5: 有温度的行右侧三列不得为 NULL（温度/RS 写入不填右侧列是允许的，
      但有温度而右侧列 NULL 说明状态机漏算）

用法：python scripts/harness/check_right_side.py
任一约束不满足 → exit 1。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.config import LOCAL_DB_PATH

import sqlite3

ENTER_TEMPS = {"热", "沸"}
MAINTAIN_TEMPS = {"温", "热", "沸"}
DOWN_TEMPS = {"平", "凉", "寒", "冻"}

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"PASS  {name}")
    else:
        failures += 1
        print(f"FAIL  {name}  {detail}")


def spec_recompute(temps):
    """规则规格的独立实现（刻意不复用 right_side.py，避免循环论证）。"""
    in_right = False
    days = 0
    out = []
    for t in temps:
        if t is None or pd.isna(t):
            out.append((in_right, days))  # 温度缺失：保持状态不累计
            continue
        if in_right:
            if t in MAINTAIN_TEMPS:
                days += 1
            else:  # 平及以下 → 退出清零
                in_right, days = False, 0
        else:
            if t in ENTER_TEMPS:
                in_right, days = True, 1
        out.append((in_right, days))
    return out


def main():
    con = sqlite3.connect(str(LOCAL_DB_PATH))
    df = pd.read_sql(
        "SELECT symbol_id, trade_date, temperature, is_right_side, right_side_days "
        "FROM daily_indicator WHERE temperature IS NOT NULL",
        con,
    )
    con.close()
    print(f"载入有温度的行: {len(df):,}")
    if df.empty:
        check("daily_indicator 有数据", False, "表为空")
        return 1

    # C5: NULL 检查
    null_rows = df[df["is_right_side"].isna() | df["right_side_days"].isna()]
    check(
        "C5 有温度的行右侧列无 NULL",
        len(null_rows) == 0,
        f"{len(null_rows)} 行违规: {null_rows[['symbol_id', 'trade_date', 'temperature']].head(5).to_dict('records')}",
    )
    df = df.dropna(subset=["is_right_side", "right_side_days"]).copy()

    df["is_right_side"] = df["is_right_side"].astype(int)
    df["right_side_days"] = df["right_side_days"].astype(int)

    # C1: 平/凉/寒/冻 → 无右侧
    down = df[df["temperature"].isin(DOWN_TEMPS)]
    bad_c1 = down[(down["is_right_side"] != 0) | (down["right_side_days"] != 0)]
    check(
        f"C1 平/凉/寒/冻 无右侧天数（{len(down):,} 行）",
        len(bad_c1) == 0,
        f"{len(bad_c1)} 行违规，样例: {bad_c1.head(3).to_dict('records')}",
    )

    # C2: 右侧中 → 温度 温/热/沸 且天数 ≥1
    up = df[df["is_right_side"] == 1]
    bad_c2 = up[~up["temperature"].isin(MAINTAIN_TEMPS) | (up["right_side_days"] < 1)]
    check(
        f"C2 右侧中温度≥温且天数≥1（{len(up):,} 行）",
        len(bad_c2) == 0,
        f"{len(bad_c2)} 行违规，样例: {bad_c2.head(3).to_dict('records')}",
    )

    # C3 + C4: 逐品种独立重算比对
    df = df.sort_values(["symbol_id", "trade_date"])
    mismatch = 0
    entry_violations = 0
    mismatch_samples = []
    entry_samples = []
    for sid, g in df.groupby("symbol_id", sort=False):
        temps = g["temperature"].tolist()
        expected = spec_recompute(temps)
        actual_in = g["is_right_side"].tolist()
        actual_days = g["right_side_days"].tolist()
        prev_in = False
        for i, (t, (exp_in, exp_days)) in enumerate(zip(temps, expected)):
            if exp_in != bool(actual_in[i]) or exp_days != actual_days[i]:
                mismatch += 1
                if len(mismatch_samples) < 5:
                    mismatch_samples.append(
                        f"{sid} {g['trade_date'].iloc[i]} 温度={t} 库=({actual_in[i]},{actual_days[i]}) 期望=({int(exp_in)},{exp_days})"
                    )
            # C3: 入场日温度必须是 热/沸
            if not prev_in and actual_in[i] == 1 and t not in ENTER_TEMPS:
                entry_violations += 1
                if len(entry_samples) < 5:
                    entry_samples.append(f"{sid} {g['trade_date'].iloc[i]} 温度={t}")
            prev_in = bool(actual_in[i])

    check(
        "C4 全量独立重算与库一致",
        mismatch == 0,
        f"{mismatch} 处不一致，样例: {mismatch_samples}",
    )
    check(
        "C3 入场日温度 ∈ {热,沸}",
        entry_violations == 0,
        f"{entry_violations} 处违规，样例: {entry_samples}",
    )

    print("\n全部通过" if failures == 0 else f"\n{failures} 项约束失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
