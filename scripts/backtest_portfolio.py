# -*- coding: utf-8 -*-
"""组合层级回测：市场宽度仓位控制 + 沸区加仓 + 移动止盈。

1. 市场宽度（温+热品种占比）映射总仓位 30%~60%
2. 信号：温→热（combo_A过滤器）
3. 沸区加仓：进入沸时追加 50% 仓位
4. 退出：热→温 | 移动止盈（距高点-8%）| 安全网（凉/寒/冻）
"""

import sys
sys.path.insert(0, '.')
import numpy as np
import pandas as pd
from collections import defaultdict
from src.db import get_session
from sqlalchemy import text

# ============================================================
# 辅助函数
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

THRESHOLDS = [
    (-65, -45, '冻'), (-45, -25, '寒'), (-25, -10, '凉'),
    (-10, 12, '平'), (12, 35, '温'), (35, 55, '热'), (55, 65, '沸'),
]
LEVEL_ORDER = ['冻', '寒', '凉', '平', '温', '热', '沸']
LEVEL_IDX = {l: i for i, l in enumerate(LEVEL_ORDER)}
CONFIRM = {
    '沸': {'enter': 4, 'exit': 8}, '冻': {'enter': 4, 'exit': 8},
    '热': {'enter': 3, 'exit': 5}, '寒': {'enter': 3, 'exit': 5},
    '温': {'enter': 2, 'exit': 3}, '凉': {'enter': 2, 'exit': 3},
    '平': {'enter': 1, 'exit': 1},
}

def raw_level(s):
    for lo, hi, label in THRESHOLDS:
        if lo <= s < hi:
            return label
    return '沸' if s >= 55 else '冻'

def compute_temperature_full(close, high, low):
    n = len(close)
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

    hi250 = rolling_max(high, 250)
    dd_pct = np.full(n, 0.0)
    valid_dd = ~np.isnan(hi250)
    dd_pct[valid_dd] = (close[valid_dd] - hi250[valid_dd]) / (hi250[valid_dd] + 1e-9)
    dd_score = np.tanh(dd_pct * 3.5)

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
    raw_temp = z_score * 0.40 + dd_score * 0.20 + mom_composite * 0.40
    raw_temp_scaled = raw_temp * 65

    smooth_temp = np.full(n, np.nan)
    smooth_temp[0] = raw_temp_scaled[0] if not np.isnan(raw_temp_scaled[0]) else 0.0
    for i in range(1, n):
        if np.isnan(raw_temp_scaled[i]):
            smooth_temp[i] = smooth_temp[i - 1]
            continue
        abs_score = abs(smooth_temp[i - 1]) if not np.isnan(smooth_temp[i - 1]) else abs(raw_temp_scaled[i])
        if abs_score < 10: span = 3
        elif abs_score < 25: span = 5
        elif abs_score < 40: span = 8
        else: span = 12
        alpha = 2.0 / (span + 1)
        smooth_temp[i] = alpha * raw_temp_scaled[i] + (1 - alpha) * smooth_temp[i - 1]

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
# 信号预计算
# ============================================================
print('=' * 70)
print('  组合层级回测：仓位控制 + 沸区加仓 + 移动止盈')
print('=' * 70)

with get_session() as s:
    symb_rows = s.execute(text("SELECT symbol_id, name FROM symbols WHERE node_type='industry_l2'")).all()
symb_ids = [r[0] for r in symb_rows]
symb_names = {r[0]: r[1] for r in symb_rows}

print(f'\n[1/3] 计算 {len(symb_ids)} 个品种的温度状态...')
all_states = {}      # sid -> list of (date, state_label, raw_temp)
all_signals = []     # (date, sid, name, close, signal_score)

skipped = 0
for idx, sid in enumerate(symb_ids):
    with get_session() as s:
        rows = s.execute(text(
            'SELECT trade_date, close, high, low FROM daily_price WHERE symbol_id=:sid ORDER BY trade_date'
        ), {'sid': sid}).all()
    if len(rows) < 200:
        skipped += 1
        continue
    dates = [r[0] for r in rows]
    close = np.array([r[1] for r in rows], dtype=float)
    high  = np.array([r[2] for r in rows], dtype=float)
    low   = np.array([r[3] for r in rows], dtype=float)

    state, raw_vals, smooth = compute_temperature_full(close, high, low)

    # 记录每日状态
    st_list = []
    warm_start = None
    for i in range(len(dates)):
        d = str(dates[i])
        st_list.append((d, state[i], float(close[i]), float(raw_vals[i])))
        # 信号：温→热
        if i > 0 and state[i-1] == '温' and state[i] == '热':
            # combo_A 过滤器
            sig_score = raw_vals[i]
            if sig_score < 38:
                continue
            # 从 close 计算回撤（需要250日数据）
            hi250 = np.max(close[max(0,i-250):i+1])
            dd = (close[i] - hi250) / (hi250 + 1e-9)
            if dd < -0.25:
                continue
            # 温段持续天数
            if warm_start is None:
                warm_start = i - 5  # fallback
            warm_dur = i - warm_start
            if warm_dur < 5:
                continue
            all_signals.append((d, sid, symb_names[sid], float(close[i]), float(sig_score)))

        # 追踪温段起点
        if i > 0 and state[i] == '温' and state[i-1] != '温':
            warm_start = i

    all_states[sid] = st_list
    if (idx + 1) % 20 == 0:
        print(f'  ... {idx+1}/{len(symb_ids)} done')

print(f'  有效品种: {len(all_states)} (跳过 {skipped})')
print(f'  信号总数: {len(all_signals)}')

# ============================================================
# 组合模拟
# ============================================================
print(f'\n[2/3] 组合模拟...')

# 构建统一时间线
all_dates_set = set()
for sid, st_list in all_states.items():
    for d, _, _, _ in st_list:
        all_dates_set.add(d)
all_dates = sorted(all_dates_set)

# 构建快速查询：date -> {sid: (state, close)}
date_to_states = defaultdict(dict)
for sid, st_list in all_states.items():
    for d, s, c, _ in st_list:
        date_to_states[d][sid] = (s, c)

# 构建信号查询：date -> list of (sid, name, close)
date_to_signals = defaultdict(list)
for d, sid, name, c, sc in sorted(all_signals, key=lambda x: x[0]):
    date_to_signals[d].append((sid, name, c, sc))

# 组合参数
TRAILING_STOP = 0.08          # 距高点回撤 8% 止盈
SCALE_IN_RATIO = 0.50         # 沸区加仓：追加原仓位的 50%
COST_BPS = 20                 # 双边千2摩擦
MAX_SINGLE_WEIGHT = 0.15      # 单品种最大权重 15%
BREADTH_HIGH = 0.50           # >=50% → 60% 仓位
BREADTH_MID = 0.20            # >=20% → 30% 仓位
ALLOC_HIGH = 0.60
ALLOC_MID = 0.30

# 持仓：{sid: {entry_date, entry_price, peak_price, weight, scaled_in_this_round, state}}
positions = {}
total_sectors = len(all_states)

# 记录
daily_equity = []   # (date, nav, exposure, breadth, n_positions)
trade_log = []      # list of trade records

capital = 100.0     # 初始 100

capital_before = capital  # for period returns

for di, date in enumerate(all_dates):
    states_today = date_to_states.get(date, {})
    signals_today = date_to_signals.get(date, [])

    # ---- 1. 市场宽度 ----
    warm_hot_count = 0
    valid_count = 0
    for sid in all_states:
        if sid in states_today:
            s = states_today[sid][0]
            if s in ('温', '热', '沸'):
                warm_hot_count += 1
            valid_count += 1
    breadth = warm_hot_count / max(valid_count, 1)

    if breadth >= BREADTH_HIGH:
        target_alloc = ALLOC_HIGH
    elif breadth >= BREADTH_MID:
        target_alloc = ALLOC_MID
    else:
        target_alloc = 0.0  # 不新开仓，但保留现有

    # ---- 2. 更新现有持仓峰值，检查移动止盈 ----
    exited_today = []
    for sid in list(positions.keys()):
        pos = positions[sid]
        if sid in states_today:
            st, c_val = states_today[sid]
            c = float(c_val)
            pos['state'] = st
            pos['peak_price'] = max(float(pos['peak_price']), c)

            # 移动止盈
            if c < pos['peak_price'] * (1 - TRAILING_STOP):
                ret = (c / pos['entry_price'] - 1.0) * 100 - COST_BPS / 100.0
                trade_log.append({
                    'sid': sid, 'name': symb_names[sid],
                    'entry_date': pos['entry_date'], 'exit_date': date,
                    'entry_price': pos['entry_price'], 'exit_price': c,
                    'return_pct': ret, 'weight': pos['weight'],
                    'exit_reason': f'止盈-{TRAILING_STOP*100:.0f}%',
                    'scaled': pos.get('scaled_in_this_round', False),
                })
                exited_today.append(sid)

            # 安全网
            elif st in ('凉', '寒', '冻') and pos['state_prev'] in ('温', '热', '沸'):
                ret = (c / pos['entry_price'] - 1.0) * 100 - COST_BPS / 100.0
                trade_log.append({
                    'sid': sid, 'name': symb_names[sid],
                    'entry_date': pos['entry_date'], 'exit_date': date,
                    'entry_price': pos['entry_price'], 'exit_price': c,
                    'return_pct': ret, 'weight': pos['weight'],
                    'exit_reason': '安全网',
                    'scaled': pos.get('scaled_in_this_round', False),
                })
                exited_today.append(sid)

            # 热→温 退出
            elif pos['state_prev'] == '热' and st == '温':
                ret = (c / pos['entry_price'] - 1.0) * 100 - COST_BPS / 100.0
                trade_log.append({
                    'sid': sid, 'name': symb_names[sid],
                    'entry_date': pos['entry_date'], 'exit_date': date,
                    'entry_price': pos['entry_price'], 'exit_price': c,
                    'return_pct': ret, 'weight': pos['weight'],
                    'exit_reason': '热→温',
                    'scaled': pos.get('scaled_in_this_round', False),
                })
                exited_today.append(sid)

    # 移除已退出
    for sid in exited_today:
        del positions[sid]

    # ---- 3. 沸区加仓 ----
    for sid in list(positions.keys()):
        pos = positions[sid]
        if pos.get('state') == '沸' and not pos.get('scaled_in_this_round', False):
            # 加仓 50%
            add_w = pos['weight'] * SCALE_IN_RATIO
            # 从其他持仓等比例缩减来腾空间
            other_sids = [s for s in positions if s != sid]
            total_other_w = sum(positions[s]['weight'] for s in other_sids)
            if total_other_w > 0.01:
                for s in other_sids:
                    positions[s]['weight'] -= positions[s]['weight'] / total_other_w * add_w
                pos['weight'] += add_w
                pos['scaled_in_this_round'] = True
                pos['scale_add_price'] = float(states_today[sid][1]) if sid in states_today else pos['entry_price']

    # ---- 4. 记录状态（用于次日判断状态转移）----
    for sid in positions:
        if sid in states_today:
            positions[sid]['state_prev'] = states_today[sid][0]

    # ---- 5. 新开仓（信号触发） ----
    if target_alloc > 0 and signals_today:
        total_exposure = sum(p['weight'] for p in positions.values())
        remaining = target_alloc - total_exposure

        # 按信号强度排序，优先开最强的
        signals_today.sort(key=lambda x: x[3], reverse=True)

        for sig_sid, sig_name, sig_close, sig_score in signals_today:
            if remaining <= 0.005:
                break
            if sig_sid in positions:
                continue  # 已持仓

            # 每次开仓权重
            n_pos = len(positions) + 1
            single_w = min(target_alloc / max(n_pos, 1), MAX_SINGLE_WEIGHT)
            single_w = min(single_w, remaining)

            if single_w < 0.01:
                continue

            # 从现有持仓等比例缩减
            if len(positions) > 0:
                total_existing = sum(p['weight'] for p in positions.values())
                if total_existing > 0.001:
                    scale_down = (total_existing + single_w - target_alloc) / total_existing
                    if scale_down > 0:
                        for s in positions:
                            positions[s]['weight'] *= (1 - scale_down)

            positions[sig_sid] = {
                'entry_date': date,
                'entry_price': sig_close,
                'peak_price': sig_close,
                'weight': single_w,
                'state': states_today[sig_sid][0] if sig_sid in states_today else '热',
                'state_prev': states_today[sig_sid][0] if sig_sid in states_today else '热',
                'scaled_in_this_round': False,
            }

            remaining = target_alloc - sum(p['weight'] for p in positions.values())

    # ---- 6. 计算当日收益 ----
    daily_ret = 0.0
    for sid, pos in positions.items():
        if sid in states_today:
            c_today = states_today[sid][1]
            # 需要昨日价格
            # 使用 entry_price 作为基础（简化：用持仓收益率估算）
            # 实际组合日收益 = sum(weight_i * (close_today/close_yesterday - 1))
            # 但我们没有存昨收。用 entry_price 算累计再微分不可靠。
            # 改为记录每日 close，diff 算日收益。
            pass

    # ---- 记录快照 ----
    exposure = sum(p['weight'] for p in positions.values())
    daily_equity.append((date, capital, exposure, breadth, len(positions)))

# ---- 日收益率链式计算 ----
# 需要每日 close 来算组合日收益。重新遍历。
# 存储每个品种的每日 close
sid_daily_close = {}
for sid, st_list in all_states.items():
    sid_daily_close[sid] = {}
    for d, s, c, _ in st_list:
        sid_daily_close[sid][d] = c

# 重新跑一遍，记录每日收益
positions = {}
nav = 100.0
nav_history = []
daily_returns = []
exposure_history = []
breadth_history = []
npos_history = []

for di, date in enumerate(all_dates):
    states_today = date_to_states.get(date, {})
    signals_today = date_to_signals.get(date, [])

    # 市场宽度
    warm_hot_count = sum(1 for sid in all_states
                         if sid in states_today and states_today[sid][0] in ('温', '热', '沸'))
    valid_count = sum(1 for sid in all_states if sid in states_today)
    breadth = warm_hot_count / max(valid_count, 1)
    if breadth >= BREADTH_HIGH: target_alloc = ALLOC_HIGH
    elif breadth >= BREADTH_MID: target_alloc = ALLOC_MID
    else: target_alloc = 0.0

    # 更新峰值 + 记录昨日权重
    prev_weights = {sid: pos['weight'] for sid, pos in positions.items()}

    # 日收益 = sum(weight_i * ret_i)
    day_ret = 0.0
    for sid, pos in positions.items():
        if sid in states_today:
            c = float(states_today[sid][1])
            # 有昨收吗？
            prev_close = sid_daily_close[sid].get(all_dates[di-1]) if di > 0 else None
            if prev_close and prev_close > 0:
                r = c / prev_close - 1.0
                day_ret += pos['weight'] * r
            pos['peak_price'] = max(pos['peak_price'], c)

    nav *= (1 + day_ret)

    # 止盈检查
    exited_today = []
    for sid in list(positions.keys()):
        pos = positions[sid]
        if sid in states_today:
            st, c = states_today[sid]
            pos['state'] = st

            # 移动止盈
            if c < pos['peak_price'] * (1 - TRAILING_STOP):
                ret = (c / pos['entry_price'] - 1.0) * 100 - COST_BPS / 100.0
                trade_log.append({
                    'sid': sid, 'name': symb_names[sid],
                    'entry_date': pos['entry_date'], 'exit_date': date,
                    'return_pct': ret, 'weight': pos['weight'],
                    'exit_reason': f'止盈-{int(TRAILING_STOP*100)}%',
                    'scaled': pos.get('scaled_in_this_round', False),
                })
                exited_today.append(sid)
            # 安全网
            elif st in ('凉', '寒', '冻') and pos['state_prev'] in ('温', '热', '沸'):
                ret = (c / pos['entry_price'] - 1.0) * 100 - COST_BPS / 100.0
                trade_log.append({
                    'sid': sid, 'name': symb_names[sid],
                    'entry_date': pos['entry_date'], 'exit_date': date,
                    'return_pct': ret, 'weight': pos['weight'],
                    'exit_reason': '安全网',
                    'scaled': pos.get('scaled_in_this_round', False),
                })
                exited_today.append(sid)
            # 热→温
            elif pos['state_prev'] == '热' and st == '温':
                ret = (c / pos['entry_price'] - 1.0) * 100 - COST_BPS / 100.0
                trade_log.append({
                    'sid': sid, 'name': symb_names[sid],
                    'entry_date': pos['entry_date'], 'exit_date': date,
                    'return_pct': ret, 'weight': pos['weight'],
                    'exit_reason': '热→温',
                    'scaled': pos.get('scaled_in_this_round', False),
                })
                exited_today.append(sid)

    for sid in exited_today:
        del positions[sid]

    # 沸区加仓
    for sid in list(positions.keys()):
        pos = positions[sid]
        if pos.get('state') == '沸' and not pos.get('scaled_in_this_round', False):
            add_w = pos['weight'] * SCALE_IN_RATIO
            other_sids = [s for s in positions if s != sid]
            total_other = sum(positions[s]['weight'] for s in other_sids)
            if total_other > 0.001:
                for s in other_sids:
                    positions[s]['weight'] -= positions[s]['weight'] / total_other * add_w
                pos['weight'] += add_w
                pos['scaled_in_this_round'] = True
                pos['scale_add_date'] = date

    # 新开仓
    if target_alloc > 0 and signals_today:
        total_exp = sum(p['weight'] for p in positions.values())
        remaining = target_alloc - total_exp
        signals_today.sort(key=lambda x: x[3], reverse=True)

        for sig_sid, sig_name, sig_close, sig_score in signals_today:
            if remaining <= 0.005 or sig_sid in positions:
                continue
            n_pos = len(positions) + 1
            single_w = min(target_alloc / max(n_pos, 1), MAX_SINGLE_WEIGHT)
            single_w = min(single_w, remaining)
            if single_w < 0.01:
                continue

            if len(positions) > 0:
                total_existing = sum(p['weight'] for p in positions.values())
                if total_existing > 0.001:
                    scale = (total_existing + single_w - target_alloc) / total_existing
                    if scale > 0:
                        for s in positions:
                            positions[s]['weight'] *= (1 - scale)

            positions[sig_sid] = {
                'entry_date': date,
                'entry_price': sig_close,
                'peak_price': sig_close,
                'weight': single_w,
                'state': states_today[sig_sid][0] if sig_sid in states_today else '热',
                'state_prev': states_today[sig_sid][0] if sig_sid in states_today else '热',
                'scaled_in_this_round': False,
            }
            remaining = target_alloc - sum(p['weight'] for p in positions.values())

    # 记录 state_prev 给明天用
    for sid in positions:
        if sid in states_today:
            positions[sid]['state_prev'] = states_today[sid][0]

    # 快照
    exposure = sum(p['weight'] for p in positions.values())
    nav_history.append(nav)
    daily_returns.append(day_ret)
    exposure_history.append(exposure)
    breadth_history.append(breadth)
    npos_history.append(len(positions))

# 最终清仓
for sid, pos in positions.items():
    last_date = all_dates[-1]
    if sid in date_to_states.get(last_date, {}):
        c = date_to_states[last_date][sid][1]
    else:
        c = pos['entry_price']
    ret = (c / pos['entry_price'] - 1.0) * 100 - COST_BPS / 100.0
    trade_log.append({
        'sid': sid, 'name': symb_names[sid],
        'entry_date': pos['entry_date'], 'exit_date': last_date,
        'return_pct': ret, 'weight': pos['weight'],
        'exit_reason': '清仓',
        'scaled': pos.get('scaled_in_this_round', False),
    })
    nav *= (1 + pos['weight'] * (c / pos['entry_price'] - 1.0))

# ============================================================
# 3. 汇总
# ============================================================
print(f'\n[3/3] 汇总结果')

trades_df = pd.DataFrame(trade_log)
closed = trades_df[trades_df['exit_reason'] != '清仓']

n_trades = len(closed)
wins = (closed['return_pct'] > 0).sum()
losses = (closed['return_pct'] < 0).sum()
wr = wins / n_trades * 100 if n_trades > 0 else 0
aw = closed[closed['return_pct'] > 0]['return_pct'].mean() if wins > 0 else 0
al = abs(closed[closed['return_pct'] < 0]['return_pct'].mean()) if losses > 0 else 0
plr = aw / al if al > 0 else float('inf')

# 按退出原因分类
by_reason = closed.groupby('exit_reason').agg(
    笔数=('return_pct', 'count'),
    胜率=('return_pct', lambda x: (x > 0).sum() / len(x) * 100),
    均益=('return_pct', 'mean'),
).sort_values('笔数', ascending=False)

# 按年
closed['year'] = pd.to_datetime(closed['entry_date']).dt.year
by_year = closed.groupby('year').agg(
    笔数=('return_pct', 'count'),
    胜率=('return_pct', lambda x: (x > 0).sum() / len(x) * 100),
    均益=('return_pct', 'mean'),
    中位数=('return_pct', 'median'),
)

# 与沸区加仓相关
scaled_trades = closed[closed['scaled'] == True]
not_scaled = closed[closed['scaled'] == False]

# 夏普 & 最大回撤
dr = np.array(daily_returns)
if len(dr) > 5:
    monthly = pd.Series(dr, index=pd.to_datetime(all_dates)).resample('ME').apply(lambda x: (1+x).prod()-1)
    sharpe = monthly.mean() / (monthly.std() + 1e-9) * np.sqrt(12) if len(monthly) > 3 else 0
else:
    sharpe = 0

cum_nav = np.array(nav_history)
peak = np.maximum.accumulate(cum_nav)
mdd = (cum_nav / peak - 1).min() * 100

total_days = len(all_dates)
total_years = total_days / 252
cagr = (nav / 100) ** (1 / total_years) - 1 if total_years > 0 else 0

# Calmar ratio
calmar = cagr / abs(mdd / 100) if mdd < 0 else 0

# 胜率/亏损率 时间序列（滚动年）
rolling_wr = []
for i in range(252, len(all_dates)):
    day = all_dates[i]
    yr_ago = all_dates[i-252]
    period = closed[(closed['entry_date'] >= yr_ago) & (closed['entry_date'] <= day)]
    if len(period) > 5:
        rw = (period['return_pct'] > 0).sum() / len(period) * 100
        ra = period['return_pct'].mean()
        rolling_wr.append((day, rw, ra, len(period)))

print(f'\n{"="*70}')
print(f'  策略绩效')
print(f'{"="*70}')
print(f'  回测区间: {all_dates[0]} ~ {all_dates[-1]} ({total_days}天 / {total_years:.1f}年)')
print(f'  品种数量: {len(all_states)}')
print(f'')
print(f'  初始净值: 100.00')
print(f'  最终净值: {nav:.2f}')
print(f'  年化收益: {cagr*100:.1f}%')
print(f'  最大回撤: {mdd:.1f}%')
print(f'  Sharpe:   {sharpe:.2f}')
print(f'  Calmar:   {calmar:.2f}')
print(f'')
print(f'  总交易:   {len(trades_df)} 笔 (已平 {n_trades})')
print(f'  胜率:     {wr:.1f}%')
print(f'  盈亏比:   {plr:.2f}')
print(f'  均益:     {closed["return_pct"].mean():+.2f}%')
print(f'  中位数:   {closed["return_pct"].median():+.2f}%')
print(f'  均盈利:   {aw:+.2f}%')
print(f'  均亏损:   {-al:.2f}%')
print(f'')
print(f'  沸区加仓笔数: {len(scaled_trades)}')
if len(scaled_trades) > 0:
    print(f'    加仓均益: {scaled_trades["return_pct"].mean():+.2f}%')
    print(f'    未加仓均益: {not_scaled["return_pct"].mean():+.2f}%')

print(f'\n{"="*70}')
print(f'  退出原因分布')
print(f'{"="*70}')
print(by_year.to_string())
print()
print(by_reason.to_string())

# 与基准对比（等权买入持有）
print(f'\n{"="*70}')
print(f'  与无过滤基准对比')
print(f'{"="*70}')

# 基准：等权持有所有 L2 行业
bench_nav = [100.0]
for di in range(1, len(all_dates)):
    date = all_dates[di]
    prev_date = all_dates[di-1]
    rets = []
    for sid in all_states:
        c = sid_daily_close[sid].get(date)
        pc = sid_daily_close[sid].get(prev_date)
        if c and pc and pc > 0:
            rets.append(c / pc - 1.0)
    if rets:
        bench_nav.append(bench_nav[-1] * (1 + np.mean(rets)))
    else:
        bench_nav.append(bench_nav[-1])

bench_final = bench_nav[-1]
bench_peak = np.maximum.accumulate(np.array(bench_nav))
bench_mdd = (np.array(bench_nav) / bench_peak - 1).min() * 100

print(f'  策略最终净值: {nav:.2f} ({cagr*100:.1f}% p.a.)')
print(f'  基准最终净值: {bench_final:.2f} (等权持有全部L2)')
print(f'  超额收益:     {nav - bench_final:+.2f}')
print(f'  策略 MDD:     {mdd:.1f}%')
print(f'  基准 MDD:     {bench_mdd:.1f}%')

# 最大5笔盈利/亏损
print(f'\n{"="*70}')
print(f'  Top 5 盈利')
print(f'{"="*70}')
for _, row in closed.nlargest(5, 'return_pct').iterrows():
    print(f'  {row["name"]:10s} {row["entry_date"]} → {row["exit_date"]} {row["return_pct"]:+.1f}% [{row["exit_reason"]}]')

print(f'\n{"="*70}')
print(f'  Worst 5 亏损')
print(f'{"="*70}')
for _, row in closed.nsmallest(5, 'return_pct').iterrows():
    print(f'  {row["name"]:10s} {row["entry_date"]} → {row["exit_date"]} {row["return_pct"]:+.1f}% [{row["exit_reason"]}]')

# 滚动1年胜率
if rolling_wr:
    rdf = pd.DataFrame(rolling_wr, columns=['date', 'wr', 'avg_ret', 'n'])
    print(f'\n{"="*70}')
    print(f'  滚动 1 年（最近 5 期）')
    print(f'{"="*70}')
    for _, row in rdf.tail(5).iterrows():
        print(f'  {row["date"]}: 胜率{row["wr"]:.1f}% 均益{row["avg_ret"]:+.1f}% ({int(row["n"])}笔)')

print()
print('回测完成。')
