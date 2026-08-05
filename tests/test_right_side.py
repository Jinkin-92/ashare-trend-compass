# -*- coding: utf-8 -*-
"""右侧状态机单元测试（基于 displayed 档位 + 自然日天数）。

口径：进入需"热/沸"；"温"及以上维持并累计（自然日）；"平"及以下退出清零。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators.right_side import compute_right_side_state


def _dates(*ymd: str) -> pd.Series:
    """辅助：生成自然日序列。"""
    return pd.Series(pd.to_datetime(list(ymd)).date)


def test_long_right_side_enter_maintain_exit():
    """多头右侧：平/平/温/热/热/温/平（按行号累计，单日序列）。"""
    temps = pd.Series(["平", "平", "温", "热", "热", "温", "平"])
    dates = _dates("2026-07-25", "2026-07-26", "2026-07-27", "2026-07-28",
                   "2026-07-29", "2026-07-30", "2026-07-31")
    result = compute_right_side_state(temps, calendar_dates=dates)
    assert result.loc[3, "is_right_side"] == True
    assert result.loc[3, "right_side_days"] == 1
    assert result.loc[3, "right_side_entry_temp"] == "热"
    # 第 5 天维持（自然日 29 - 28 + 1 = 2）
    assert result.loc[4, "is_right_side"] == True
    assert result.loc[4, "right_side_days"] == 2
    # 第 6 天温度=温：维持，自然日 30 - 28 + 1 = 3
    assert result.loc[5, "is_right_side"] == True
    assert result.loc[5, "right_side_days"] == 3
    # 第 7 天温度=平：退出清零
    assert result.loc[6, "is_right_side"] == False
    assert result.loc[6, "right_side_days"] == 0


def test_short_side_is_not_right_side():
    """空头侧：凉/寒/冻 都不是右侧。"""
    temps = pd.Series(["平", "凉", "寒", "寒", "冻", "冻", "平"])
    result = compute_right_side_state(temps)
    assert list(result["is_right_side"]) == [False] * 7
    assert all(result["right_side_days"] == 0)


def test_long_right_side_enter_沸_directly():
    """沸直接触发多头右侧。"""
    temps = pd.Series(["平", "沸", "热"])
    result = compute_right_side_state(temps)
    assert list(result["is_right_side"]) == [False, True, True]
    assert result.loc[1, "right_side_entry_temp"] == "沸"


def test_温_不进入右侧_但是预警():
    """温是准右侧（预警），单独出现不算正式右侧，天数=0。"""
    temps = pd.Series(["平", "温", "温", "温", "平"])
    result = compute_right_side_state(temps)
    assert list(result["is_right_side"]) == [False, False, False, False, False]


def test_进入后_温度回到平_退出():
    """热 → 平：退出右侧。"""
    temps = pd.Series(["平", "热", "热", "平"])
    result = compute_right_side_state(temps)
    assert list(result["is_right_side"]) == [False, True, True, False]
    assert result.loc[1, "right_side_days"] == 1
    assert result.loc[2, "right_side_days"] == 2
    assert result.loc[3, "right_side_days"] == 0


def test_凉_退出后重新进入需要再热_不累计():
    """退出后重新进入需要再触发"热"，不累计之前的天数。"""
    temps = pd.Series(["平", "热", "热", "平", "热"])
    dates = _dates("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31")
    result = compute_right_side_state(temps, calendar_dates=dates)
    assert result.loc[1, "right_side_days"] == 1
    assert result.loc[2, "right_side_days"] == 2
    assert result.loc[3, "right_side_days"] == 0
    assert result.loc[4, "right_side_days"] == 1


def test_持续热_长期右侧():
    """持续 30 天热 → 自然日累计 30。"""
    dates = pd.Series(pd.date_range("2026-07-01", periods=30).date)
    temps = pd.Series(["热"] * 30)
    result = compute_right_side_state(temps, calendar_dates=dates)
    assert list(result["is_right_side"]) == [True] * 30
    assert result["right_side_days"].iloc[-1] == 30


def test_右侧中途温度震荡_温不破坏():
    """热/温反复震荡但不归平：右侧持续，天数累计（自然日）。"""
    temps = pd.Series(["热", "温", "热", "温", "温", "平"])
    dates = pd.Series(pd.date_range("2026-07-26", periods=6).date)
    result = compute_right_side_state(temps, calendar_dates=dates)
    assert list(result["is_right_side"]) == [True, True, True, True, True, False]
    # 26 进入，27 维持2，28 维持3，29 维持4，30 维持5
    assert list(result["right_side_days"]) == [1, 2, 3, 4, 5, 0]


def test_温度缺失_保持状态不累计():
    """温度为 NaN 的交易日：状态保持，天数不变。"""
    temps = pd.Series(["热", None, "温", "平"])
    dates = _dates("2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31")
    result = compute_right_side_state(temps, calendar_dates=dates)
    assert result.loc[0, "is_right_side"] == True
    assert result.loc[0, "right_side_days"] == 1
    # NaN 不累计也不变天数，沿用上一个有效 displayed
    assert result.loc[1, "is_right_side"] == True
    assert result.loc[1, "right_side_days"] == 2  # 自然日仍推进
    assert result.loc[2, "right_side_days"] == 3
    assert result.loc[3, "is_right_side"] == False


def test_自然日天数_不依赖行号():
    """验证：相同温度序列，给不同 calendar_dates，天数反映自然日差而不是行号。"""
    temps = pd.Series(["热", "热", "热"])
    # 紧密日历：3 天
    tight = _dates("2026-07-29", "2026-07-30", "2026-07-31")
    r1 = compute_right_side_state(temps, calendar_dates=tight)
    # 稀疏日历：跨越 14 天
    sparse = _dates("2026-07-15", "2026-07-22", "2026-07-29")
    r2 = compute_right_side_state(temps, calendar_dates=sparse)
    assert r1["right_side_days"].tolist() == [1, 2, 3]
    assert r2["right_side_days"].tolist() == [1, 8, 15]


def test_种子状态续算_与全量一致():
    """带种子状态的增量续算结果应与全量计算一致（自然日模式下）。"""
    full = pd.Series(["平", "热", "热", "温", "热", "热", "平"])
    dates = pd.Series(pd.date_range("2026-07-25", periods=7).date)
    full_result = compute_right_side_state(full, calendar_dates=dates)
    seed = full_result.loc[3]
    seed_date = dates.iloc[3]  # 2026-07-28
    inc_result = compute_right_side_state(
        full.iloc[4:].reset_index(drop=True),
        initial_in_right=bool(seed["is_right_side"]),
        initial_days=int(seed["right_side_days"]),
        initial_entry_temp=seed["right_side_entry_temp"],
        initial_entry_date=seed_date - pd.Timedelta(days=int(seed["right_side_days"]) - 1),
        calendar_dates=dates.iloc[4:].reset_index(drop=True),
    )
    assert list(inc_result["is_right_side"]) == list(full_result.loc[4:, "is_right_side"])
    assert list(inc_result["right_side_days"]) == list(full_result.loc[4:, "right_side_days"])