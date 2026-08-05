# -*- coding: utf-8 -*-
"""右侧状态机（基于温度状态机的 displayed 档位推导）。

核心规则（来自 Jinkin 的《基本理念》，2026-07 确认口径）：
- 「热」是右侧的正式确认：当品种趋势温度首次温转热时，标志了右侧的开始。
- 期间只要品种的趋势温度没有回到"平"及以下，则右侧趋势没有被破坏。
- 「右侧天数」记录了确认右侧至今的持续天数。

> 关键：用温度状态机输出的 `displayed` 档位驱动，而不是 score_smooth 当天
> 的原始 bucket——否则右侧状态和温度显示会对不上。

状态机规则：
- **进入右侧**：当前不在右侧且 displayed ≥ 热（idx ≥ 5），天数 = 1。
  "温"是准右侧/预警，单独出现不触发进入。
- **维持右侧**：已在右侧且 displayed ≥ 温（idx ≥ 4），天数累计。
- **退出右侧**：已在右侧且 displayed < 温（跌到平/凉/寒/冻），天数清零，状态退出。
- displayed 为 NaN 的交易日：不改变状态、不累计天数（防御性处理）。
- 空头侧（凉/寒/冻）统一叫"左侧"，不计右侧天数。

天数口径：
- 提供 `calendar_dates`（自然日）时，按自然日差计算天数（更贴近"温度反应周期"语义）。
- 未提供时，按行号 +1 累计（旧行为，引擎在种子续算时会传 calendar_dates）。
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_ENTER_TEMPS = {"热", "沸"}
_MAINTAIN_TEMPS = {"温", "热", "沸"}


def compute_right_side_state(
    temperatures: pd.Series,
    initial_in_right: bool = False,
    initial_days: int = 0,
    initial_entry_temp: Optional[str] = None,
    initial_entry_date=None,  # type: ignore[valid-type]
    calendar_dates: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """根据每日 displayed 档位序列计算右侧状态与右侧天数。

    Args:
        temperatures: 每日 displayed 档位序列，元素 ∈ {沸,热,温,平,凉,寒,冻}
        initial_in_right: 序列起点之前是否已在右侧（增量续算的种子状态）
        initial_days: 序列起点之前的右侧天数（种子状态）
        initial_entry_temp: 序列起点之前的入场温度（种子状态）
        initial_entry_date: 序列起点之前的入场自然日（种子状态；给 calendar 时用）
        calendar_dates: 与 temperatures 等长的自然日序列；提供时按自然日算天数。

    Returns:
        DataFrame 列：temperature, is_right_side, right_side_days, right_side_entry_temp
    """
    df = pd.DataFrame({"temperature": temperatures})
    df["is_right_side"] = False
    df["right_side_days"] = 0
    df["right_side_entry_temp"] = None

    in_right = initial_in_right
    entry_temp: Optional[str] = initial_entry_temp if initial_in_right else None
    entry_date = (
        pd.Timestamp(initial_entry_date).date() if initial_entry_date is not None else None
    )
    use_calendar = calendar_dates is not None and len(calendar_dates) == len(df)

    for i in range(len(df)):
        cur_t = df["temperature"].iloc[i]
        if cur_t is None or pd.isna(cur_t):
            # NaN：保持状态，days 按当前模式推进一格
            df.iat[i, df.columns.get_loc("is_right_side")] = in_right
            df.iat[i, df.columns.get_loc("right_side_entry_temp")] = entry_temp
            df.iat[i, df.columns.get_loc("right_side_days")] = _calc_days(
                i, in_right, entry_date, calendar_dates, initial_days
            ) if in_right else 0
            continue

        if in_right:
            if cur_t in _MAINTAIN_TEMPS:
                df.iat[i, df.columns.get_loc("is_right_side")] = True
                df.iat[i, df.columns.get_loc("right_side_entry_temp")] = entry_temp
                df.iat[i, df.columns.get_loc("right_side_days")] = _calc_days(
                    i, True, entry_date, calendar_dates, initial_days
                )
            else:
                in_right = False
                entry_temp = None
                entry_date = None
                df.iat[i, df.columns.get_loc("is_right_side")] = False
                df.iat[i, df.columns.get_loc("right_side_entry_temp")] = None
                df.iat[i, df.columns.get_loc("right_side_days")] = 0
        else:
            if cur_t in _ENTER_TEMPS:
                in_right = True
                entry_temp = cur_t
                if use_calendar and pd.notna(calendar_dates.iloc[i]):
                    entry_date = pd.Timestamp(calendar_dates.iloc[i]).date()
                df.iat[i, df.columns.get_loc("is_right_side")] = True
                df.iat[i, df.columns.get_loc("right_side_entry_temp")] = entry_temp
                df.iat[i, df.columns.get_loc("right_side_days")] = _calc_days(
                    i, True, entry_date, calendar_dates, initial_days
                )
            else:
                df.iat[i, df.columns.get_loc("is_right_side")] = False
                df.iat[i, df.columns.get_loc("right_side_entry_temp")] = None
                df.iat[i, df.columns.get_loc("right_side_days")] = 0

    return df[["temperature", "is_right_side", "right_side_days", "right_side_entry_temp"]]


def _calc_days(i, in_right, entry_date, calendar_dates, initial_days):
    """统一计算 days：日历优先，否则回退到行号。"""
    if not in_right:
        return 0
    if (
        calendar_dates is not None
        and entry_date is not None
        and pd.notna(calendar_dates.iloc[i])
    ):
        return (pd.Timestamp(calendar_dates.iloc[i]).date() - entry_date).days + 1
    # 无 calendar：种子续算时 i 是新序列起点，初始天数已包含 entry
    return initial_days + i