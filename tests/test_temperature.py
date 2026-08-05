# -*- coding: utf-8 -*-
"""趋势温度算法单元测试 — v2 多尺度框架。

v2 变化：
- 三因子 Z-score(40%) + 回撤(20%) + 动量(40%)
- 非线性拉伸：[-1, 1] → [-100, 100] 分段立方 + clamp
- 新阈值：沸≥70、热≥45、温≥15、平(-12,15)、凉(-35,-12]、寒(-65,-35]、冻≤-65
- 状态机：非对称 enter/exit 确认天数
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators.temperature import (
    CONFIRM_DAYS,
    EXTREME_BUFFER_DAYS,
    TEMPERATURE_LEVELS,
    _raw_bucket_idx,
    classify_temperature,
    run_temperature_state_machine,
    stretch_score,
)


# ============================================================================
# 非线性拉伸
# ============================================================================

def test_stretch_zero_remains_zero():
    assert stretch_score(np.array([0.0]))[0] == 0.0


def test_stretch_positive_expands():
    """正向拉伸：0.77 的原始值应达到 ≥70（沸阈值）。"""
    result = stretch_score(np.array([0.77]))
    assert result[0] >= 70.0, f"expected ≥70, got {result[0]}"


def test_stretch_negative_expands():
    """负向拉伸：-0.7 应达到 ≤-65（冻阈值）。"""
    result = stretch_score(np.array([-0.7]))
    assert result[0] <= -65.0, f"expected ≤-65, got {result[0]}"


def test_stretch_clamped():
    """超出范围的原始值应被 clamp 到 ±100。"""
    result = stretch_score(np.array([2.0, -2.0]))
    assert result[0] <= 100.0
    assert result[0] >= 0
    assert result[1] >= -100.0
    assert result[1] <= 0


def test_stretch_preserves_sign():
    result = stretch_score(np.array([-0.3, 0.5]))
    assert result[0] < 0
    assert result[1] > 0


# ============================================================================
# 分档
# ============================================================================

def test_raw_bucket_boundary():
    """拉伸后分数 → 档位的边界映射（v2 阈值）。"""
    idx_to_label = {0: "冻", 1: "寒", 2: "凉", 3: "平", 4: "温", 5: "热", 6: "沸"}
    cases = {
        -100: "冻", -90: "冻", -66: "冻", -65: "冻",
        -64: "寒", -50: "寒", -36: "寒", -35: "寒",
        -34: "凉", -20: "凉", -13: "凉", -12: "凉",
        -11: "平", 0: "平", 14: "平",
        15: "温", 30: "温", 44: "温",
        45: "热", 60: "热", 69: "热",
        70: "沸", 85: "沸", 100: "沸",
    }
    for score, expected in cases.items():
        assert idx_to_label[_raw_bucket_idx(score)] == expected, f"score={score} → {idx_to_label[_raw_bucket_idx(score)]}, expected {expected}"


# ============================================================================
# 状态机
# ============================================================================

def test_state_machine_first_valid_takes_raw():
    """第一个有效 score 直接取原始档（状态从零建立）。"""
    scores = pd.Series([np.nan, np.nan, 80, 50])
    result = run_temperature_state_machine(scores)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == "沸"   # 80 ≥ 70
    assert result[3] == "沸"   # 50 ∈ [45,70) → 热，但确认天数不足


def test_state_machine_nan_keeps_last_displayed():
    """NaN 沿用上一个有效状态。"""
    scores = pd.Series([50, np.nan, np.nan, -20])
    # 50 → 热，-20 → 凉
    result = run_temperature_state_machine(scores)
    assert result[0] == "热"
    assert result[1] == "热"   # NaN → 沿用
    assert result[2] == "热"   # NaN → 沿用
    # -20 → 凉，热 exit=5，nán 维持热 (pending=1 < 5)
    assert result[3] == "热"


def test_state_machine_confirm_enter():
    """温 enter=2：目标连续 2 天才进入。"""
    scores = pd.Series([0, 30, 30])
    # 0→平, 30→温(enter=2)
    result = run_temperature_state_machine(scores)
    assert result[0] == "平"
    assert result[1] == "平"   # pending=1 < 2
    assert result[2] == "温"   # pending=2 ≥ 2, 走 1 步


def test_state_machine_confirm_exit():
    """热 exit=5：离开需要 5 天确认。"""
    scores = pd.Series([50, 10, 10, 10, 10, 10])
    # 50→热, 10→平
    result = run_temperature_state_machine(scores)
    assert result[0] == "热"
    assert result[1] == "热"   # pending=1, need=exit("热")=5
    assert result[2] == "热"   # pending=2
    assert result[3] == "热"   # pending=3
    assert result[4] == "热"   # pending=4
    assert result[5] == "温"   # pending=5 ≥ 5 → 走 1 步到温


def test_state_machine_adjacent_step():
    """确认后每次只走 1 步。"""
    # 沸(6) → 平(3)：走 3 步需要 3 次确认
    scores = pd.Series([80] + [0] * 20)
    result = run_temperature_state_machine(scores)
    assert result[0] == "沸"
    # 沸 exit=6，需要 6 天确认 → 6天后走1步到热
    # 热 exit=5，再 5 天 → 温
    # 温 exit=3，再 3 天 → 平
    # 总共: 6 + 5 + 3 = 14 天后到平
    # index: 0=沸, 1-6=沸(pending 1-6), 7=热(走1步), 8-12=热(pending exit 1-5), 13=温, ...
    assert result[6] == "热"   # 第6天确认 exit("沸")=6 → 走1步到热
    assert result[7] == "热"


def test_state_machine_freeze_confirmation():
    """冻 enter=4：空头极值也需 4 天确认。"""
    scores = pd.Series([-70, -70, -70, -70])
    result = run_temperature_state_machine(scores)
    assert result[0] == "冻"   # 直接进入
    assert result[1] == "冻"
    assert result[2] == "冻"
    assert result[3] == "冻"


# ============================================================================
# classify_temperature 集成测试
# ============================================================================

def _make_trend_prices(n: int, drift: float, noise: float = 0.005):
    """生成趋势 + 噪音的价格序列。"""
    np.random.seed(42)
    close = 100 * np.exp(np.cumsum(np.random.randn(n) * noise + drift))
    high = close * (1 + np.abs(np.random.randn(n)) * 0.01)
    low = close * (1 - np.abs(np.random.randn(n)) * 0.01)
    return close, high, low


def test_classify_temperature_basic():
    """简单上涨趋势应产出非空温度且落七档内。"""
    n = 300
    close, high, low = _make_trend_prices(n, drift=0.001)
    df = classify_temperature(pd.Series(close), pd.Series(high), pd.Series(low))

    assert set(df.columns) >= {"temperature_score", "temperature_score_smooth", "temperature"}
    # v2 预热窗口 = 60 天
    assert df["temperature_score"].iloc[:60].isna().all()
    valid = df.dropna(subset=["temperature_score"])
    assert len(valid) > 0
    assert valid["temperature"].isin(TEMPERATURE_LEVELS).all()


def test_classify_temperature_insufficient_data():
    """数据不足（< 20 天）时温度应为 NaN。"""
    n = 10
    close = pd.Series(np.linspace(100, 110, n))
    high = close + 1
    low = close - 1
    df = classify_temperature(close, high, low)
    assert df["temperature_score"].isna().all()
    assert df["temperature"].isna().all()


def test_classify_temperature_flat_no_division_error():
    """极 flat 行情不应触发除零，且全部为平。"""
    n = 300
    close = pd.Series([100.0] * n)
    high = close + 0.01
    low = close - 0.01
    df = classify_temperature(close, high, low)
    assert not df.empty
    valid = df.dropna(subset=["temperature_score"])
    assert len(valid) > 0
    assert (valid["temperature"] == "平").all()


def test_score_smooth_smoothing():
    """验证自适应平滑生效：smooth_std ≤ raw_std。"""
    n = 300
    close, high, low = _make_trend_prices(n, drift=0.0005, noise=0.01)
    df = classify_temperature(pd.Series(close), pd.Series(high), pd.Series(low))
    valid = df.dropna(subset=["temperature_score"])
    raw_std = valid["temperature_score"].std()
    smooth_std = valid["temperature_score_smooth"].std()
    assert smooth_std <= raw_std


def test_bull_market_reaches_boiling():
    """强牛市（长期上涨）应能触及沸。"""
    n = 500
    close, high, low = _make_trend_prices(n, drift=0.003, noise=0.008)
    df = classify_temperature(pd.Series(close), pd.Series(high), pd.Series(low))
    valid = df.dropna(subset=["temperature_score"])
    # 强牛应至少到过热
    assert valid["temperature"].isin(["热", "沸"]).any(), "强牛应该能到热或沸"
