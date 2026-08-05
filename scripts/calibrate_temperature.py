# -*- coding: utf-8 -*-
"""校准脚本：对代表性品种扫参数组合 + 跑新旧 RS 口径对比。

参数扫描：
- SCORE_SMOOTH_SPAN: [2, 3, 4, 5]
- CONFIRM_DAYS: [1, 2, 3]
- RS_TREND_SINGLE_TH: [5, 8, 12]
- RS_TREND_DOUBLE_TH: [12, 18, 24]

RS 双口径：
- 快窗口：(10, 21, 63, 126), (0.4, 0.3, 0.2, 0.1) — 旧/校准口径
- IBD：(21, 63, 126, 252), (0.4, 0.2, 0.2, 0.2) — 新

输出：JSON 写到 web/data/calibration/compare.json，给 HTML 渲染用。
"""
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import _engine  # noqa: E402
from src.indicators.relative_strength import (  # noqa: E402
    calculate_rs_trend,
    rank_rs_by_node_type,
    weighted_return_series,
)
from src.indicators.temperature import (  # noqa: E402
    CONFIRM_DAYS as DEFAULT_CONFIRM,
    SCORE_SMOOTH_SPAN as DEFAULT_SPAN,
    _raw_bucket_idx,
    calculate_ma,
    calculate_atr,
    weighted_roc,
    vol_adj,
    run_temperature_state_machine,
)
import src.indicators.temperature as temp_mod

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("calibrate")


# ---- 选品种 ----
SAMPLE_SYMBOLS = [
    # 指数（3 只）
    ("IDX_000001", "上证指数"),
    ("IDX_000300", "沪深300"),
    ("IDX_000905", "中证500"),
    # L1 行业（3 只）
    ("SW_801080", "电子"),
    ("SW_801120", "食品饮料"),
    ("SW_801890", "机械设备"),
    # L2 行业（2 只）
    ("SW_801081", "半导体"),
    ("SW_801072", "通信设备"),
    # 个股（3 只，跨极端行情）
    ("000001", "平安银行"),
    ("600519", "贵州茅台"),
    ("000333", "美的集团"),
]

PARAM_GRID_TEMP = [
    {"span": 2, "confirm": 1, "label": "span=2,confirm=1"},
    {"span": 3, "confirm": 1, "label": "span=3,confirm=1"},
    {"span": 3, "confirm": 2, "label": "span=3,confirm=2"},
    {"span": 5, "confirm": 2, "label": "span=5,confirm=2"},
    {"span": 5, "confirm": 3, "label": "span=5,confirm=3"},
]
RS_THRESH_GRID = [
    (5, 12, "s=5,d=12"),
    (8, 18, "s=8,d=18"),
    (12, 24, "s=12,d=24"),
]
RS_REGIMES = [
    {"label": "IBD(21/63/126/252)", "windows": (21, 63, 126, 252), "weights": (0.4, 0.2, 0.2, 0.2)},
    {"label": "快窗口(10/21/63/126)", "windows": (10, 21, 63, 126), "weights": (0.4, 0.3, 0.2, 0.1)},
]

WINDOW_DAYS = 90  # 近 3 个月


def load_prices(symbol_ids, start_date, end_date):
    placeholders = ",".join(f":s{i}" for i in range(len(symbol_ids)))
    params = {f"s{i}": s for i, s in enumerate(symbol_ids)}
    params["start_date"] = start_date.isoformat()
    params["end_date"] = end_date.isoformat()
    query = f"""
        SELECT dp.symbol_id, dp.trade_date, dp.open, dp.high, dp.low, dp.close, s.node_type
        FROM daily_price dp JOIN symbols s ON dp.symbol_id = s.symbol_id
        WHERE dp.symbol_id IN ({placeholders})
          AND dp.trade_date BETWEEN :start_date AND :end_date
        ORDER BY dp.symbol_id, dp.trade_date
    """
    df = pd.read_sql(query, _engine, params=params)
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def load_prices_all(start_date, end_date):
    """加载全市场 close + node_type（RS 全市场排名用）。"""
    query = """
        SELECT dp.symbol_id, dp.trade_date, dp.close, s.node_type
        FROM daily_price dp JOIN symbols s ON dp.symbol_id = s.symbol_id
        WHERE dp.trade_date BETWEEN :start_date AND :end_date
        ORDER BY dp.symbol_id, dp.trade_date
    """
    df = pd.read_sql(query, _engine, params={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()})
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def compute_raw_score(df_one_symbol):
    """重算 score_raw（按列），复用于所有参数变体。"""
    n = len(df_one_symbol)
    if n < 20:
        return None
    regime, ma_fast_w, ma_slow_w, atr_long_w, max_w, vol_threshold, roc_windows = (
        ("long", 20, 60, 60, 120, 1.5, (20, 60, 120)) if n >= 250
        else ("mid", 10, 20, 20, 60, 1.4, (10, 30, 60)) if n >= 60
        else ("short", 5, 10, 5, 20, 1.3, (5, 10, 20))
    )
    close = df_one_symbol["close"]
    high = df_one_symbol["high"]
    low = df_one_symbol["low"]
    ma_fast = calculate_ma(close, ma_fast_w)
    ma_slow = calculate_ma(close, ma_slow_w)
    atr_short = calculate_atr(high, low, close, window=max(2, atr_long_w // 6))
    atr_long = calculate_atr(high, low, close, window=atr_long_w)
    atr_ratio = (atr_short / atr_long.replace({0: np.nan})).fillna(1.0)
    roc = weighted_roc(close, windows=roc_windows)
    direction = (close / ma_slow - 1).clip(-0.05, 0.05) / 0.05 * 0.7 + (
        ma_fast / ma_slow - 1
    ).clip(-0.03, 0.03) / 0.03 * 0.3
    vol = vol_adj(direction, atr_ratio, vol_threshold)
    raw = direction * 20 + roc * 0.7 + vol
    valid = pd.Series(np.arange(n) >= max_w, index=raw.index)
    raw = raw.where(valid, np.nan)
    return raw


def run_with_params(score_raw, span, confirm_days):
    """给定 score_raw + 平滑 span + confirm_days，输出 displayed。"""
    smooth = score_raw.ewm(span=span, min_periods=1).mean()
    saved = (temp_mod.SCORE_SMOOTH_SPAN, temp_mod.CONFIRM_DAYS)
    temp_mod.SCORE_SMOOTH_SPAN = span
    temp_mod.CONFIRM_DAYS = confirm_days
    try:
        result = run_temperature_state_machine(smooth)
    finally:
        temp_mod.SCORE_SMOOTH_SPAN, temp_mod.CONFIRM_DAYS = saved
    return result


def right_side_from_displayed(displayed, dates):
    """根据 displayed 档位序列 + 自然日，计算 is_right_side + right_side_days 序列。"""
    from src.indicators.right_side import compute_right_side_state
    s = pd.Series(displayed)
    d = pd.Series([pd.Timestamp(x).date() for x in dates])
    rs = compute_right_side_state(s, calendar_dates=d)
    return rs["is_right_side"].tolist(), rs["right_side_days"].tolist()


def tier_switch_stats(displayed):
    """统计：档位切换总次数、每日最大跨越档数、有没有同日跨 2 档以上。"""
    transitions = 0
    max_step = 0
    for i in range(1, len(displayed)):
        if displayed[i] is None or displayed[i - 1] is None:
            continue
        idx_now = "冻寒凉平温热沸".index(displayed[i])
        idx_prev = "冻寒凉平温热沸".index(displayed[i - 1])
        step = abs(idx_now - idx_prev)
        if step > 0:
            transitions += 1
        max_step = max(max_step, step)
    return {
        "transitions": transitions,
        "max_step": max_step,
        "violates_no_jump": max_step > 1,  # 严禁跨 ≥2 档
    }


def main():
    print(f"加载 {len(SAMPLE_SYMBOLS)} 个品种近 {WINDOW_DAYS} 天数据...")
    end = date(2026, 7, 31)
    start = end - timedelta(days=WINDOW_DAYS + 60)  # 多读一些以满足 252 窗口
    raw = load_prices([s for s, _ in SAMPLE_SYMBOLS], start, end)
    print(f"  原始行数: {len(raw)}")

    # ---- 温度参数扫描 ----
    print("\n温度参数扫描...")
    temp_results = {}  # {(label): {symbol: stats}}
    for params in PARAM_GRID_TEMP:
        temp_results[params["label"]] = {}
        for sid, name in SAMPLE_SYMBOLS:
            df_one = raw[raw["symbol_id"] == sid].sort_values("trade_date").reset_index(drop=True)
            score_raw = compute_raw_score(df_one)
            if score_raw is None:
                continue
            displayed = run_with_params(score_raw, params["span"], params["confirm"])
            stats = tier_switch_stats(displayed)
            # 取末 60 天
            last_n = 60
            tail_displayed = displayed[-last_n:]
            tail_dates = [d.isoformat() for d in df_one["trade_date"].iloc[-last_n:]]
            stats["series"] = tail_displayed
            stats["dates"] = tail_dates
            # 计算右侧状态
            is_right, days = right_side_from_displayed(tail_displayed, tail_dates)
            stats["is_right_side"] = is_right
            stats["right_side_days"] = days
            temp_results[params["label"]][sid] = {"name": name, **stats}

    # ---- RS 双口径对比 ----
    print("RS 双口径对比（基于全市场横截面排名）...")
    # 加载全市场近 N 天数据
    end2 = end
    start2 = end2 - timedelta(days=WINDOW_DAYS + 60)
    full_raw = load_prices_all(start2, end2)
    rs_results = []
    for regime in RS_REGIMES:
        per_regime = {"label": regime["label"], "symbols": {}}
        # 1) 算每个品种的 weighted_return
        wr_list = []
        for sid, grp in full_raw.groupby("symbol_id", sort=False):
            grp = grp.sort_values("trade_date").reset_index(drop=True)
            wr = weighted_return_series(grp["close"], windows=regime["windows"], weights=regime["weights"])
            grp["weighted_return"] = wr.values
            wr_list.append(grp[["symbol_id", "trade_date", "node_type", "weighted_return"]])
        wr_df = pd.concat(wr_list, ignore_index=True)
        # 2) 每天横截面排名
        ranked = rank_rs_by_node_type(wr_df)
        # 3) 算趋势箭头（默认阈值）
        trended = calculate_rs_trend(ranked[["symbol_id", "trade_date", "rs_score"]])
        # 4) 抽样本品种
        for sid, name in SAMPLE_SYMBOLS:
            sub = trended[trended["symbol_id"] == sid].sort_values("trade_date")
            scores = sub["rs_score"].tolist()
            per_regime["symbols"][sid] = {
                "name": name,
                "rs_scores": scores,
                "dates": [d.isoformat() for d in sub["trade_date"].tolist()],
            }
        rs_results.append(per_regime)

    # ---- RS 阈值扫描 ----
    print("RS 阈值扫描...")
    rs_thresh_results = []
    for single, double, label in RS_THRESH_GRID:
        per_thresh = {"label": label, "single": single, "double": double, "symbols": {}}
        # 取 IBD 口径下的 rs_score，再用不同阈值算 trend
        ibd = next(r for r in rs_results if r["label"].startswith("IBD"))
        for sid, name in SAMPLE_SYMBOLS:
            scores = ibd["symbols"].get(sid, {}).get("rs_scores", [])
            dates = ibd["symbols"].get(sid, {}).get("dates", [])
            if not scores:
                continue
            df_trend = pd.DataFrame({
                "symbol_id": [sid] * len(scores),
                "trade_date": pd.to_datetime(dates),
                "rs_score": scores,
            })
            trended = calculate_rs_trend(df_trend, lookback_days=5, single_th=single, double_th=double)
            arrows = trended.sort_values("trade_date")["rs_score_trend"].tolist()
            counts = {a: arrows.count(a) for a in ["↑↑", "↑", "flat", "↓", "↓↓"]}
            per_thresh["symbols"][sid] = {
                "name": name,
                "arrows": arrows,
                "counts": counts,
            }
        rs_thresh_results.append(per_thresh)

    # ---- 写 JSON ----
    out_dir = ROOT / "web" / "data" / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "compare.json"
    payload = {
        "generated_at": date.today().isoformat(),
        "symbols": [{"id": s, "name": n} for s, n in SAMPLE_SYMBOLS],
        "temp_params": [p["label"] for p in PARAM_GRID_TEMP],
        "temp_results": temp_results,
        "rs_regimes": rs_results,
        "rs_thresholds": rs_thresh_results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入: {out_path}")

    # 打印汇总
    print("\n=== 温度：档位切换统计 ===")
    for label, sym_data in temp_results.items():
        total_trans = sum(d["transitions"] for d in sym_data.values())
        violations = sum(1 for d in sym_data.values() if d["violates_no_jump"])
        print(f"  {label}: 总切换 {total_trans}, 跨≥2档品种 {violations}")

    print("\n=== RS：箭头分布（IBD 口径下） ===")
    for thresh in rs_thresh_results:
        total = {"↑↑": 0, "↑": 0, "flat": 0, "↓": 0, "↓↓": 0}
        for s in thresh["symbols"].values():
            for k in total:
                total[k] += s["counts"].get(k, 0)
        print(f"  {thresh['label']}: {total}")


if __name__ == "__main__":
    main()