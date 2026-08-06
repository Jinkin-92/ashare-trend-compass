# -*- coding: utf-8 -*-
"""趋势温度算法 — 多尺度框架 v2。

七档温度：沸 / 热 / 温 / 平 / 凉 / 寒 / 冻

由三个维度加权合成：
1. Z-score 分 (40%): 多周期滚动 Z-score（60/120/250 日），tanh 压缩
2. 回撤深度 (20%): 距 250 日高点偏离度
3. 多尺度动量 (40%): 20/60/120 日 ROC + MA 排列

原始温度 ∈ [-1, 1] → 非线性拉伸 → [-100, 100]（分段立方拉伸 + clamp）

分档阈值（拉伸后 [-100, 100] 区间）：
  沸 ≥ 70，热 ≥ 45，温 ≥ 15，平 (-12, 15)，凉 (-35, -12]，寒 (-65, -35]，冻 ≤ -65

状态机（非对称确认天数）：
  - 沸 enter=3 / exit=6，冻 enter=4 / exit=8
  - 热/寒 enter=3 / exit=5
  - 温/凉 enter=2 / exit=3
  - 平 enter=1 / exit=1
  - 自适应平滑：极端档 span=12，中档 span=5-8，平档 span=3

与旧版 v1 的主要区别：
  - v2 用多尺度 Z-score + 回撤 + 动量替代 v1 的 MA 方向 + ROC + vol_adj
  - v2 增加非线性拉伸解决原始分压缩问题
  - v2 状态机从对称缓冲改为非对称 enter/exit 确认
"""

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TEMPERATURE_LEVELS = ["冻", "寒", "凉", "平", "温", "热", "沸"]
_BUCKET_IDX: Dict[str, int] = {name: i for i, name in enumerate(TEMPERATURE_LEVELS)}
_IDX_BUCKET: Dict[int, str] = dict(enumerate(TEMPERATURE_LEVELS))
EXTREME_IDX = {0, 6}  # 冻 / 沸

# ---- 状态机参数 ----
CONFIRM_DAYS: Dict[str, Dict[str, int]] = {
    "沸": {"enter": 3, "exit": 1},
    "冻": {"enter": 3, "exit": 1},
    "热": {"enter": 2, "exit": 1},
    "寒": {"enter": 2, "exit": 1},
    "温": {"enter": 1, "exit": 1},
    "凉": {"enter": 1, "exit": 1},
    "平": {"enter": 1, "exit": 1},
}
EXTREME_BUFFER_DAYS = 3  # 保留以兼容旧引用（v2 不再使用此常量）

# ---- 权重 ----
Z_WEIGHT = 0.40
DD_WEIGHT = 0.20
MOM_WEIGHT = 0.40

# ---- 拉伸后 [-100, 100] 区间的分档阈值 ----
SCORE_THRESHOLDS = [70, 45, 15, -12, -35, -65]  # 沸, 热, 温, 平, 凉, 寒; 其余为冻


# ============================================================================
# 底层滚动计算（纯 numpy，不依赖 pandas）
# ============================================================================

def _rolling_min(arr: np.ndarray, w: int) -> np.ndarray:
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.min(arr[i - w + 1 : i + 1])
    return s


def _rolling_max(arr: np.ndarray, w: int) -> np.ndarray:
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.max(arr[i - w + 1 : i + 1])
    return s


def _rolling_mean(arr: np.ndarray, w: int) -> np.ndarray:
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.mean(arr[i - w + 1 : i + 1])
    return s


def _rolling_std(arr: np.ndarray, w: int) -> np.ndarray:
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.std(arr[i - w + 1 : i + 1], ddof=1)
    return s


# ============================================================================
# 非线性拉伸
# ============================================================================

def stretch_score(raw_temp: np.ndarray) -> np.ndarray:
    """对 [-1, 1] 的原始温度做分段立方拉伸 → [-100, 100]（带 clamp）。

    正向: y = x * 65 * (1 + 1.2 * x²)
    负向: y = x * 65 * (1 + 1.5 * x²)  （负向略放大，冻只在 raw ≤ -0.7 时触发）
    clamp: [-100, 100]
    """
    result = np.zeros_like(raw_temp, dtype=float)

    mask_pos = raw_temp > 0
    mask_neg = raw_temp < 0

    result[mask_pos] = raw_temp[mask_pos] * 65 * (1 + 1.2 * raw_temp[mask_pos] ** 2)
    result[mask_neg] = raw_temp[mask_neg] * 65 * (1 + 1.5 * raw_temp[mask_neg] ** 2)
    # 零保持零

    return np.clip(result, -100.0, 100.0)


# ============================================================================
# 分档
# ============================================================================

def _raw_bucket_idx(score: float) -> int:
    """拉伸后分数 → 档位索引（沸=6, ..., 冻=0）。"""
    if score >= 70:
        return 6
    if score >= 45:
        return 5
    if score >= 15:
        return 4
    if score > -12:
        return 3
    if score > -35:
        return 2
    if score > -65:
        return 1
    return 0


def _raw_level_str(score: float) -> str:
    return _IDX_BUCKET[_raw_bucket_idx(score)]


# ============================================================================
# 多尺度温度计算（核心）
# ============================================================================

def compute_raw_temperature(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
) -> np.ndarray:
    """计算原始温度 raw_temp ∈ [-1, 1]（三因子加权合成）。

    Returns:
        raw_temp: shape (n,) 数组，nan 表示数据不足以计算
    """
    n = len(close)

    # ---- 1) Z-score 综合 (40%) ----
    windows_z = [60, 120, 250]
    z_weights_arr = [0.35, 0.35, 0.30]
    z_composite = np.zeros(n)
    z_weights_used = np.zeros(n)
    for wi, w in enumerate(windows_z):
        if n < w:
            continue
        mu = _rolling_mean(close, w)
        sigma = _rolling_std(close, w)
        valid = (~np.isnan(mu)) & (~np.isnan(sigma)) & (sigma > 0)
        z = np.full(n, 0.0)
        z[valid] = (close[valid] - mu[valid]) / sigma[valid]
        z = np.clip(z, -3.5, 3.5)
        z_composite[valid] += z[valid] * z_weights_arr[wi]
        z_weights_used[valid] += z_weights_arr[wi]
    valid_w = z_weights_used > 0
    z_composite[valid_w] /= z_weights_used[valid_w]
    z_score = np.tanh(z_composite * 0.8)

    # ---- 2) 回撤深度 (20%) ----
    hi250 = _rolling_max(high, 250)
    dd_pct = np.full(n, 0.0)
    valid_dd = ~np.isnan(hi250)
    dd_pct[valid_dd] = (close[valid_dd] - hi250[valid_dd]) / (hi250[valid_dd] + 1e-9)
    dd_score = np.tanh(dd_pct * 3.5)

    # ---- 3) 多尺度动量 (40%) ----
    mom_windows = [20, 60, 120]
    mom_weights_arr = [0.45, 0.30, 0.25]
    mom_composite = np.zeros(n)
    mom_w_used = np.zeros(n)
    for wi, w in enumerate(mom_windows):
        if n < w:
            continue
        roc = np.full(n, np.nan)
        roc[w:] = close[w:] / close[:-w] - 1.0
        roc = np.clip(roc, -0.5, 0.5)
        roc_score = np.tanh(roc * 4.0)
        mf_w = max(5, w // 4)
        ma_f = _rolling_mean(close, mf_w)
        ma_s = _rolling_mean(close, w)
        valid_ma = (~np.isnan(ma_f)) & (~np.isnan(ma_s)) & (ma_s > 0)
        ma_ratio = np.full(n, 0.0)
        ma_ratio[valid_ma] = (ma_f[valid_ma] / ma_s[valid_ma] - 1.0)
        ma_ratio = np.clip(ma_ratio, -0.15, 0.15)
        ma_score = np.tanh(ma_ratio * 15)
        scale_score = roc_score * 0.6 + ma_score * 0.4
        mask = ~np.isnan(scale_score)
        mom_composite[mask] += scale_score[mask] * mom_weights_arr[wi]
        mom_w_used[mask] += mom_weights_arr[wi]
    valid_m = mom_w_used > 0
    mom_composite[valid_m] /= mom_w_used[valid_m]

    # ---- 加权合成 ----
    raw_temp = z_score * Z_WEIGHT + dd_score * DD_WEIGHT + mom_composite * MOM_WEIGHT
    return raw_temp


# ============================================================================
# 自适应平滑
# ============================================================================

def smooth_temperature(raw_scaled: np.ndarray) -> np.ndarray:
    """自适应 EMA 平滑：分数越远离零（越极端），平滑越强。

    平/温/凉 (|s|<30): span=3~5
    热/寒 (|s| 30~55): span=8
    沸/冻 (|s|≥55): span=12
    """
    n = len(raw_scaled)
    smooth = np.full(n, np.nan)
    first_valid = np.where(~np.isnan(raw_scaled))[0]
    if len(first_valid) == 0:
        return smooth
    smooth[first_valid[0]] = raw_scaled[first_valid[0]]
    for i in range(first_valid[0] + 1, n):
        if np.isnan(raw_scaled[i]):
            smooth[i] = smooth[i - 1]
            continue
        abs_score = (
            abs(smooth[i - 1])
            if not np.isnan(smooth[i - 1])
            else abs(raw_scaled[i])
        )
        if abs_score < 15:
            span = 3
        elif abs_score < 30:
            span = 5
        elif abs_score < 55:
            span = 8
        else:
            span = 12
        alpha = 2.0 / (span + 1)
        smooth[i] = alpha * raw_scaled[i] + (1 - alpha) * smooth[i - 1]
    return smooth


# ============================================================================
# 状态机
# ============================================================================

def run_temperature_state_machine(score_smooth) -> List[Optional[str]]:
    """自适应状态机（v2：非对称 enter/exit 确认天数）。

    规则：
    - NaN：保持上一个有效状态
    - 首个有效值：直接取原始档
    - 非对称确认：向外（远离"平"）退出用 exit 天数，向内靠近用 enter 天数
    - 确认后每次走 1 步（相邻转移）

    Args:
        score_smooth: np.ndarray 或 pd.Series
    """
    if isinstance(score_smooth, pd.Series):
        score_smooth = score_smooth.values
    n = len(score_smooth)
    state: List[Optional[str]] = [None] * n
    current: Optional[str] = None
    pending_level: Optional[str] = None
    pending_days: int = 0

    for i in range(n):
        s = score_smooth[i]
        if np.isnan(s):
            state[i] = current
            continue

        rl = _raw_level_str(float(s))

        if current is None:
            current = rl
        elif rl != current:
            if pending_level != rl:
                pending_level = rl
                pending_days = 1
            else:
                pending_days += 1

            cur_idx = _BUCKET_IDX[current]
            tgt_idx = _BUCKET_IDX[rl]
            # 向外（远离"平"索引 3）用 exit，向内用 enter
            if abs(cur_idx - 3) > abs(tgt_idx - 3):
                need = CONFIRM_DAYS[current]["exit"]
            else:
                need = CONFIRM_DAYS[rl]["enter"]

            if pending_days >= need:
                # 确认天数已满足，直接跳到目标档位
                # （确认天数本身就是滞后滤波，不需要额外限制为相邻转移）
                current = rl
                pending_level = None
                pending_days = 0
        else:
            # 回到当前档位：保持 pending 不变（容忍小幅波动，不清零不递减）
            pass

        state[i] = current

    return state


# ============================================================================
# 主入口
# ============================================================================

def classify_temperature(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
) -> pd.DataFrame:
    """多尺度趋势温度（v2 框架）。

    Args:
        close, high, low: 价格序列

    Returns:
        DataFrame:
            close, ma20, ma60, temperature_score (拉伸后),
            temperature_score_smooth, temperature
    """
    c = close.values.astype(float)
    h = high.values.astype(float)
    l = low.values.astype(float)
    n = len(c)

    if n < 20:
        logger.debug("数据长度 %s 小于最小周期 20，返回空温度", n)
        return pd.DataFrame(
            {
                "close": close.values,
                "ma20": np.full(n, np.nan),
                "ma60": np.full(n, np.nan),
                "temperature_score": np.full(n, np.nan),
                "temperature_score_smooth": np.full(n, np.nan),
                "temperature": pd.Categorical([np.nan] * n, categories=TEMPERATURE_LEVELS),
            },
            index=close.index,
        )

    # 计算原始温度 → 非线性拉伸 → 自适应平滑
    raw_temp = compute_raw_temperature(c, h, l)
    raw_scaled = stretch_score(raw_temp)
    smooth_arr = smooth_temperature(raw_scaled)

    # 状态机
    states = run_temperature_state_machine(smooth_arr)

    # 数据不足窗口（最少 60 天才有意义的第一组 Z-score）
    min_window = 60
    for i in range(min(n, min_window)):
        if not np.isnan(raw_scaled[i]):
            pass  # keep it
    # 前 min_window 天标记为 NaN
    valid_start = min_window if n >= min_window else n
    raw_scaled[:valid_start] = np.nan
    smooth_arr[:valid_start] = np.nan
    for i in range(valid_start):
        states[i] = None

    return pd.DataFrame(
        {
            "close": close.values,
            "ma20": _rolling_mean(c, 20),
            "ma60": _rolling_mean(c, 60),
            "temperature_score": raw_scaled,
            "temperature_score_smooth": smooth_arr,
            "temperature": pd.Categorical(states, categories=TEMPERATURE_LEVELS),
        },
        index=close.index,
    )[
        [
            "close",
            "ma20",
            "ma60",
            "temperature_score",
            "temperature_score_smooth",
            "temperature",
        ]
    ]


# ============================================================================
# 向后兼容导出（v1 旧脚本引用）
# ============================================================================

SCORE_SMOOTH_SPAN = 3  # v1 常量，v2 不再使用


def calculate_ma(series: pd.Series, window: int) -> pd.Series:
    """计算简单移动平均（v1 兼容）。"""
    return series.rolling(window=window, min_periods=1).mean()


def calculate_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14
) -> pd.Series:
    """计算 ATR（v1 兼容）。"""
    high_low = high - low
    high_close = (high - close.shift(1)).abs()
    low_close = (low - close.shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=window, min_periods=1).mean()


def weighted_roc(
    close: pd.Series, windows: Tuple[int, ...] = (5, 10, 20, 60, 120)
) -> pd.Series:
    """多周期收益率加权（v1 兼容）。"""
    weights = np.linspace(0.5, 0.1, len(windows))
    weights = weights / weights.sum()
    rocs = []
    for w in windows:
        roc = (close / close.shift(w) - 1) * 100
        roc = np.sign(roc) * np.minimum(np.abs(roc), 30)
        rocs.append(roc)
    rocs_df = pd.concat(rocs, axis=1)
    return (rocs_df * weights).sum(axis=1)


def vol_adj(
    dir_raw: pd.Series, atr_ratio: pd.Series, threshold: float, k: float = 0.15
) -> pd.Series:
    """波动扩张分（v1 兼容）。"""
    x = (atr_ratio - threshold) / k
    gate = 1.0 / (1.0 + np.exp(-x))
    return dir_raw * 4 * gate
