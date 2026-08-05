# -*- coding: utf-8 -*-
"""趋势相对强度算法单元测试。"""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators.relative_strength import (
    calculate_rs_trend,
    rank_rs_by_node_type,
    weighted_return_series,
)


def test_weighted_return_series_reweight_and_nan():
    """数据不足最大窗口时，应自动 reweight，全缺失时返回 NaN。"""
    # 窗口 (10,21,63,126)：100 天数据 10/21/63 可用，126 缺失 → reweight
    close = pd.Series(100 * np.exp(np.linspace(0, 0.1, 100)))
    ret = weighted_return_series(close)
    # 最短窗口 10 之前全 NaN；第 63 天起 10/21/63 全部可用，必非 NaN
    assert ret.iloc[63:].notna().all()
    assert ret.iloc[:10].isna().all()


def test_weighted_return_series_all_nan_when_too_short():
    """数据少于最小窗口时全部返回 NaN。"""
    close = pd.Series([100, 101, 102])
    ret = weighted_return_series(close)
    assert ret.isna().all()


def test_rank_rs_by_node_type_range():
    """排名应落在 1-99，且最强为 99、最弱为 1。"""
    df = pd.DataFrame({
        "symbol_id": ["A", "B", "C", "D", "E"],
        "trade_date": [date(2024, 1, 1)] * 5,
        "node_type": ["stock"] * 5,
        "weighted_return": [10, 5, 0, -5, -10],
    })
    ranked = rank_rs_by_node_type(df)
    scores = ranked.sort_values("symbol_id")["rs_score"].tolist()
    assert min(scores) == 1
    assert max(scores) == 99
    assert all(1 <= s <= 99 for s in scores)
    assert ranked.loc[ranked["weighted_return"].idxmax(), "rs_score"] == 99
    assert ranked.loc[ranked["weighted_return"].idxmin(), "rs_score"] == 1


def test_calculate_rs_trend_arrows():
    """构造分数序列，验证趋势箭头规则（默认 single_th=8, double_th=18）。"""
    df = pd.DataFrame({
        "symbol_id": ["X"] * 8,
        "trade_date": pd.date_range("2024-01-01", periods=8),
        "rs_score": [50, 55, 45, 30, 50, 80, 40, 35],
    })
    result = calculate_rs_trend(df)
    trends = result.sort_values("trade_date")["rs_score_trend"].tolist()
    assert trends[0] == "flat"          # 无历史
    # lookback=5：trends[5] 对比 row0, trends[6] 对比 row1, trends[7] 对比 row2
    assert trends[5] == "↑↑"            # 80 vs 50 = +30 ≥ double_th
    assert trends[6] == "↓"             # 40 vs 55 = -15, |-15| > single_th(8) 但 < double_th(18)
    assert trends[7] == "↓"             # 35 vs 45 = -10 ≤ -single_th
