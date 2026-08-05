# -*- coding: utf-8 -*-
"""趋势相对强度算法。

按 node_type 分组做全局百分位排名，输出 1-99 的 RS 分数及趋势箭头。

窗口口径：IBD RS Rating 起点 21/63/126/252 四个交易日（约 1/3/6/12 月），
权重 [0.4, 0.2, 0.2, 0.2]（近期 1 季度权重更高）。
PRD 7.1 节校准阶段需要用真实数据验证与参考系统（趋势动物"强度"）的排名吻合度，
必要时切换到快窗口口径（10/21/63/126，0.4/0.3/0.2/0.1）。
"""

import logging
from typing import List, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# IBD 风格：1/3/6/12 月回望窗口（21/63/126/252 个交易日）
DEFAULT_RS_WINDOWS: Tuple[int, ...] = (21, 63, 126, 252)
DEFAULT_RS_WEIGHTS: Tuple[float, ...] = (0.4, 0.2, 0.2, 0.2)

# RS 趋势箭头阈值（5 个交易日回望，需校准）
RS_TREND_SINGLE_TH = 8     # 单箭头阈值
RS_TREND_DOUBLE_TH = 18    # 双箭头阈值


def weighted_return_series(
    close: pd.Series,
    windows: Tuple[int, ...] = DEFAULT_RS_WINDOWS,
    weights: Tuple[float, ...] = DEFAULT_RS_WEIGHTS,
) -> pd.Series:
    """计算多周期加权收益率序列，缺失窗口自动 reweight。

    Args:
        close: 按日期排序的收盘价序列。
        windows: 回望窗口（交易日）。
        weights: 对应窗口权重，总和应为 1。

    Returns:
        与 close 等长的 Series，数据不足时对应位置为 NaN。
    """
    if len(windows) != len(weights):
        raise ValueError("windows 与 weights 长度必须相同")

    weights_arr = np.array(weights, dtype=float)
    if not np.isclose(weights_arr.sum(), 1.0):
        weights_arr = weights_arr / weights_arr.sum()

    returns = pd.DataFrame(index=close.index)
    for i, w in enumerate(windows):
        returns[w] = (close / close.shift(w) - 1) * 100

    mask = returns.notna()
    # 每行可用窗口的权重归一化
    denom = (mask.mul(weights_arr, axis=1)).sum(axis=1)
    weighted_sum = (returns.mul(weights_arr, axis=1)).sum(axis=1)
    result = weighted_sum / denom
    # 全部窗口都缺失时 denom 为 0，结果应为 NaN（pandas 会给出 inf，这里修正）
    result = result.where(denom > 0)
    return result


def rank_rs_by_node_type(df: pd.DataFrame) -> pd.DataFrame:
    """按交易日 + node_type 分组，对 weighted_return 做 1-99 排名。

    输入列：symbol_id, trade_date, node_type, weighted_return
    输出列增加：rs_score
    """
    required = {"symbol_id", "trade_date", "node_type", "weighted_return"}
    if not required.issubset(df.columns):
        raise ValueError(f"输入 DataFrame 必须包含列 {required}")

    df = df.dropna(subset=["weighted_return"]).copy()
    if df.empty:
        df["rs_score"] = pd.Series(dtype="Int64")
        return df

    # 整数排名：最强=1，最弱=n
    df["_rank"] = df.groupby(["trade_date", "node_type"])["weighted_return"].rank(
        method="min", ascending=False
    )
    # 同组仅 1 个品种时设为 50
    df["_group_count"] = df.groupby(["trade_date", "node_type"])["symbol_id"].transform("count")
    raw_score = ((1 - (df["_rank"] - 1) / (df["_group_count"] - 1)) * 98 + 1).round().clip(1, 99)
    df["rs_score"] = np.where(df["_group_count"] <= 1, 50, raw_score)
    # 使用 nullable Int64，避免个别 NaN 导致 cast 失败
    df["rs_score"] = df["rs_score"].astype("Int64")
    return df.drop(columns=["_rank", "_group_count"])[["symbol_id", "trade_date", "node_type", "weighted_return", "rs_score"]]


def calculate_rs_trend(
    df: pd.DataFrame,
    lookback_days: int = 5,
    single_th: int = RS_TREND_SINGLE_TH,
    double_th: int = RS_TREND_DOUBLE_TH,
) -> pd.DataFrame:
    """根据历史 RS 分数计算趋势箭头。

    输入列：symbol_id, trade_date, rs_score
    输出列增加：rs_score_prev_1d, rs_score_prev_5d, rs_score_trend

    阈值（lookback_days 个交易日前的 rs_score 对比）：
      delta ≥ double_th → "↑↑"
      delta ≥ single_th → "↑"
      delta ≤ -double_th → "↓↓"
      delta ≤ -single_th → "↓"
      其它 → "flat"
    """
    if df.empty:
        df["rs_score_prev_1d"] = pd.Series(dtype="Int64")
        df["rs_score_prev_5d"] = pd.Series(dtype="Int64")
        df["rs_score_trend"] = pd.Series(dtype=object)
        return df

    df = df.sort_values(["symbol_id", "trade_date"]).copy()
    df["rs_score_prev_1d"] = df.groupby("symbol_id")["rs_score"].shift(1)
    df["rs_score_prev_5d"] = df.groupby("symbol_id")["rs_score"].shift(lookback_days)

    score = df["rs_score"].astype(float)
    prev = df[f"rs_score_prev_{lookback_days}d"].astype(float)
    delta = score - prev
    missing = score.isna() | prev.isna()

    df["rs_score_trend"] = np.where(
        missing,
        "flat",
        np.where(
            delta >= double_th, "↑↑",
            np.where(
                delta >= single_th, "↑",
                np.where(
                    delta <= -double_th, "↓↓",
                    np.where(delta <= -single_th, "↓", "flat"),
                ),
            ),
        ),
    )

    return df.reset_index(drop=True)