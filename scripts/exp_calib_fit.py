#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校准实验：温度阈值网格搜索 + RS 窗口候选对比（纯分析，不改生产代码）。

用法：PYTHONIOENCODING=utf-8 python scripts/exp_calib_fit.py
"""
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from src.db import get_session
from src.indicators.relative_strength import weighted_return_series
from src.indicators.temperature import _raw_bucket_idx, classify_temperature

DATES = ["2026-07-21", "2026-07-28"]
LEVELS = ["冻", "寒", "凉", "平", "温", "热", "沸"]
LV = {t: i for i, t in enumerate(LEVELS)}

# ---------- 载入参考样本（带 _sid） ----------
samples = []
for d in DATES:
    m = pd.read_csv(ROOT / "docs" / "calibration" / f"{d}-diff.csv")
    m["trade_date"] = d
    samples.append(m)
samples = pd.concat(samples, ignore_index=True)
samples = samples.dropna(subset=["_sid"])
print(f"样本数: {len(samples)}")

# ---------- 池子信息 ----------
with get_session() as s:
    syms = pd.read_sql(
        text("SELECT symbol_id, name, node_type FROM symbols WHERE node_type IN ('industry_l1','industry_l2')"),
        s.bind,
    )
pool_of = dict(zip(syms["symbol_id"], syms["node_type"]))
samples["pool"] = samples["_sid"].map(pool_of)

# ---------- 实验 1：温度原始分 + 阈值网格搜索 ----------
# diff.csv 里的 temperature_score 就是库中存储的原始分（状态机只作用于 temperature 列）
samples["ref_lv"] = samples["ref_temperature"].map(LV)
samples = samples.dropna(subset=["temperature_score", "ref_lv"])
print(f"有效温度样本: {len(samples)}")

# 基线：当前阈值
def bucket_with(score, th):
    """th = (沸,热,温,平,凉,寒) 下界，平带为 (凉下界, 温下界]"""
    t_boiling, t_hot, t_warm, t_flat_lo, t_cool, t_cold = th
    if score >= t_boiling: return 6
    if score >= t_hot: return 5
    if score >= t_warm: return 4
    if score > t_cool: return 3   # 平: (凉下界, 温下界)
    if score > t_cold: return 2   # 凉
    return 1 if score > -1e18 else 0

def bucket_series(score, th):
    t6, t5, t4, t3, t2, t1 = th  # 沸/热/温/平下界(即凉上界)/凉下界/寒下界
    out = np.zeros(len(score), dtype=int)
    s = score.values
    out[:] = 0
    out[s > t1] = 1
    out[s > t2] = 2
    out[s > t3] = 3
    out[s >= t4] = 4
    out[s >= t5] = 5
    out[s >= t6] = 6
    return out

def eval_th(th):
    pred = bucket_series(samples["temperature_score"], th)
    diff = np.abs(pred - samples["ref_lv"].values)
    exact = (diff == 0).mean()
    adj = (diff <= 1).mean()
    obj = (diff == 0).sum() + 0.5 * (diff == 1).sum()
    return obj, exact, adj

base_th = (50, 25, 5, -5, -25, -50)
obj, exact, adj = eval_th(base_th)
print(f"基线阈值 {base_th}: obj={obj:.1f} exact={exact:.3f} adj={adj:.3f}")

best = (obj, base_th)
# 粗网格：沸/热不动或微调，重点搜 温/平/凉/寒 下界
for t4 in range(3, 16, 2):       # 温下界
    for t3 in range(-20, 5, 3):  # 平下界（凉上界）
        if t3 >= t4: continue
        for t2 in range(-45, -8, 3):  # 凉下界
            if t2 >= t3: continue
            for t1 in range(-70, -25, 5):  # 寒下界
                if t1 >= t2: continue
                th = (50, 25, t4, t3, t2, t1)
                o, e, a = eval_th(th)
                if o > best[0]:
                    best = (o, th, e, a)
print(f"最优阈值 {best[1]}: obj={best[0]:.1f} exact={best[2]:.3f} adj={best[3]:.3f}")

# 在最优邻域细搜 ±1
bt = best[1]
for dt4, dt3, dt2, dt1 in product(range(-2, 3), repeat=4):
    th = (50, 25, bt[2] + dt4, bt[3] + dt3, bt[4] + dt2, bt[5] + dt1)
    if not (th[5] < th[4] < th[3] < th[2]): continue
    o, e, a = eval_th(th)
    if o > best[0]:
        best = (o, th, e, a)
print(f"细搜后最优 {best[1]}: obj={best[0]:.1f} exact={best[2]:.3f} adj={best[3]:.3f}")

# 温度偏差方向（最优阈值下）
pred = bucket_series(samples["temperature_score"], best[1])
samples["pred_lv"] = pred
samples["signed"] = samples["pred_lv"] - samples["ref_lv"]
print("偏差方向分布(本地-参考):", samples["signed"].value_counts().sort_index().to_dict())
print("高温品种核对:", samples[samples["ref_lv"] >= 4][["trade_date", "name", "ref_temperature", "temperature_score", "pred_lv"]].to_string(index=False))

# ---------- 实验 2：RS 窗口候选 ----------
CAND = {
    "a_IBD_0.4/0.2/0.2/0.2": ((63, 126, 189, 252), (0.4, 0.2, 0.2, 0.2)),
    "b_0.7r63+0.3r126": ((63, 126), (0.7, 0.3)),
    "c_pure_r63": ((63,), (1.0,)),
    "d_0.5/0.3/0.2_x252": ((63, 126, 252), (0.5, 0.3, 0.2)),
    "e_0.6/0.2/0.2_x252": ((63, 126, 252), (0.6, 0.2, 0.2)),
    "f_0.4r21/0.3r63/0.2r126/0.1r252": ((21, 63, 126, 252), (0.4, 0.3, 0.2, 0.1)),
}

pools = {}
for nt in ["industry_l1", "industry_l2"]:
    pool_sids = syms[syms["node_type"] == nt]["symbol_id"].tolist()
    frames = []
    for i in range(0, len(pool_sids), 50):
        chunk = pool_sids[i : i + 50]
        ph = ",".join(f":s{j}" for j in range(len(chunk)))
        params = {f"s{j}": s for j, s in enumerate(chunk)}
        frames.append(
            pd.read_sql(
                text(f"SELECT symbol_id, trade_date, close FROM daily_price WHERE symbol_id IN ({ph}) ORDER BY symbol_id, trade_date"),
                __import__("src.db", fromlist=["_engine"])._engine,
                params=params,
            )
        )
    pools[nt] = pd.concat(frames, ignore_index=True)

def pct_scores(pool_df, windows, weights, dates):
    """对池子内全部品种算加权收益率，再按日横截面 1-99 百分位。"""
    rows = []
    for sid, g in pool_df.groupby("symbol_id"):
        g = g.sort_values("trade_date")
        wr = weighted_return_series(g["close"].reset_index(drop=True), windows, weights)
        tmp = pd.DataFrame({"symbol_id": sid, "trade_date": g["trade_date"].values, "wr": wr.values})
        rows.append(tmp[tmp["trade_date"].isin(dates)])
    df = pd.concat(rows, ignore_index=True).dropna(subset=["wr"])
    df["rank"] = df.groupby("trade_date")["wr"].rank(method="min", ascending=False)
    df["n"] = df.groupby("trade_date")["symbol_id"].transform("count")
    df["score"] = ((1 - (df["rank"] - 1) / (df["n"] - 1)) * 98 + 1).round().clip(1, 99)
    return df[["symbol_id", "trade_date", "score"]]

samples["ref_rs"] = samples["ref_rs"].astype(float)
print("\n== RS 候选对比 ==")
results = []
for name, (w, wt) in CAND.items():
    preds = []
    for nt in ["industry_l1", "industry_l2"]:
        preds.append(pct_scores(pools[nt], w, wt, DATES))
    pred = pd.concat(preds, ignore_index=True)
    mm = samples.merge(pred, left_on=["_sid", "trade_date"], right_on=["symbol_id", "trade_date"], how="inner")
    err = (mm["score"] - mm["ref_rs"]).abs()
    spear = mm[["score", "ref_rs"]].corr(method="spearman").iloc[0, 1]
    results.append((name, err.mean(), err.median(), spear, len(mm)))
    print(f"{name:38s} MAE={err.mean():5.1f} med={err.median():5.1f} spearman={spear:+.3f} n={len(mm)}")

# ---------- 实验 3：参考温度 × 参考强度 ----------
print("\n== 参考温度 × 参考强度（两日合并） ==")
print(samples.groupby("ref_temperature")["ref_rs"].describe()[["count", "mean", "min", "max"]].round(1))
