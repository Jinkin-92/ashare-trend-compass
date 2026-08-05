# -*- coding: utf-8 -*-
"""非线性拉伸评分 + 三个品种完整温度轨迹测试"""

import sys
sys.path.insert(0, '.')
import numpy as np
from collections import Counter
from src.db import get_session
from sqlalchemy import text

# ============================================================
# 底层计算（不变）
# ============================================================
def rolling_min(arr, w):
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.min(arr[i - w + 1:i + 1])
    return s

def rolling_max(arr, w):
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.max(arr[i - w + 1:i + 1])
    return s

def rolling_mean(arr, w):
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.mean(arr[i - w + 1:i + 1])
    return s

def rolling_std(arr, w):
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.std(arr[i - w + 1:i + 1], ddof=1)
    return s

# ============================================================
# 非线性拉伸函数 — 核心改动
# ============================================================
# 原公式: scaled = raw_temp * 65 → 理论范围 [-65, 65]，实际 [-36, 50]
# 新公式: 分段幂律拉伸，零附近近线性，两端加速膨胀
#
# pos: y = x * 65 * (1 + 1.2 * x²)    → max 0.77→85
# neg: y = x * 65 * (1 + 3.0 * x²)    → max -0.55→-68
#
# 再通过 erf 做一次平滑非线性映射到 [-100, 100]
def stretch_score(raw_temp):
    """对 [-1, 1] 的 raw_temp 做拉伸到 [-100, 100]"""
    result = np.zeros_like(raw_temp)
    
    mask_pos = raw_temp > 0
    mask_neg = raw_temp < 0
    mask_zero = raw_temp == 0
    
    # 正向拉伸：cubic amplification
    result[mask_pos] = raw_temp[mask_pos] * 65 * (1 + 1.2 * raw_temp[mask_pos]**2)
    
    # 负向拉伸：更强的 cubic amplification
    result[mask_neg] = raw_temp[mask_neg] * 65 * (1 + 3.0 * raw_temp[mask_neg]**2)
    
    # 零保持零
    result[mask_zero] = 0.0
    
    return result

# 重新标定的阈值（映射到 [-100, 100] 区间）
THRESHOLDS = [
    (-100, -65, '冻'), (-65, -35, '寒'), (-35, -12, '凉'),
    (-12, 15, '平'), (15, 45, '温'), (45, 70, '热'), (70, 100, '沸'),
]
LEVEL_ORDER = ['冻', '寒', '凉', '平', '温', '热', '沸']
LEVEL_IDX = {l: i for i, l in enumerate(LEVEL_ORDER)}
CONFIRM = {
    '沸': {'enter': 3, 'exit': 6}, '冻': {'enter': 4, 'exit': 8},
    '热': {'enter': 3, 'exit': 5}, '寒': {'enter': 3, 'exit': 5},
    '温': {'enter': 2, 'exit': 3}, '凉': {'enter': 2, 'exit': 3},
    '平': {'enter': 1, 'exit': 1},
}

def raw_level(s):
    for lo, hi, label in THRESHOLDS:
        if lo <= s < hi:
            return label
    return '沸' if s >= 70 else '冻'

def compute_temperature_full(close, high, low):
    n = len(close)
    
    # 1) Z-score (40%)
    windows_z = [60, 120, 250]
    z_weights = [0.35, 0.35, 0.30]
    z_composite = np.zeros(n)
    z_weights_used = np.zeros(n)
    for wi, w in enumerate(windows_z):
        mu = rolling_mean(close, w)
        sigma = rolling_std(close, w)
        valid = (~np.isnan(mu)) & (~np.isnan(sigma)) & (sigma > 0)
        z = np.full(n, 0.0)
        z[valid] = (close[valid] - mu[valid]) / sigma[valid]
        z = np.clip(z, -3.5, 3.5)
        z_composite[valid] += z[valid] * z_weights[wi]
        z_weights_used[valid] += z_weights[wi]
    valid_w = z_weights_used > 0
    z_composite[valid_w] /= z_weights_used[valid_w]
    z_score = np.tanh(z_composite * 0.8)

    # 2) 回撤深度 (20%)
    hi250 = rolling_max(high, 250)
    dd_pct = np.full(n, 0.0)
    valid_dd = ~np.isnan(hi250)
    dd_pct[valid_dd] = (close[valid_dd] - hi250[valid_dd]) / (hi250[valid_dd] + 1e-9)
    dd_score = np.tanh(dd_pct * 3.5)

    # 3) 多尺度动量 (40%)
    mom_windows = [20, 60, 120]
    mom_weights = [0.45, 0.30, 0.25]
    mom_composite = np.zeros(n)
    mom_w_used = np.zeros(n)
    for wi, w in enumerate(mom_windows):
        roc = np.full(n, np.nan)
        roc[w:] = close[w:] / close[:-w] - 1.0
        roc = np.clip(roc, -0.5, 0.5)
        roc_score = np.tanh(roc * 4.0)
        mf_w = max(5, w // 4)
        ma_f = rolling_mean(close, mf_w)
        ma_s = rolling_mean(close, w)
        valid_ma = (~np.isnan(ma_f)) & (~np.isnan(ma_s)) & (ma_s > 0)
        ma_ratio = np.full(n, 0.0)
        ma_ratio[valid_ma] = (ma_f[valid_ma] / ma_s[valid_ma] - 1.0)
        ma_ratio = np.clip(ma_ratio, -0.15, 0.15)
        ma_score = np.tanh(ma_ratio * 15)
        scale_score = roc_score * 0.6 + ma_score * 0.4
        mask = ~np.isnan(scale_score)
        mom_composite[mask] += scale_score[mask] * mom_weights[wi]
        mom_w_used[mask] += mom_weights[wi]
    valid_m = mom_w_used > 0
    mom_composite[valid_m] /= mom_w_used[valid_m]

    # 加权合成
    raw_temp = z_score * 0.40 + dd_score * 0.20 + mom_composite * 0.40
    
    # ★ 非线性拉伸（唯一改动）
    raw_temp_scaled = stretch_score(raw_temp)

    # 自适应平滑（极端档位更粘滞）
    smooth_temp = np.full(n, np.nan)
    first_valid = np.where(~np.isnan(raw_temp_scaled))[0]
    if len(first_valid) == 0:
        return [None]*n, raw_temp_scaled, np.full(n, np.nan)
    smooth_temp[first_valid[0]] = raw_temp_scaled[first_valid[0]]
    for i in range(first_valid[0] + 1, n):
        if np.isnan(raw_temp_scaled[i]):
            smooth_temp[i] = smooth_temp[i - 1]
            continue
        abs_score = abs(smooth_temp[i - 1]) if not np.isnan(smooth_temp[i - 1]) else abs(raw_temp_scaled[i])
        if abs_score < 15: span = 3
        elif abs_score < 30: span = 5
        elif abs_score < 55: span = 8
        else: span = 12
        alpha = 2.0 / (span + 1)
        smooth_temp[i] = alpha * raw_temp_scaled[i] + (1 - alpha) * smooth_temp[i - 1]

    # 状态机
    state = [None] * n
    current = None
    pending_level = None
    pending_days = 0
    for i in range(n):
        s = smooth_temp[i]
        if np.isnan(s):
            state[i] = current
            continue
        rl = raw_level(s)
        if current is None:
            current = rl
        elif rl != current:
            if pending_level != rl:
                pending_level = rl
                pending_days = 1
            else:
                pending_days += 1
            cur_idx = LEVEL_IDX[current]
            tgt_idx = LEVEL_IDX[rl]
            if abs(cur_idx - 3) > abs(tgt_idx - 3):
                need = CONFIRM[current]['exit']
            else:
                need = CONFIRM[rl]['enter']
            if pending_days >= need:
                current = rl
                pending_level = None
                pending_days = 0
        else:
            pending_level = None
            pending_days = 0
        state[i] = current
    return state, raw_temp_scaled, smooth_temp

# ============================================================
# 测试三个品种
# ============================================================
targets = [
    ('CONCEPT_309049', 'CPO概念'),
    ('SW_801081', '半导体'),
    ('SW_801053', '贵金属(黄金)'),
]

for sid, name in targets:
    print(f'\n{"="*70}')
    print(f'  {name} ({sid})')
    print(f'{"="*70}')
    
    with get_session() as s:
        rows = s.execute(text(
            'SELECT trade_date, close, high, low FROM daily_price WHERE symbol_id=:sid ORDER BY trade_date'
        ), {'sid': sid}).all()
    
    clean = [r for r in rows if r[1] is not None and r[2] is not None and r[3] is not None]
    close = np.array([float(r[1]) for r in clean], dtype=float)
    high = np.array([float(r[2]) for r in clean], dtype=float)
    low = np.array([float(r[3]) for r in clean], dtype=float)
    dates = [r[0] for r in clean]
    
    state, raw, smooth = compute_temperature_full(close, high, low)
    
    # 状态分布
    c = Counter([s for s in state if s])
    total = sum(c.values())
    print(f'\n  状态分布 ({total} 天):')
    for lv in LEVEL_ORDER:
        bar = '█' * int(c.get(lv, 0) / max(total / 30, 1))
        print(f'  {lv:2s} {c.get(lv,0):4d} 天 ({c.get(lv,0)/total*100:5.1f}%) {bar}')
    
    # 极值
    valid_raw = raw[200:]
    max_idx = np.nanargmax(valid_raw) + 200
    min_idx = np.nanargmin(valid_raw) + 200
    print(f'\n  最高: raw={raw[max_idx]:.1f} sm={smooth[max_idx]:.1f} 日期={dates[max_idx]} 价格={close[max_idx]:.0f}')
    print(f'  最低: raw={raw[min_idx]:.1f} sm={smooth[min_idx]:.1f} 日期={dates[min_idx]} 价格={close[min_idx]:.0f}')
    
    # 状态转换
    print(f'\n  完整周期转换:')
    prev = None
    transitions = []
    for i, st in enumerate(state):
        if st and st != prev:
            transitions.append((dates[i], prev, st, close[i], smooth[i]))
            prev = st
    
    for d, fr, to, c, sm in transitions:
        print(f'  {d}: {str(fr):>5s} → {to:2s}  @{c:.0f}  sm={sm:.1f}')
    
    # 月度快照（最近18个月）
    print(f'\n  月度快照:')
    for yr in [2025, 2026]:
        for m in range(1, 13):
            for d in [28, 25, 20, 15, 10, 5, 1]:
                tgt = f'{yr}-{m:02d}-{d:02d}'
                matches = [i for i, dt in enumerate(dates) if str(dt) >= tgt]
                if matches:
                    idx = matches[0]
                    if idx < len(state) and state[idx]:
                        if m % 3 == 1 or state[idx] in ('沸', '冻', '寒'):
                            print(f'  {yr}-{m:02d}: {state[idx]:2s}  close={close[idx]:.0f}  raw={raw[idx]:.1f}  sm={smooth[idx]:.1f}')
                    break
    
    # 价格关键节点
    print(f'\n  关键周期价格:')
    # Find first and last
    first_idx = 200
    last_idx = len(close) - 1
    chg = (close[last_idx] / close[first_idx] - 1) * 100
    print(f'  区间: {dates[first_idx]} ~ {dates[last_idx]}  涨跌: {chg:+.1f}%')
    print(f'  价格范围: {close[first_idx]:.0f} ~ {close[last_idx]:.0f}')

# 汇总对比
print(f'\n\n{"="*70}')
print(f'  三品种对比总结')
print(f'{"="*70}')
for sid, name in targets:
    with get_session() as s:
        rows = s.execute(text(
            'SELECT trade_date, close, high, low FROM daily_price WHERE symbol_id=:sid ORDER BY trade_date'
        ), {'sid': sid}).all()
    clean = [r for r in rows if r[1] is not None]
    close = np.array([float(r[1]) for r in clean], dtype=float)
    high = np.array([float(r[2]) for r in clean], dtype=float)
    low = np.array([float(r[3]) for r in clean], dtype=float)
    dates = [r[0] for r in clean]
    
    state, raw, smooth = compute_temperature_full(close, high, low)
    c = Counter([s for s in state if s])
    total = sum(c.values())
    
    valid_raw = raw[200:]
    p50_val = np.nanpercentile(valid_raw, 50)
    
    print(f'\n  {name}:')
    print(f'    沸状态: {c.get("沸",0)} 天 ({c.get("沸",0)/total*100:.1f}%)')
    print(f'    冻状态: {c.get("冻",0)} 天 ({c.get("冻",0)/total*100:.1f}%)')
    print(f'    最高评分: {np.nanmax(valid_raw):.1f}')
    print(f'    最低评分: {np.nanmin(valid_raw):.1f}')
    print(f'    中位评分: {p50_val:.1f}')
    print(f'    价格涨跌: {(close[-1]/close[200]-1)*100:+.1f}%')
