#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校准实验第二轮：连续方向分 + 更快 RS 候选。

用法：PYTHONIOENCODING=utf-8 python scripts/exp_calib_fit2.py
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
from src.indicators.temperature import calculate_atr, calculate_ma, weighted_roc

DATES = ["2026-07-21", "2026-07-28"]
LEVELS = ["冻", "寒", "凉", "平", "温", "热", "沸"]
LV = {t: i for i, t in enumerate(LEVELS)}

samples = []
for d in DATES:
    m = pd.read_csv(ROOT / "docs" / "calibration" / f"{d}-diff.csv")
    m["trade_date"] = d
    samples.append(m)
samples = pd.concat(samples, ignore_index=True).dropna(subset=["_sid"])
samples["trade_date"] = pd.to_datetime(samples["trade_date"]).dt.date
samples["ref_lv"] = samples["ref_temperature"].map(LV)
samples["ref_rs"] = samples["ref_rs"].astype(float)

with get_session() as s:
    syms = pd.read_sql(
        text("SELECT symbol_id, name, node_type FROM symbols WHERE node_type IN ('industry_l1','industry_l2')"),
        s.bind,
    )

engine = __import__("src.db", fromlist=["_engine"])._engine


def load_prices(sids):
    frames = []
    for i in range(0, len(sids), 50):
        chunk = sids[i : i + 50]
        ph = ",".join(f":s{j}" for j in range(len(chunk)))
        params = {f"s{j}": s for j, s in enumerate(chunk)}
        frames.append(
            pd.read_sql(
                text(f"SELECT symbol_id, trade_date, open, high, low, close FROM daily_price WHERE symbol_id IN ({ph}) ORDER BY symbol_id, trade_date"),
                engine,
                params=params,
            )
        )
    df = pd.concat(frames, ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


# ---------- 实验 1b：连续方向分 ----------
sids = sorted(samples["_sid"].unique())
prices = load_prices(sids)


def smooth_score_series(g, k_price=0.05, k_arr=0.03, dir_w=25.0, roc_w=0.7):
    """连续方向分版本：dir = clip(close/ma60-1, ±k_price)/k_price*0.7 + clip(ma20/ma60-1, ±k_arr)/k_arr*0.3"""
    g = g.sort_values("trade_date").reset_index(drop=True)
    close, high, low = g["close"], g["high"], g["low"]
    ma20, ma60 = calculate_ma(close, 20), calculate_ma(close, 60)
    dir_raw = (close / ma60 - 1).clip(-k_price, k_price) / k_price * 0.7 + (ma20 / ma60 - 1).clip(-k_arr, k_arr) / k_arr * 0.3
    atr_s = calculate_atr(high, low, close, 10)
    atr_l = calculate_atr(high, low, close, 60)
    ratio = (atr_s / atr_l.replace({0: np.nan})).fillna(1.0)
    vol = np.where(ratio > 1.5, dir_raw * 8, 0)
    roc = weighted_roc(close)
    score = dir_raw * dir_w + roc * roc_w + vol
    score.iloc[:120] = np.nan  # 与 classify_temperature 的 valid_mask(>=120) 对齐
    return pd.DataFrame({"symbol_id": g["symbol_id"].iloc[0], "trade_date": g["trade_date"], "smooth_score": score})


ss = pd.concat([smooth_score_series(g) for _, g in prices.groupby("symbol_id")], ignore_index=True)
samp = samples.merge(ss, left_on=["_sid", "trade_date"], right_on=["symbol_id", "trade_date"], how="left")
samp = samp.dropna(subset=["smooth_score", "ref_lv"])
print(f"有效样本: {len(samp)}")


def bucket_series(score, th):
    t6, t5, t4, t3, t2, t1 = th
    s = score.values if hasattr(score, "values") else score
    out = np.zeros(len(s), dtype=int)
    out[s > t1] = 1
    out[s > t2] = 2
    out[s > t3] = 3
    out[s >= t4] = 4
    out[s >= t5] = 5
    out[s >= t6] = 6
    return out


def search(score_col, t6_t5_grid=((50, 25), (60, 30), (40, 20))):
    best = (-1, None, 0, 0)
    for t6, t5 in t6_t5_grid:
        for t4 in range(3, 20, 2):
            for t3 in range(-25, 5, 2):
                if t3 >= t4:
                    continue
                for t2 in range(-50, -8, 2):
                    if t2 >= t3:
                        continue
                    for t1 in range(-80, -25, 3):
                        if t1 >= t2:
                            continue
                        th = (t6, t5, t4, t3, t2, t1)
                        pred = bucket_series(samp[score_col], th)
                        diff = np.abs(pred - samp["ref_lv"].values)
                        obj = (diff == 0).sum() + 0.5 * (diff == 1).sum()
                        if obj > best[0]:
                            best = (obj, th, (diff == 0).mean(), (diff <= 1).mean())
    return best


b_old = search("temperature_score")
print(f"旧公式(二值方向)最优 {b_old[1]}: exact={b_old[2]:.3f} adj={b_old[3]:.3f}")
b_new = search("smooth_score")
print(f"新公式(连续方向)最优 {b_new[1]}: exact={b_new[2]:.3f} adj={b_new[3]:.3f}")

# 新公式 + 方向权重/roc 权重小网格
best2 = (-1, None, 0, 0, None)
for dir_w, roc_w in [(25, 0.7), (20, 0.7), (30, 0.7), (25, 1.0), (20, 1.0), (30, 0.5)]:
    ss2 = pd.concat(
        [smooth_score_series(g, dir_w=dir_w, roc_w=roc_w) for _, g in prices.groupby("symbol_id")],
        ignore_index=True,
    )
    samp2 = samples.merge(ss2, left_on=["_sid", "trade_date"], right_on=["symbol_id", "trade_date"], how="left").dropna(
        subset=["smooth_score", "ref_lv"]
    )
    samp_bak = samp
    samp = samp2
    b = search("smooth_score", t6_t5_grid=((50, 25),))
    samp = samp_bak
    print(f"  dir_w={dir_w} roc_w={roc_w}: th={b[1]} exact={b[2]:.3f} adj={b[3]:.3f}")
    if b[0] > best2[0]:
        best2 = (b[0], b[1], b[2], b[3], (dir_w, roc_w))
print(f"综合最优: 权重={best2[4]} 阈值={best2[1]} exact={best2[2]:.3f} adj={best2[3]:.3f}")

# ---------- 实验 2b：更快 RS 候选 ----------
CAND = {
    "f_0.4r21/0.3r63/0.2r126/0.1r252": ((21, 63, 126, 252), (0.4, 0.3, 0.2, 0.1)),
    "g_0.5r21/0.3r63/0.2r126": ((21, 63, 126), (0.5, 0.3, 0.2)),
    "h_pure_r21": ((21,), (1.0,)),
    "i_0.4r10/0.3r21/0.2r63/0.1r126": ((10, 21, 63, 126), (0.4, 0.3, 0.2, 0.1)),
    "j_0.5r10/0.3r21/0.2r63": ((10, 21, 63), (0.5, 0.3, 0.2)),
    "k_0.6r21/0.4r63": ((21, 63), (0.6, 0.4)),
}

pools = {}
for nt in ["industry_l1", "industry_l2"]:
    pool_sids = syms[syms["node_type"] == nt]["symbol_id"].tolist()
    pools[nt] = load_prices(pool_sids)[["symbol_id", "trade_date", "close"]]


def pct_scores(pool_df, windows, weights, dates):
    dates = pd.to_datetime(dates).date
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


print("\n== RS 更快候选 ==")
for name, (w, wt) in CAND.items():
    pred = pd.concat([pct_scores(pools[nt], w, wt, DATES) for nt in ["industry_l1", "industry_l2"]], ignore_index=True)
    mm = samples.merge(pred, left_on=["_sid", "trade_date"], right_on=["symbol_id", "trade_date"], how="inner")
    err = (mm["score"] - mm["ref_rs"]).abs()
    spear = mm[["score", "ref_rs"]].corr(method="spearman").iloc[0, 1]
    print(f"{name:38s} MAE={err.mean():5.1f} med={err.median():5.1f} spearman={spear:+.3f} n={len(mm)}")
