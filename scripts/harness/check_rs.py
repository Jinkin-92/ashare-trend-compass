#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RS 强度门禁：用原始收盘价独立重算最新截面 RS，与 daily_indicator 比对。

RS 公式（2026-07-29 校准的快窗口口径）：weighted_return = 0.4*r10 + 0.3*r21 + 0.2*r63 + 0.1*r126
（rN = N 个交易日收益率 %；缺失窗口按可用窗口 reweight），
按 trade_date + node_type 横截面排名映射到 1-99 分位。

约束：
- C1: 最新交易日全部品种 rs_score 与独立重算结果误差 ≤ 1（取整容差）
- C2: rs_score ∈ [1, 99]
- C3: 抽验三孚股份等"温度/RS 背离"案例：打印推导过程供人工核对（INFO，不门禁）

用法：python scripts/harness/check_rs.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import sqlite3

import numpy as np
import pandas as pd

from src.config import LOCAL_DB_PATH

WINDOWS = ((10, 0.4), (21, 0.3), (63, 0.2), (126, 0.1))

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"PASS  {name}")
    else:
        failures += 1
        print(f"FAIL  {name}  {detail}")


def main():
    con = sqlite3.connect(str(LOCAL_DB_PATH))
    max_date = con.execute(
        "SELECT MAX(trade_date) FROM daily_indicator WHERE rs_score IS NOT NULL"
    ).fetchone()[0]
    print(f"最新 RS 截面: {max_date}")

    # 读全品种最近 253+ 交易日收盘价（向量化独立重算）
    df = pd.read_sql(
        """
        SELECT dp.symbol_id, s.node_type, dp.trade_date, dp.close
        FROM daily_price dp JOIN symbols s ON s.symbol_id = dp.symbol_id
        WHERE dp.trade_date >= date(?, '-400 days')
        """,
        con,
        params=(max_date,),
    )
    db_rs = pd.read_sql(
        "SELECT symbol_id, rs_score FROM daily_indicator WHERE trade_date = ? AND rs_score IS NOT NULL",
        con,
        params=(max_date,),
    ).set_index("symbol_id")["rs_score"]
    con.close()

    piv = df.pivot_table(index="trade_date", columns="symbol_id", values="close").sort_index()
    node_type = df.drop_duplicates("symbol_id").set_index("symbol_id")["node_type"]
    last = piv.index[-1]
    check("价格透表最后日期 == 最新 RS 截面", str(last) == max_date, f"{last} vs {max_date}")

    # 生产语义：只有当日有价格的品种才进入当日横截面排名池
    # （概念源被代理掐断导致大面积缺当日数据时，旧口径把陈旧品种也拉进排名池，误报超差）
    df = df.sort_values(["symbol_id", "trade_date"])
    last_date = df.groupby("symbol_id")["trade_date"].last()
    alive = last_date[last_date == max_date].index
    df = df[df["symbol_id"].isin(alive)]

    # 各品种加权收益率：与生产语义一致——按品种自身序列排序后 shift(w)（容忍缺日），
    # 缺失窗口 reweight。用 groupby-shift 向量化实现。
    wr_map = {}
    last_close = df.groupby("symbol_id")["close"].last()
    for w, wt in WINDOWS:
        shifted = df.groupby("symbol_id")["close"].shift(w)
        ret_w = (df["close"] / shifted - 1) * 100
        # 取每个品种最后一行的窗口收益率。
        # 注意必须取「最后一行位置」的值（保留 NaN），不能用 groupby.last()：
        # last() 跳过 NaN，库内概念价格存在 NULL 收盘的占位行，
        # 生产口径靠 reweight 丢弃缺失窗口，last() 会把数周前的陈旧窗口收益率
        # 捡回来，造成假超差。
        is_last_row = ~df["symbol_id"].duplicated(keep="last")
        last_ret = ret_w[is_last_row].set_axis(df.loc[is_last_row, "symbol_id"])
        wr_map[w] = (last_ret, wt)
    wsum = pd.Series(0.0, index=last_close.index)
    denom = pd.Series(0.0, index=last_close.index)
    for w, wt in WINDOWS:
        r, _ = wr_map[w]
        wsum += r.fillna(0) * wt
        denom += r.notna() * wt
    wr = (wsum / denom).where(denom > 0).dropna()

    # 按 node_type 横截面排名 → 1-99（与 rank_rs_by_node_type 同公式的独立实现）
    calc = pd.DataFrame({"wr": wr, "nt": node_type.reindex(wr.index)})
    expected = {}
    for nt, g in calc.groupby("nt"):
        n = len(g)
        if n <= 1:
            expected.update({sid: 50 for sid in g.index})
            continue
        rank = g["wr"].rank(method="min", ascending=False)
        score = ((1 - (rank - 1) / (n - 1)) * 98 + 1).round().clip(1, 99).astype(int)
        expected.update(score.to_dict())
    expected = pd.Series(expected)

    common = expected.index.intersection(db_rs.index)
    diff = (expected.loc[common].astype(int) - db_rs.loc[common].astype(int)).abs()
    bad = diff[diff > 1]
    check(
        f"C1 独立重算 RS 与库一致（{len(common)} 品种，容差±1）",
        len(bad) == 0,
        f"{len(bad)} 个超差，样例: {[(s, int(expected[s]), int(db_rs[s])) for s in bad.index[:5]]}",
    )
    check(
        "C2 rs_score ∈ [1,99]",
        bool((db_rs >= 1).all() and (db_rs <= 99).all()),
        f"范围 [{db_rs.min()}, {db_rs.max()}]",
    )

    # C3: 温度/RS 背离案例推导（INFO）
    con = sqlite3.connect(str(LOCAL_DB_PATH))
    for sid in ["603938"]:
        row = con.execute(
            "SELECT temperature, rs_score FROM daily_indicator WHERE symbol_id=? AND trade_date=?",
            (sid, max_date),
        ).fetchone()
        if row and sid in wr.index:
            closes = piv[sid].dropna()
            parts = []
            for w, wt in WINDOWS:
                if len(closes) > w:
                    r = (closes.iloc[-1] / closes.iloc[-1 - w] - 1) * 100
                    parts.append(f"r{w}={r:+.1f}%×{wt}")
                else:
                    parts.append(f"r{w}=缺失")
            pct = (wr < wr[sid]).mean() * 100
            print(
                f"INFO  {sid} 温度={row[0]} RS={row[1]} | {' '.join(parts)}"
                f" | 加权={wr[sid]:.2f}% 分位={pct:.1f}"
            )
    con.close()

    print("\n全部通过" if failures == 0 else f"\n{failures} 项约束失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
