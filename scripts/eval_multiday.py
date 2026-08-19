#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多日参考切片回放评估：当前温度公式 + RS 双口径（IBD vs 快窗口）。

对每个 reference.csv 交易日：
  - 温度：用截至当日的价格序列跑当前 classify_temperature，取当日标签/平滑分
  - RS：对全部 L1/L2 行业分别用 IBD(21/63/126/252) 与快窗口(10/21/63/126)
        两种口径计算加权收益并做 node_type 内排名，与参考强度对比

用法：
    python scripts/eval_multiday.py
    python scripts/eval_multiday.py docs/calibration/2026-08-10-reference.csv ...
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd
from sqlalchemy import text

from src.db import get_session
from src.indicators.relative_strength import (
    rank_rs_by_node_type,
    weighted_return_series,
)
from src.indicators.temperature import TEMPERATURE_LEVELS, classify_temperature
from calib_compare import ALIAS  # noqa: E402  (scripts 目录)

# IBD 旧口径（2026-08-11 前的生产默认值），作为对照列固定写死，
# 不随 relative_strength 默认值变化
IBD_RS_WINDOWS = (21, 63, 126, 252)
IBD_RS_WEIGHTS = (0.4, 0.2, 0.2, 0.2)

FAST_RS_WINDOWS = (10, 21, 63, 126)
FAST_RS_WEIGHTS = (0.4, 0.3, 0.2, 0.1)

DEFAULT_REFS = [
    "docs/calibration/2026-07-09-reference.csv",
    "docs/calibration/2026-07-21-reference.csv",
    "docs/calibration/2026-07-28-reference.csv",
    "docs/calibration/2026-08-03-reference.csv",
    "docs/calibration/2026-08-04-reference.csv",
    "docs/calibration/2026-08-10-reference.csv",
    "docs/calibration/2026-08-12-reference.csv",
    "docs/calibration/2026-08-13-reference.csv",
]


def match_symbols(ref_names, symbols):
    by_name = dict(zip(symbols["name"], symbols["symbol_id"]))
    stripped = {}
    for name, sid in zip(symbols["name"], symbols["symbol_id"]):
        stripped.setdefault(name.rstrip("ⅡⅢ"), sid)
    out = {}
    for rn in ref_names:
        rn = rn.strip()
        if rn in by_name:
            out[rn] = by_name[rn]
        elif rn in stripped:
            out[rn] = stripped[rn]
        elif ALIAS.get(rn) and ALIAS[rn] in by_name:
            out[rn] = by_name[ALIAS[rn]]
        elif ALIAS.get(rn) and ALIAS[rn].rstrip("ⅡⅢ") in stripped:
            out[rn] = stripped[ALIAS[rn].rstrip("ⅡⅢ")]
    return out


def load_all_prices(symbols):
    """一次性加载全部 L1/L2 收盘价（含日期），按 symbol_id 分组。"""
    with get_session() as s:
        df = pd.read_sql(
            text(
                "SELECT p.symbol_id, p.trade_date, p.open, p.high, p.low, p.close "
                "FROM daily_price p JOIN symbols s ON p.symbol_id = s.symbol_id "
                "WHERE s.node_type IN ('industry_l1','industry_l2') "
                "ORDER BY p.symbol_id, p.trade_date"
            ),
            s.bind,
        )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return {sid: g for sid, g in df.groupby("symbol_id")}


def eval_date(ref_csv, symbols, price_by_sid):
    trade_date = Path(ref_csv).name.split("-reference")[0]
    ref = pd.read_csv(ref_csv)
    mapping = match_symbols(ref["name"], symbols)
    ref["_sid"] = ref["name"].str.strip().map(lambda n: mapping.get(n))
    matched = ref.dropna(subset=["_sid"]).copy()

    sid_meta = symbols.set_index("symbol_id")
    lv_map = {t: i for i, t in enumerate(TEMPERATURE_LEVELS)}
    cutoff = pd.Timestamp(trade_date)

    # ---- RS：两种口径在当日的 node_type 内排名 ----
    rows = []
    for sid, g in price_by_sid.items():
        g = g[g["trade_date"] <= cutoff]
        if len(g) < 30:
            continue
        close = pd.Series(g["close"].values, index=g["trade_date"])
        wr_ibd = weighted_return_series(close, IBD_RS_WINDOWS, IBD_RS_WEIGHTS).iloc[-1]
        wr_fast = weighted_return_series(close, FAST_RS_WINDOWS, FAST_RS_WEIGHTS).iloc[-1]
        rows.append((sid, sid_meta.loc[sid, "node_type"], wr_ibd, wr_fast))
    rs_ibd = rank_rs_by_node_type(
        pd.DataFrame(
            [(r[0], r[1], r[2]) for r in rows],
            columns=["symbol_id", "node_type", "weighted_return"],
        ).assign(trade_date=trade_date)
    ).set_index("symbol_id")["rs_score"]
    rs_fast = rank_rs_by_node_type(
        pd.DataFrame(
            [(r[0], r[1], r[3]) for r in rows],
            columns=["symbol_id", "node_type", "weighted_return"],
        ).assign(trade_date=trade_date)
    ).set_index("symbol_id")["rs_score"]

    # ---- 温度：当前公式回放 ----
    out = []
    for _, row in matched.iterrows():
        sid = row["_sid"]
        g = price_by_sid.get(sid)
        if g is None:
            continue
        g = g[g["trade_date"] <= cutoff]
        if len(g) < 61:
            continue
        close = pd.Series(g["close"].values, index=g["trade_date"])
        high = pd.Series(g["high"].values, index=g["trade_date"])
        low = pd.Series(g["low"].values, index=g["trade_date"])
        res = classify_temperature(close, high, low)
        local_t = res["temperature"].iloc[-1]
        score = res["temperature_score_smooth"].iloc[-1]
        out.append(
            {
                "date": trade_date,
                "name": row["name"],
                "ref_t": row["ref_temperature"],
                "local_t": str(local_t),
                "score": score,
                "ref_rs": row["ref_rs"],
                "rs_ibd": int(rs_ibd.get(sid)) if sid in rs_ibd.index and pd.notna(rs_ibd.get(sid)) else None,
                "rs_fast": int(rs_fast.get(sid)) if sid in rs_fast.index and pd.notna(rs_fast.get(sid)) else None,
            }
        )

    df = pd.DataFrame(out)
    df["signed"] = df["local_t"].map(lv_map) - df["ref_t"].map(lv_map)
    df["absdiff"] = df["signed"].abs()
    return trade_date, df


def summarize(trade_date, df):
    n = len(df)
    exact = (df["absdiff"] == 0).sum()
    adj = (df["absdiff"] <= 1).sum()
    ibd_mae = (df["rs_ibd"] - df["ref_rs"]).abs().mean()
    fast_mae = (df["rs_fast"] - df["ref_rs"]).abs().mean()
    ibd_mean = (df["rs_ibd"] - df["ref_rs"]).mean()
    fast_mean = (df["rs_fast"] - df["ref_rs"]).mean()
    print(
        f"== {trade_date} ==  品种 {n}\n"
        f"  温度: 一致 {exact}/{n}={exact/n*100:.1f}%  ±1档 {adj}/{n}={adj/n*100:.1f}%  "
        f"signed均值 {df['signed'].mean():+.2f}  分布 {df['signed'].value_counts().sort_index().to_dict()}\n"
        f"  RS-IBD : 均值 {ibd_mean:+.1f}  MAE {ibd_mae:.1f}\n"
        f"  RS-快窗: 均值 {fast_mean:+.1f}  MAE {fast_mae:.1f}"
    )
    return {
        "date": trade_date, "n": n, "exact": exact, "adj": adj,
        "signed_mean": df["signed"].mean(),
        "ibd_mae": ibd_mae, "fast_mae": fast_mae,
    }


def main():
    refs = sys.argv[1:] or DEFAULT_REFS
    with get_session() as s:
        symbols = pd.DataFrame(
            s.execute(
                text("SELECT symbol_id, name, node_type FROM symbols "
                     "WHERE node_type IN ('industry_l1','industry_l2')")
            ).all(),
            columns=["symbol_id", "name", "node_type"],
        )
    price_by_sid = load_all_prices(symbols)

    summaries = []
    frames = []
    for ref_csv in refs:
        trade_date, df = eval_date(ref_csv, symbols, price_by_sid)
        summaries.append(summarize(trade_date, df))
        frames.append(df)

    all_df = pd.concat(frames)
    n = len(all_df)
    print(
        f"\n== 汇总（{n} 样本，{len(refs)} 日）==\n"
        f"  温度: 一致 {(all_df['absdiff']==0).mean()*100:.1f}%  "
        f"±1档 {(all_df['absdiff']<=1).mean()*100:.1f}%  "
        f"signed均值 {all_df['signed'].mean():+.2f}\n"
        f"  RS-IBD : MAE {(all_df['rs_ibd']-all_df['ref_rs']).abs().mean():.1f}\n"
        f"  RS-快窗: MAE {(all_df['rs_fast']-all_df['ref_rs']).abs().mean():.1f}"
    )
    out_csv = ROOT / "docs" / "calibration" / "multiday-eval.csv"
    all_df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"明细 -> {out_csv}")


if __name__ == "__main__":
    main()
