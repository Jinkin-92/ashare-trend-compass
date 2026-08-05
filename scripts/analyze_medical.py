# -*- coding: utf-8 -*-
"""应用新多尺度 Z-score 框架分析医疗服务 (SW_801156)。"""
import sys
sys.path.insert(0, '.')
import numpy as np
import pandas as pd
from src.db import get_session
from sqlalchemy import text

SID = 'SW_801156'

# ── 加载数据 ─────────────────────────────────────────────
with get_session() as s:
    rows = s.execute(text(
        'SELECT trade_date, close, high, low FROM daily_price WHERE symbol_id=:sid ORDER BY trade_date'
    ), {'sid': SID}).all()

df = pd.DataFrame(rows, columns=['trade_date', 'close', 'high', 'low'])
close = df['close'].values
high = df['high'].values
low = df['low'].values
n = len(close)
print(f'医疗服务 ({SID})')
print(f'数据范围: {df.trade_date.iloc[0]} ~ {df.trade_date.iloc[-1]} 共 {n} 天')
print(f'最新收盘: {close[-1]:.2f} ({df.trade_date.iloc[-1]})')

# ── 新框架计算 ─────────────────────────────────────────────

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

# ── 组件 1: 滚动 Z-score（40% 权重）─────────────────────────
windows_z = [60, 120, 250]
z_weights = [0.35, 0.35, 0.30]  # 长周期略低（数据可能不足）
z_scores_all = np.zeros((n, len(windows_z)))

for wi, w in enumerate(windows_z):
    mu = rolling_mean(close, w)
    sigma = rolling_std(close, w)
    valid = (~np.isnan(mu)) & (~np.isnan(sigma)) & (sigma > 0)
    z = np.full(n, np.nan)
    z[valid] = (close[valid] - mu[valid]) / sigma[valid]
    z = np.clip(z, -3.5, 3.5)
    z_scores_all[:, wi] = z

# 加权合并
z_composite = np.zeros(n)
z_weights_used = np.zeros(n)
for wi, w in enumerate(windows_z):
    mask = ~np.isnan(z_scores_all[:, wi])
    z_composite[mask] += z_scores_all[mask, wi] * z_weights[wi]
    z_weights_used[mask] += z_weights[wi]

# 对不足 250 天部分重新归一化
valid_w = z_weights_used > 0
z_composite[valid_w] /= z_weights_used[valid_w]

# 缩放到约 [-1, 1]
z_score = np.tanh(z_composite * 0.8)

# ── 组件 2: 回撤深度（20% 权重）────────────────────────────
hi250 = rolling_max(high, 250)  # 250日高点
dd = np.full(n, 0.0)
valid_dd = ~np.isnan(hi250)
dd[valid_dd] = (close[valid_dd] - hi250[valid_dd]) / (hi250[valid_dd] + 1e-9)
# 映射：0% → 0, -10% → -0.25, -30% → -0.65, -50% → -0.95
dd_score = np.tanh(dd * 3.5)

# ── 组件 3: 多尺度动量（40% 权重）──────────────────────────
mom_windows = [20, 60, 120]
mom_weights = [0.45, 0.30, 0.25]

mom_composite = np.zeros(n)
mom_w_used = np.zeros(n)

for wi, w in enumerate(mom_windows):
    roc = np.full(n, np.nan)
    roc[w:] = close[w:] / close[:-w] - 1.0
    roc = np.clip(roc, -0.5, 0.5)
    roc_score = np.tanh(roc * 4.0)

    ma_f = rolling_mean(close, max(5, w // 4))
    ma_s = rolling_mean(close, w)
    valid_ma = (~np.isnan(ma_f)) & (~np.isnan(ma_s)) & (ma_s > 0)
    ma_ratio = np.full(n, 0.0)
    ma_ratio[valid_ma] = (ma_f[valid_ma] / ma_s[valid_ma] - 1.0)
    ma_ratio = np.clip(ma_ratio, -0.15, 0.15)
    ma_score = np.tanh(ma_ratio * 15)

    scale_score = roc_score * 0.6 + ma_score * 0.4
    mask = ~np.isnan(scale_score)
    scale_score[~mask] = 0.0
    mom_composite[mask] += scale_score[mask] * mom_weights[wi]
    mom_w_used[mask] += mom_weights[wi]

valid_m = mom_w_used > 0
mom_composite[valid_m] /= mom_w_used[valid_m]

# ── 最终合成 ───────────────────────────────────────────────
raw_temp = z_score * 0.40 + dd_score * 0.20 + mom_composite * 0.40

# 放大到评分范围 [-65, 65]
raw_temp_scaled = raw_temp * 65

# Level-dependent EMA 平滑
smooth_temp = np.full(n, np.nan)
smooth_temp[0] = raw_temp_scaled[0] if not np.isnan(raw_temp_scaled[0]) else 0

for i in range(1, n):
    if np.isnan(raw_temp_scaled[i]):
        smooth_temp[i] = smooth_temp[i-1]
        continue
    abs_score = abs(smooth_temp[i-1]) if not np.isnan(smooth_temp[i-1]) else abs(raw_temp_scaled[i])
    if abs_score < 10:
        span = 3
    elif abs_score < 25:
        span = 5
    elif abs_score < 40:
        span = 8
    else:
        span = 12
    alpha = 2.0 / (span + 1)
    smooth_temp[i] = alpha * raw_temp_scaled[i] + (1 - alpha) * smooth_temp[i-1]

# ── 新阈值 ─────────────────────────────────────────────────
THRESHOLDS = [
    (-65, -45, '冻'),
    (-45, -25, '寒'),
    (-25, -10, '凉'),
    (-10, 12, '平'),
    (12, 35, '温'),
    (35, 55, '热'),
    (55, 65, '沸'),
]

def raw_level(s):
    for lo, hi, label in THRESHOLDS:
        if lo <= s < hi:
            return label
    return '沸' if s >= 55 else '冻'

LEVEL_ORDER = ['冻', '寒', '凉', '平', '温', '热', '沸']
LEVEL_IDX = {l: i for i, l in enumerate(LEVEL_ORDER)}

# ── 状态机 ─────────────────────────────────────────────────
CONFIRM = {
    '沸': {'enter': 4, 'exit': 8},
    '冻': {'enter': 4, 'exit': 8},
    '热': {'enter': 3, 'exit': 5},
    '寒': {'enter': 3, 'exit': 5},
    '温': {'enter': 2, 'exit': 3},
    '凉': {'enter': 2, 'exit': 3},
    '平': {'enter': 1, 'exit': 1},
}

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
        pending_level = None
        pending_days = 0
    elif rl != current:
        if pending_level != rl:
            pending_level = rl
            pending_days = 1
        else:
            pending_days += 1

        cur_idx = LEVEL_IDX[current]
        tgt_idx = LEVEL_IDX[rl]

        # 离开极端档更严格
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

# ── 输出 ───────────────────────────────────────────────────
print()
print(f"{'日期':<12} {'收盘':>8} {'Z60':>6} {'Z120':>6} {'Z250':>6} {'DD%':>6} {'MOM':>6} {'RAW':>7} {'平滑':>7} {'状态':>4}")
print('-' * 85)

# 最近 30 天
for i in range(max(0, n - 30), n):
    d = df.trade_date.iloc[i]
    c = close[i]
    z60 = z_scores_all[i, 0] if not np.isnan(z_scores_all[i, 0]) else float('nan')
    z120 = z_scores_all[i, 1] if not np.isnan(z_scores_all[i, 1]) else float('nan')
    z250 = z_scores_all[i, 2] if not np.isnan(z_scores_all[i, 2]) else float('nan')
    dd_v = dd[i] * 100 if not np.isnan(dd[i]) else float('nan')
    mom_v = mom_composite[i]
    raw_v = raw_temp_scaled[i]
    smooth_v = smooth_temp[i]
    st = state[i] or '--'
    mark = ' ◀' if i == n - 1 else ''
    print(f'{d:<12} {c:>8.2f} {z60:>6.2f} {z120:>6.2f} {z250:>6.2f} {dd_v:>5.1f} {mom_v:>6.3f} {raw_v:>7.1f} {smooth_v:>7.1f} {st:>4}{mark}')

# ── 状态变迁历史 ───────────────────────────────────────────
print()
print('─── 状态变迁 ───')
prev_st = None
for i in range(n):
    st = state[i]
    if st and st != prev_st:
        d = df.trade_date.iloc[i]
        print(f'  {d}: {prev_st or "NONE"} → {st} (close={close[i]:.2f})')
        prev_st = st

# ── 阶段分布 ──────────────────────────────────────────────
print()
print('─── 全局阶段分布 ───')
from collections import Counter
cnt = Counter([s for s in state if s])
total = sum(cnt.values())
for lv in LEVEL_ORDER:
    c = cnt.get(lv, 0)
    bar = '█' * int(c / total * 50)
    print(f'  {lv}: {c:4d} 天 ({c/total*100:5.1f}%) {bar}')
print(f'  共 {total} 个交易日')

# ── 当前阶段详情 ──────────────────────────────────────────
print()
print('─── 当前状态详情 ───')
idx = n - 1
print(f'  日期: {df.trade_date.iloc[idx]}')
print(f'  收盘: {close[idx]:.2f}')
print(f'  当前温度状态: {state[idx]}')
print(f'  原始评分: {raw_temp_scaled[idx]:.2f}')
print(f'  平滑评分: {smooth_temp[idx]:.2f}')
print(f'  Z60: {z_scores_all[idx,0]:.2f}  Z120: {z_scores_all[idx,1]:.2f}  Z250: {z_scores_all[idx,2]:.2f}')
print(f'  250日回撤: {dd[idx]*100:.1f}%')
print(f'  动量分: {mom_composite[idx]:.3f}')
print(f'  Z-score 分: {z_score[idx]:.3f}')
print(f'  回撤分: {dd_score[idx]:.3f}')

# 判断方向
if state[idx] in ('冻', '寒', '凉'):
    direction = '弱势'
elif state[idx] in ('温', '热', '沸'):
    direction = '强势'
else:
    direction = '中性'

# 看趋势是在变好还是变坏
if idx >= 20:
    recent_raw = raw_temp_scaled[idx - 20:idx + 1]
    slope = np.polyfit(range(21), recent_raw, 1)[0]
    if slope > 0.2:
        trend = '↑ 改善中'
    elif slope < -0.2:
        trend = '↓ 恶化中'
    else:
        trend = '→ 走平'
else:
    trend = '数据不足'

print(f'  趋势方向: {direction} ({trend})')
