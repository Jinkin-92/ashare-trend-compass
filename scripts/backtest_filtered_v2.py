# -*- coding: utf-8 -*-
"""信号过滤器回测 v2 — 修复 RS，增加自适应过滤器。"""

import sys
sys.path.insert(0, '.')
import numpy as np
import pandas as pd
from collections import defaultdict
from src.db import get_session
from sqlalchemy import text

# ============================================================
# 1. 辅助函数
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

    # MA60 slope
    ma60 = rolling_mean(close, 60)
    ma60_slope = np.full(n, np.nan)
    valid_ma60 = ~np.isnan(ma60)
    ma60_shifted = np.roll(ma60, 10)
    mask60 = valid_ma60 & (~np.isnan(ma60_shifted))
    ma60_slope[mask60] = (ma60[mask60] - ma60_shifted[mask60]) / (np.abs(ma60_shifted[mask60]) + 1e-9)

    # ATR
    tr = np.maximum(high - low, np.abs(high - np.roll(close, 1)))
    tr[0] = high[0] - low[0]
    atr_short = rolling_mean(tr, 10)
    atr_long = rolling_mean(tr, 60)

    # Smoothing
    smooth_temp = np.full(n, np.nan)
    smooth_temp[0] = raw_temp_scaled[0] if not np.isnan(raw_temp_scaled[0]) else 0.0

    for i in range(1, n):
        if np.isnan(raw_temp_scaled[i]):
            smooth_temp[i] = smooth_temp[i - 1]
            continue
        abs_score = abs(smooth_temp[i - 1]) if not np.isnan(smooth_temp[i - 1]) else abs(raw_temp_scaled[i])
        if abs_score < 10:
            span = 3
        elif abs_score < 25:
            span = 5
        elif abs_score < 40:
            span = 8
        else:
            span = 12
        alpha = 2.0 / (span + 1)
        smooth_temp[i] = alpha * raw_temp_scaled[i] + (1 - alpha) * smooth_temp[i - 1]

    # State machine
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

    return smooth_temp, state, raw_temp_scaled, dd_pct, ma60_slope, atr_short, atr_long


# ============================================================
# 2. RS 计算（日频，直接查 daily_indicator 表）
# ============================================================
def load_rs_data():
    """从 daily_indicator 表加载所有 L2 行业的 rs_score，返回 {sid: {date_str: rs}}。"""
    with get_session() as s:
        rows = s.execute(text("""
            SELECT d.symbol_id, d.trade_date, d.rs_score
            FROM daily_indicator d
            JOIN symbols sym ON d.symbol_id = sym.symbol_id
            WHERE sym.node_type = 'industry_l2'
            ORDER BY d.symbol_id, d.trade_date
        """)).all()

    rs_data = defaultdict(dict)
    for sid, td, rs in rows:
        rs_data[sid][str(td)] = rs if rs is not None else 0.0
    return rs_data


# ============================================================
# 3. 回测引擎
# ============================================================
def backtest_filtered(sid, name, close, high, low, dates, rs_map, filters):
    smooth_temp, state, raw_temp, dd_pct, ma60_slope, atr_short, atr_long = \
        compute_temperature_full(close, high, low)

    trades = []
    in_position = False
    entry_price = entry_date = entry_idx = None
    warm_start_idx = None

    for i in range(1, len(state)):
        prev_s = state[i - 1]
        curr_s = state[i]

        if curr_s == '温' and prev_s != '温':
            warm_start_idx = i

        if not in_position:
            if prev_s == '温' and curr_s == '热':
                skip = False
                skip_reason = []

                # F1: 信号强度
                ss = filters.get('min_signal_strength', 0)
                if ss > 0 and raw_temp[i] < ss:
                    skip = True
                    skip_reason.append(f'sig={raw_temp[i]:.0f}<{ss}')

                # F2: 回撤
                md = filters.get('max_drawdown_pct', 0)
                if md < 0 and dd_pct[i] < md:
                    skip = True
                    skip_reason.append(f'dd={dd_pct[i]*100:.0f}%<{md*100:.0f}%')

                # F3: 温段持续
                mw = filters.get('min_warm_days', 0)
                if mw > 0 and warm_start_idx is not None:
                    dur = i - warm_start_idx
                    if dur < mw:
                        skip = True
                        skip_reason.append(f'warm={dur}d<{mw}d')

                # F4: MA60 上升
                if filters.get('require_ma60_rising', False):
                    if np.isnan(ma60_slope[i]) or ma60_slope[i] <= 0:
                        skip = True
                        skip_reason.append('MA60↓')

                # F5: ATR 扩张
                if filters.get('require_atr_expanding', False):
                    if np.isnan(atr_short[i]) or np.isnan(atr_long[i]) or atr_short[i] <= atr_long[i]:
                        skip = True
                        skip_reason.append('ATR↓')

                # F6: RS
                mr = filters.get('min_rs', 0)
                if mr > 0:
                    date_str = str(dates[i]).split(' ')[0]  # "2024-01-02"
                    rs_val = rs_map.get(date_str, 0)
                    if rs_val < mr:
                        skip = True
                        skip_reason.append(f'RS={rs_val:.0f}<{mr}')

                # F7: 自适应信号强度 (raw_temp / (1 + ATR ratio))
                adaptive = filters.get('adaptive_signal', False)
                if adaptive:
                    atr_ratio = atr_short[i] / (atr_long[i] + 1e-9) if not np.isnan(atr_short[i]) and not np.isnan(atr_long[i]) else 1.0
                    adj_threshold = 35 + 10 * max(0, 1.5 - atr_ratio)
                    if raw_temp[i] < adj_threshold:
                        skip = True
                        skip_reason.append(f'adapt={raw_temp[i]:.0f}<{adj_threshold:.0f}')

                # F8: 连续 N 日收盘上涨
                up_days = filters.get('min_up_days', 0)
                if up_days > 0:
                    consecutive = 0
                    j = i
                    while j > 0 and j > i - up_days:
                        if close[j] > close[j-1]:
                            consecutive += 1
                        else:
                            break
                        j -= 1
                    if consecutive < up_days:
                        skip = True
                        skip_reason.append(f'up={consecutive}<{up_days}')

                if skip:
                    continue

                in_position = True
                entry_price = close[i]
                entry_date = dates[i]
                entry_idx = i
        else:
            exit_reason = None
            if prev_s == '热' and curr_s == '温':
                exit_reason = '热→温'
            elif curr_s in ('凉', '寒', '冻') and prev_s in ('温', '热'):
                exit_reason = '安全网'

            if exit_reason:
                exit_price = close[i]
                exit_date = dates[i]
                ret = (exit_price / entry_price - 1.0) * 100
                hold_days = i - entry_idx
                trades.append({
                    'symbol': sid, 'name': name,
                    'entry_date': str(entry_date), 'exit_date': str(exit_date),
                    'entry_price': entry_price, 'exit_price': exit_price,
                    'return_pct': ret, 'hold_days': hold_days,
                    'exit_reason': exit_reason,
                })
                in_position = False
                entry_price = entry_date = entry_idx = None

    if in_position:
        exit_price = close[-1]
        ret = (exit_price / entry_price - 1.0) * 100
        trades.append({
            'symbol': sid, 'name': name,
            'entry_date': str(entry_date), 'exit_date': str(dates[-1]),
            'entry_price': entry_price, 'exit_price': exit_price,
            'return_pct': ret, 'hold_days': len(state) - 1 - entry_idx,
            'exit_reason': 'OPEN',
        })
    return trades


def trade_summary(trades):
    if not trades:
        return {'total': 0, 'closed': 0, 'win_rate': 0, 'avg_return': 0,
                'median_return': 0, 'avg_win': 0, 'avg_loss': 0,
                'pl_ratio': 0, 'avg_hold': 0, 'sharpe_approx': 0,
                'annual': {}, 'worst_5': []}

    df = pd.DataFrame(trades)
    closed = df[df['exit_reason'] != 'OPEN']
    all_ret = df['return_pct']
    wins = (closed['return_pct'] > 0).sum()
    losses = (closed['return_pct'] < 0).sum()
    tc = len(closed)
    wr = wins / tc * 100 if tc > 0 else 0
    aw = closed[closed['return_pct'] > 0]['return_pct'].mean() if wins > 0 else 0
    al = abs(closed[closed['return_pct'] < 0]['return_pct'].mean()) if losses > 0 else 0
    plr = aw / al if al > 0 else float('inf')

    annual = {}
    for year in sorted(pd.to_datetime(df['entry_date']).dt.year.unique()):
        yr = df[pd.to_datetime(df['entry_date']).dt.year == year]
        yr_c = yr[yr['exit_reason'] != 'OPEN']
        yr_w = (yr_c['return_pct'] > 0).sum()
        annual[int(year)] = {
            'trades': len(yr_c),
            'win_rate': yr_w / len(yr_c) * 100 if len(yr_c) > 0 else 0,
            'avg_return': yr_c['return_pct'].mean() if len(yr_c) > 0 else 0,
        }

    sharpe = 0
    if tc > 5:
        try:
            monthly_ret = df.set_index(pd.to_datetime(df['entry_date']))['return_pct'].resample('ME').sum()
            if len(monthly_ret) > 3:
                sharpe = monthly_ret.mean() / (monthly_ret.std() + 1e-9) * np.sqrt(12)
        except:
            pass

    worst_5 = []
    if tc >= 5:
        for idx in np.argsort(closed['return_pct'].values)[:5]:
            row = closed.iloc[idx]
            worst_5.append(f'{row["name"]} {row["entry_date"][:10]} {row["return_pct"]:+.1f}%')

    cum = (1 + all_ret / 100).cumprod()
    max_dd = (cum / cum.cummax() - 1).min() * 100 if len(cum) > 0 else 0

    return {
        'total': len(df), 'closed': tc, 'win_rate': wr,
        'avg_return': all_ret.mean(), 'median_return': all_ret.median(),
        'avg_win': aw, 'avg_loss': -al, 'pl_ratio': plr,
        'avg_hold': df['hold_days'].mean(), 'sharpe_approx': sharpe,
        'max_drawdown': max_dd, 'annual': annual, 'worst_5': worst_5,
    }


# ============================================================
# 4. 主流程
# ============================================================

print('=' * 70)
print('  信号过滤器回测 v2 (修复RS + 新增自适应过滤器)')
print('=' * 70)

with get_session() as s:
    symb_rows = s.execute(text(
        "SELECT symbol_id, name FROM symbols WHERE node_type='industry_l2'"
    )).all()
symb_ids = [r[0] for r in symb_rows]
symb_names = {r[0]: r[1] for r in symb_rows}

print(f'\n加载价格数据 ({len(symb_ids)} 品种)...')
all_price_data = {}
skipped = 0
with get_session() as s:
    for sid in symb_ids:
        rows = s.execute(text(
            'SELECT trade_date, close, high, low FROM daily_price WHERE symbol_id=:sid ORDER BY trade_date'
        ), {'sid': sid}).all()
        if len(rows) < 200:
            skipped += 1
            continue
        df = pd.DataFrame(rows, columns=['trade_date', 'close', 'high', 'low'])
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df = df.set_index('trade_date')
        all_price_data[sid] = df

print(f'  有效: {len(all_price_data)} (跳过 {skipped})')

print('加载 RS 数据...')
rs_database = load_rs_data()
rs_count = sum(len(v) for v in rs_database.values())
print(f'  RS 记录: {rs_count}')

# ============================================================
# 5. 过滤器组合
# ============================================================
filter_sets = {
    'baseline':       {'label': '基准(无过滤)', 'filters': {}},
    'F1_sig38':       {'label': 'F1: 信号≥38', 'filters': {'min_signal_strength': 38}},
    'F2_dd25':        {'label': 'F2: 回撤≤25%', 'filters': {'max_drawdown_pct': -0.25}},
    'F3_warm5':       {'label': 'F3: 温段≥5天', 'filters': {'min_warm_days': 5}},
    'F6_rs30':        {'label': 'F6: RS≥30', 'filters': {'min_rs': 30}},
    'F6_rs50':        {'label': 'F6b: RS≥50', 'filters': {'min_rs': 50}},
    'F8_up3':         {'label': 'F8: 连涨≥3天', 'filters': {'min_up_days': 3}},
    'F9_adapt':       {'label': 'F9: 自适应信号', 'filters': {'adaptive_signal': True}},

    'combo_A':        {'label': 'A: 信号+回撤+温段', 'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'min_warm_days': 5}},
    'combo_AR':       {'label': 'A+RS30: 信号+回撤+温段+RS30', 'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'min_warm_days': 5, 'min_rs': 30}},
    'combo_AU':       {'label': 'A+连涨: 信号+回撤+温段+连涨3', 'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'min_warm_days': 5, 'min_up_days': 3}},
    'combo_Aa':       {'label': 'A+自适应: 信号+回撤+温段+自适应', 'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'min_warm_days': 5, 'adaptive_signal': True}},

    'tight1':         {'label': '紧1: 信号40+回撤20%+温段8+RS40', 'filters': {'min_signal_strength': 40, 'max_drawdown_pct': -0.20, 'min_warm_days': 8, 'min_rs': 40}},
    'tight2':         {'label': '紧2: 信号40+回撤20%+温段8', 'filters': {'min_signal_strength': 40, 'max_drawdown_pct': -0.20, 'min_warm_days': 8}},
    'tight3':         {'label': '紧3: 信号40+回撤20%+温段8+连涨3+RS40', 'filters': {'min_signal_strength': 40, 'max_drawdown_pct': -0.20, 'min_warm_days': 8, 'min_up_days': 3, 'min_rs': 40}},
}

results = {}

for key, cfg in filter_sets.items():
    print(f'\n--- {cfg["label"]} ---', end='', flush=True)
    all_trades = []
    for sid in all_price_data:
        df = all_price_data[sid]
        rs_map = rs_database.get(sid, {})
        trades = backtest_filtered(
            sid, symb_names[sid],
            df['close'].values, df['high'].values, df['low'].values, df.index,
            rs_map, cfg['filters'],
        )
        all_trades.extend(trades)

    s = trade_summary(all_trades)
    results[key] = s
    print(f' {s["closed"]}笔 胜率{s["win_rate"]:.1f}% 均益{s["avg_return"]:+.2f}% PLR{s["pl_ratio"]:.2f} Sharpe{s["sharpe_approx"]:.2f}')

# ============================================================
# 6. 汇总
# ============================================================
print(f'\n{"=" * 95}')
print(f'  汇 总 对 比')
print(f'{"=" * 95}')
print(f'{"配置":<35s} {"笔数":>5s} {"胜率":>6s} {"均益":>7s} {"PLR":>5s} {"Sharpe":>6s} {"MDD":>7s}')
print('-' * 95)

for key in filter_sets:
    cfg = filter_sets[key]
    s = results[key]
    if s['closed'] == 0:
        print(f'{cfg["label"]:<35s} {"0":>5s}')
        continue
    print(f'{cfg["label"]:<35s} {s["closed"]:>5d} {s["win_rate"]:>5.1f}% {s["avg_return"]:>+6.2f}% {s["pl_ratio"]:>4.2f} {s["sharpe_approx"]:>5.2f} {s["max_drawdown"]:>6.1f}%')

# 年度拆解
print(f'\n{"=" * 95}')
print(f'  年 度 拆 解')
print(f'{"=" * 95}')
years = sorted(set().union(*[set(s['annual'].keys()) for s in results.values() if s['closed'] > 0]))

print(f'{"配置":<35s}', end='')
for yr in years:
    print(f' {yr:>12s}', end='')
print()
print('-' * (35 + 13 * len(years)))

for key in filter_sets:
    cfg = filter_sets[key]
    s = results[key]
    if s['closed'] == 0:
        continue
    print(f'{cfg["label"]:<35s}', end='')
    for yr in years:
        a = s['annual'].get(yr, {})
        if a:
            print(f' {a["win_rate"]:>4.0f}%{a["avg_return"]:>+6.1f}%', end='')
        else:
            print(f' {"-":>12s}', end='')
    print()

print()
print('过滤完成。')
