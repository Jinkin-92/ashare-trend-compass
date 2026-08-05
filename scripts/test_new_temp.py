"""Test trend temperature v6 - z-score + drawdown hybrid, tuned for cycle extremes."""
import sys
sys.path.insert(0, '.')
from src.db import get_session
from sqlalchemy import text
import pandas as pd
import numpy as np
from collections import Counter

with get_session() as s:
    rows = s.execute(text(
        "SELECT trade_date, close, high, low FROM daily_price WHERE symbol_id='CONCEPT_309049' ORDER BY trade_date"
    )).all()

df = pd.DataFrame(rows, columns=['trade_date', 'close', 'high', 'low'])
df = df.dropna(subset=['close']).reset_index(drop=True)
close = df['close'].values
high = df['high'].values
n = len(close)


def rolling_ma(arr, w):
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.mean(arr[i - w + 1:i + 1])
    return s


def rolling_std(arr, w):
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.std(arr[i - w + 1:i + 1], ddof=1)
    return s


def rolling_max(arr, w):
    s = np.full(len(arr), np.nan)
    for i in range(w - 1, len(arr)):
        s[i] = np.max(arr[i - w + 1:i + 1])
    return s


def roc(arr, w):
    s = np.full(len(arr), np.nan)
    s[w:] = arr[w:] / arr[:-w] - 1
    return s


# === COMPONENT A: Z-score (40% weight) ===
z_scales = [(60, 0.25), (120, 0.35), (250, 0.40)]
z_composite = np.zeros(n)
z_details = {}
for w, wt in z_scales:
    m = rolling_ma(close, w)
    s = rolling_std(close, w)
    z = np.full(n, 0.0)
    valid = ~np.isnan(m) & ~np.isnan(s) & (s > 0)
    z[valid] = (close[valid] - m[valid]) / s[valid]
    z_indiv = np.tanh(z * 1.5)
    z_details[w] = z
    z_composite += z_indiv * wt

# === COMPONENT B: Drawdown (20% weight) ===
# How far from 250-day high? Captures cycle pain directly
dd_hi = rolling_max(high, 250)
dd = np.full(n, 0.0)
valid_dd = ~np.isnan(dd_hi)
dd[valid_dd] = (close[valid_dd] - dd_hi[valid_dd]) / (dd_hi[valid_dd] + 1e-9)
# dd ranges from 0 (at high) to -0.5 (50% drawdown)
# Map: dd=0→0, dd=-10%→-0.3, dd=-30%→-0.8, dd=-50%→-1.0
dd_score = np.tanh(dd * 4)

# === COMPONENT C: Multi-scale momentum (40% weight) ===
mom_scales = [(20, 0.20), (60, 0.35), (120, 0.45)]
mom_composite = np.zeros(n)
for w, wt in mom_scales:
    r = roc(close, w)
    r_score = np.nan_to_num(np.tanh(r * 1.5), 0)
    m = rolling_ma(close, w)
    half = max(5, w // 3)
    slope = np.full(n, 0.0)
    valid = np.arange(n) >= w + half
    m_shifted = np.roll(m, half)
    m_valid = valid & (~np.isnan(m_shifted))
    slope[m_valid] = (m[m_valid] - m_shifted[m_valid]) / (np.abs(m_shifted[m_valid]) + 1e-9)
    s_score = np.tanh(slope * 3)
    mom_composite += (r_score * 0.65 + s_score * 0.35) * wt

# Combined
composite = z_composite * 0.40 + dd_score * 0.20 + mom_composite * 0.40
raw_temp = composite * 100

# Level-dependent smoothing
smooth_temp = np.full(n, np.nan)
smooth_temp[0] = raw_temp[0] if not np.isnan(raw_temp[0]) else 0
for i in range(1, n):
    if np.isnan(raw_temp[i]):
        smooth_temp[i] = smooth_temp[i - 1]
        continue
    prev = smooth_temp[i - 1]
    abs_s = abs(prev) if not np.isnan(prev) else abs(raw_temp[i])
    if abs_s < 20:
        span = 4
    elif abs_s < 40:
        span = 7
    elif abs_s < 60:
        span = 12
    elif abs_s < 80:
        span = 18
    else:
        span = 25
    alpha = 2.0 / (span + 1)
    smooth_temp[i] = alpha * raw_temp[i] + (1 - alpha) * prev

# === STATE MACHINE ===
BUCKETS = [
    ('冻', -100, -60), ('寒', -60, -35), ('凉', -35, -12),
    ('平', -12, 12), ('温', 12, 40), ('热', 40, 65), ('沸', 65, 100)
]
LEVELS = ['冻', '寒', '凉', '平', '温', '热', '沸']


def raw_lv(s):
    for lbl, lo, hi in BUCKETS:
        if lo <= s < hi:
            return lbl
    return '沸' if s >= 65 else '冻'


MIN_STAY = {'冻': 12, '寒': 8, '凉': 5, '平': 3, '温': 5, '热': 8, '沸': 12}
CONFIRM = {'冻': 5, '寒': 4, '凉': 2, '平': 1, '温': 2, '热': 4, '沸': 5}

state_out = []
current = None
days_in_state = 0
pending_target = None
pending_count = 0

for i in range(n):
    s = smooth_temp[i]
    if np.isnan(s):
        state_out.append(None)
        continue
    rl = raw_lv(s)

    if current is None:
        current = rl
        days_in_state = 1
    else:
        days_in_state += 1
        if rl != current and days_in_state >= MIN_STAY[current]:
            if pending_target != rl:
                pending_target = rl
                pending_count = 1
            else:
                pending_count += 1
            if pending_count >= CONFIRM[current]:
                current = rl
                days_in_state = 0
                pending_target = None
                pending_count = 0
        elif rl == current:
            pending_target = None
            pending_count = 0

    state_out.append(current)

# === OUTPUT ===
print('=== CPO Trend Temperature v6 (z-score + drawdown + momentum) ===')
print(f'Scales: z=(60,120,250) 40% + drawdown(250d) 20% + mom=(20,60,120) 40%')
print()

target_dates = [
    ('24Q4', '2024-10-01'), ('25.01', '2025-01-02'), ('25.02', '2025-02-05'),
    ('25.03', '2025-03-03'), ('25.04', '2025-04-01'), ('25.05', '2025-05-06'),
    ('25.06', '2025-06-03'), ('25.07', '2025-07-01'), ('25.08', '2025-08-01'),
    ('25.09', '2025-09-01'), ('25.10', '2025-10-09'), ('25.11', '2025-11-03'),
    ('25.12', '2025-12-01'), ('26.01', '2026-01-05'), ('26.02', '2026-02-02'),
    ('26.03', '2026-03-02'), ('26.04', '2026-04-01'), ('26.05', '2026-05-06'),
    ('26.06', '2026-06-02'), ('26.07', '2026-07-14'), ('26.08', '2026-08-04'),
]

print(f'{"Mo":>6s} | {"Close":>7s} | {"Z":>6s} {"DD":>6s} {"Mom":>6s} | {"RawT":>6s} {"SmthT":>6s} | State')
print('-' * 70)
for label, td_str in target_dates:
    matches = df[df['trade_date'] >= td_str]
    if len(matches) == 0:
        continue
    row = matches.iloc[0]
    idx_val = row.name
    if idx_val < len(state_out) and state_out[idx_val]:
        rt = raw_temp[idx_val]
        st = smooth_temp[idx_val]
        print(f'{label:>6s} | {row.close:7.0f} | {z_composite[idx_val]:5.2f} | {dd_score[idx_val]:5.2f} | {mom_composite[idx_val]:5.2f} | {rt:5.1f} | {st:5.1f} | {state_out[idx_val]}')

print()
print('=== Key dates with z-scores ===')
for lbl, td_str in [('Low 1', '2025-04-07'), ('Low 2', '2025-04-08'), ('Low 3', '2025-04-10'),
                     ('Peak', '2026-06-30'), ('Now', '2026-08-04')]:
    matches = df[df['trade_date'] >= td_str]
    if len(matches) == 0:
        continue
    row = matches.iloc[0]
    idx_val = row.name
    z60 = z_details.get(60, np.zeros(n))[idx_val]
    z120 = z_details.get(120, np.zeros(n))[idx_val]
    z250 = z_details.get(250, np.zeros(n))[idx_val]
    print(f'  {lbl} ({row.trade_date}): close={row.close:.0f} z60={z60:.2f} z120={z120:.2f} z250={z250:.2f} dd={dd[idx_val]*100:.1f}% state={state_out[idx_val]}')

print()
print('=== State Transitions ===')
prev = None
for i, s in enumerate(state_out):
    if s and s != prev:
        print(f'  {df.iloc[i]["trade_date"]}: {str(prev):>4s} → {s:>4s}  close={df.iloc[i]["close"]:.0f}')
        prev = s

print()
print('=== Distribution ===')
c = Counter([s for s in state_out if s])
total = sum(c.values())
for lv in LEVELS:
    cnt = c.get(lv, 0)
    bar = '█' * int(cnt * 40 / max(total, 1))
    print(f'  {lv}: {cnt:4d}d ({cnt / total * 100:5.1f}%) {bar}')
print(f'  Transitions: {sum(1 for i in range(1,len(state_out)) if state_out[i] and state_out[i]!=state_out[i-1])}')
