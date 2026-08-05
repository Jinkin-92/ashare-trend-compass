# -*- coding: utf-8 -*-
"""带信号过滤器的回测：温→热买入，热→温/安全网卖出。
支持多种过滤条件组合，找出最优过滤器配置。
"""

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

def rolling_sum(arr, w):
    s = np.full(len(arr), np.nan)
    cs = np.cumsum(np.nan_to_num(arr, 0))
    s[w-1:] = cs[w-1:] - np.concatenate([[0], cs[:-w]])[w-1:]
    return s

# ============================================================
# 2. 温度计算（同新框架）
# ============================================================
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
    """返回 (smooth_temp, state, raw_temp, z_score, dd, dd_pct, mom, ma60_slope, atr_short, atr_long)."""
    n = len(close)

    # ── 组件 1: Z-score (40%) ──
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

    # ── 组件 2: 回撤深度 (20%) ──
    hi250 = rolling_max(high, 250)
    dd_pct = np.full(n, 0.0)
    valid_dd = ~np.isnan(hi250)
    dd_pct[valid_dd] = (close[valid_dd] - hi250[valid_dd]) / (hi250[valid_dd] + 1e-9)
    dd_score = np.tanh(dd_pct * 3.5)

    # ── 组件 3: 多尺度动量 (40%) ──
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

    # ── MA60 slope ──
    ma60 = rolling_mean(close, 60)
    ma60_slope = np.full(n, np.nan)
    valid_ma60 = ~np.isnan(ma60)
    ma60_shifted = np.roll(ma60, 10)
    mask60 = valid_ma60 & (~np.isnan(ma60_shifted))
    ma60_slope[mask60] = (ma60[mask60] - ma60_shifted[mask60]) / (np.abs(ma60_shifted[mask60]) + 1e-9)

    # ── ATR (volatility) ──
    tr = np.maximum(high - low, np.abs(high - np.roll(close, 1)))
    tr[0] = high[0] - low[0]
    atr_short = rolling_mean(tr, 10)
    atr_long = rolling_mean(tr, 60)

    # ── Level-dependent EMA ──
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

    # ── 状态机 ──
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

    return smooth_temp, state, raw_temp_scaled, z_score, dd_score, dd_pct, mom_composite, ma60_slope, atr_short, atr_long


# ============================================================
# 3. RS 计算（L2 行业间横向排名）
# ============================================================
def compute_all_rs(all_price_data):
    """对每个交易日，在所有 L2 品种间做 20/60/120 日 ROC 加权百分位排名。
    返回 {symbol_id: rs_series}。
    """
    # 收集所有品种的收盘价到一个 DataFrame
    close_dict = {}
    for sid, df in all_price_data.items():
        close_dict[sid] = df['close']
    price_df = pd.DataFrame(close_dict).sort_index()
    # 只保留至少有 100 个品种的日期
    price_df = price_df.dropna(thresh=100)

    rs_result = defaultdict(dict)

    # 对每个日期计算 RS
    windows = [20, 60, 120]
    weights = [0.45, 0.30, 0.25]

    for date_idx, date in enumerate(price_df.index):
        closes = price_df.iloc[date_idx]
        valid = closes.notna()
        if valid.sum() < 50:
            continue

        # 计算每个品种的多周期 ROC
        scores = {}
        for sid in valid[valid].index:
            c = closes[sid]
            roc_sum = 0
            w_sum = 0
            for w, wt in zip(windows, weights):
                if date_idx >= w:
                    prev_c = price_df.iloc[date_idx - w][sid]
                    if not np.isnan(prev_c) and prev_c > 0:
                        r = (c / prev_c - 1) * 100
                        roc_sum += np.clip(r, -100, 200) * wt
                        w_sum += wt
            if w_sum > 0:
                scores[sid] = roc_sum / w_sum

        if len(scores) < 10:
            continue

        # 百分位排名 (0-99)
        sc = pd.Series(scores)
        ranks = sc.rank(pct=True) * 100
        for sid, rank in ranks.items():
            rs_result[sid][date] = round(rank, 1)

    return rs_result


# ============================================================
# 4. 回测引擎（带过滤器）
# ============================================================
def backtest_filtered(
    sid, name, close, high, low, dates,
    rs_by_date,  # {date_str: rs_value}
    filters,
):
    """回测单个品种，应用过滤器。

    filters 字典:
        min_signal_strength: 买入时 raw_temp 最低值（默认 0，即不过滤）
        max_drawdown_pct: 买入时距 250 日高点最大回撤（默认 0，即不过滤；例如 -0.25 表示最多跌 25%）
        min_warm_days: 买入前「温」至少持续天数（默认 0）
        require_ma60_rising: 买入时 MA60 必须上升（默认 False）
        require_atr_expanding: 买入时短期 ATR > 长期 ATR（默认 False）
        min_rs: 买入时 RS 最低百分位（默认 0）
    """
    smooth_temp, state, raw_temp, z_score, dd_score, dd_pct, mom, ma60_slope, atr_short, atr_long = \
        compute_temperature_full(close, high, low)

    trades = []
    in_position = False
    entry_price = None
    entry_date = None
    entry_idx = None
    warm_start_idx = None  # 当前温段起始索引

    for i in range(1, len(state)):
        prev_s = state[i - 1]
        curr_s = state[i]

        # 追踪「温」段起始
        if curr_s == '温' and prev_s != '温':
            warm_start_idx = i

        if not in_position:
            # 检查买入信号: 温→热
            if prev_s == '温' and curr_s == '热':
                # === 过滤器检查 ===
                skip = False
                skip_reason = []

                # 1. 信号强度
                if filters.get('min_signal_strength', 0) > 0:
                    if raw_temp[i] < filters['min_signal_strength']:
                        skip = True
                        skip_reason.append(f'rawT={raw_temp[i]:.1f}<{filters["min_signal_strength"]}')

                # 2. 回撤
                if filters.get('max_drawdown_pct', 0) < 0:
                    if dd_pct[i] < filters['max_drawdown_pct']:
                        skip = True
                        skip_reason.append(f'dd={dd_pct[i]*100:.1f}%<{filters["max_drawdown_pct"]*100:.0f}%')

                # 3. 温段持续天数
                min_warm = filters.get('min_warm_days', 0)
                if min_warm > 0 and warm_start_idx is not None:
                    warm_duration = i - warm_start_idx
                    if warm_duration < min_warm:
                        skip = True
                        skip_reason.append(f'warmDays={warm_duration}<{min_warm}')

                # 4. MA60 上升
                if filters.get('require_ma60_rising', False):
                    if np.isnan(ma60_slope[i]) or ma60_slope[i] <= 0:
                        skip = True
                        skip_reason.append('MA60↓')

                # 5. ATR 扩张
                if filters.get('require_atr_expanding', False):
                    if np.isnan(atr_short[i]) or np.isnan(atr_long[i]) or atr_short[i] <= atr_long[i]:
                        skip = True
                        skip_reason.append('ATR↓')

                # 6. RS 最低
                min_rs = filters.get('min_rs', 0)
                if min_rs > 0:
                    date_str = str(dates[i])
                    rs_val = rs_by_date.get(date_str, 0)
                    if rs_val < min_rs:
                        skip = True
                        skip_reason.append(f'RS={rs_val:.0f}<{min_rs}')

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

                trade_rec = {
                    'symbol': sid,
                    'name': name,
                    'entry_date': str(entry_date),
                    'exit_date': str(exit_date),
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'return_pct': ret,
                    'hold_days': hold_days,
                    'exit_reason': exit_reason,
                }
                trades.append(trade_rec)

                in_position = False
                entry_price = None
                entry_date = None
                entry_idx = None

    # 持仓中
    if in_position:
        exit_price = close[-1]
        exit_date = dates[-1]
        ret = (exit_price / entry_price - 1.0) * 100
        hold_days = len(state) - 1 - entry_idx
        trades.append({
            'symbol': sid, 'name': name,
            'entry_date': str(entry_date), 'exit_date': str(exit_date),
            'entry_price': entry_price, 'exit_price': exit_price,
            'return_pct': ret, 'hold_days': hold_days,
            'exit_reason': 'OPEN',
        })

    return trades


# ============================================================
# 5. 统计
# ============================================================
def trade_summary(trades, label=""):
    if not trades:
        return {'total': 0, 'closed': 0, 'win_rate': 0, 'avg_return': 0,
                'median_return': 0, 'avg_win': 0, 'avg_loss': 0,
                'pl_ratio': 0, 'avg_hold': 0, 'cum_prod': 1.0, 'sharpe_approx': 0,
                'annual': {}, 'worst_drawdowns': []}

    df = pd.DataFrame(trades)
    closed = df[df['exit_reason'] != 'OPEN']
    all_ret = df['return_pct']

    wins = (closed['return_pct'] > 0).sum()
    losses = (closed['return_pct'] < 0).sum()
    total_closed = len(closed)

    win_rate = wins / total_closed * 100 if total_closed > 0 else 0
    avg_w = closed[closed['return_pct'] > 0]['return_pct'].mean() if wins > 0 else 0
    avg_l = abs(closed[closed['return_pct'] < 0]['return_pct'].mean()) if losses > 0 else 0
    pl_ratio = avg_w / avg_l if avg_l > 0 else float('inf')

    # 年度拆解
    annual = {}
    for year in sorted(pd.to_datetime(df['entry_date']).dt.year.unique()):
        yr = df[pd.to_datetime(df['entry_date']).dt.year == year]
        yr_closed = yr[yr['exit_reason'] != 'OPEN']
        yr_wins = (yr_closed['return_pct'] > 0).sum()
        annual[int(year)] = {
            'trades': len(yr_closed),
            'win_rate': yr_wins / len(yr_closed) * 100 if len(yr_closed) > 0 else 0,
            'avg_return': yr_closed['return_pct'].mean() if len(yr_closed) > 0 else 0,
        }

    # 最差连续回撤（等权累积）
    cum_series = (1 + all_ret / 100).cumprod()
    running_max = cum_series.cummax()
    drawdowns = (cum_series / running_max - 1) * 100
    max_dd = drawdowns.min()
    worst_5_dd = []
    if total_closed >= 5:
        worst_idxs = np.argsort(closed['return_pct'].values)[:5]
        for idx in worst_idxs:
            row = closed.iloc[idx]
            worst_5_dd.append(f'{row["name"]} {row["entry_date"]} {row["return_pct"]:+.1f}%')

    # Sharpe 近似（年化）
    if total_closed > 1:
        monthly_ret = df.set_index(pd.to_datetime(df['entry_date']))['return_pct'].resample('ME').sum()
        if len(monthly_ret) > 3:
            sharpe_approx = monthly_ret.mean() / (monthly_ret.std() + 1e-9) * np.sqrt(12)
        else:
            sharpe_approx = 0
    else:
        sharpe_approx = 0

    return {
        'total': len(df),
        'closed': total_closed,
        'win_rate': win_rate,
        'avg_return': all_ret.mean(),
        'median_return': all_ret.median(),
        'avg_win': avg_w,
        'avg_loss': -avg_l,
        'pl_ratio': pl_ratio,
        'avg_hold': df['hold_days'].mean(),
        'cum_prod': (1 + all_ret / 100).prod() if len(all_ret) > 0 else 1.0,
        'sharpe_approx': sharpe_approx,
        'max_drawdown': max_dd,
        'annual': annual,
        'worst_drawdowns': worst_5_dd,
    }


# ============================================================
# 6. 主流程
# ============================================================

print('=' * 70)
print('  信号过滤器回测')
print('=' * 70)

# 加载品种
with get_session() as s:
    symb_rows = s.execute(text(
        "SELECT symbol_id, name FROM symbols WHERE node_type='industry_l2'"
    )).all()

symb_ids = [r[0] for r in symb_rows]
symb_names = {r[0]: r[1] for r in symb_rows}

# 加载所有价格数据
print(f'\n加载 L2 行业价格数据 ({len(symb_ids)} 个品种)...')
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

print(f'  有效品种: {len(all_price_data)} (跳过 {skipped} 个)')

# 计算所有品种的 RS
print('计算 RS 排名...')
rs_data = compute_all_rs(all_price_data)
print(f'  RS 数据: {len(rs_data)} 个品种')

# ============================================================
# 7. 过滤器组合测试
# ============================================================

filter_sets = {
    # 基准（无过滤）
    'baseline': {
        'label': '基准（无过滤）',
        'filters': {},
    },
    # 单过滤器测试
    'F1_signal': {
        'label': 'F1: 信号强度≥38',
        'filters': {'min_signal_strength': 38},
    },
    'F2_dd': {
        'label': 'F2: 回撤≤25%',
        'filters': {'max_drawdown_pct': -0.25},
    },
    'F3_warm': {
        'label': 'F3: 温段≥5天',
        'filters': {'min_warm_days': 5},
    },
    'F4_ma60': {
        'label': 'F4: MA60上升',
        'filters': {'require_ma60_rising': True},
    },
    'F5_atr': {
        'label': 'F5: ATR扩张',
        'filters': {'require_atr_expanding': True},
    },
    'F6_rs': {
        'label': 'F6: RS≥40',
        'filters': {'min_rs': 40},
    },
    # 组合过滤器
    'combo_A': {
        'label': '组合A: 信号+回撤+温段',
        'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'min_warm_days': 5},
    },
    'combo_B': {
        'label': '组合B: 信号+回撤+MA60',
        'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'require_ma60_rising': True},
    },
    'combo_C': {
        'label': '组合C: 信号+回撤+RS≥40',
        'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'min_rs': 40},
    },
    'combo_D': {
        'label': '组合D: 全量(信号+回撤+温段+MA60+RS)',
        'filters': {'min_signal_strength': 38, 'max_drawdown_pct': -0.25, 'min_warm_days': 5, 'require_ma60_rising': True, 'min_rs': 40},
    },
    'combo_E': {
        'label': '组合E: 精简(信号+回撤+MA60)',
        'filters': {'min_signal_strength': 40, 'max_drawdown_pct': -0.20, 'require_ma60_rising': True},
    },
    'combo_F': {
        'label': '组合F: 信号35+回撤30%+RS30',
        'filters': {'min_signal_strength': 35, 'max_drawdown_pct': -0.30, 'min_rs': 30},
    },
}

results = {}

for key, cfg in filter_sets.items():
    print(f'\n--- {cfg["label"]} ---')
    all_trades = []

    for idx, sid in enumerate(all_price_data.keys()):
        df = all_price_data[sid]
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        dates = df.index

        # 构建该品种的 RS 映射
        rs_map = {str(d.date()): rs_data.get(sid, {}).get(d, 0) for d in dates}

        trades = backtest_filtered(
            sid, symb_names[sid], close, high, low, dates,
            rs_map, cfg['filters'],
        )
        all_trades.extend(trades)

    s = trade_summary(all_trades)
    results[key] = s

    print(f'  交易 {s["total"]} 笔（已平 {s["closed"]}），'
          f'胜率 {s["win_rate"]:.1f}%，均收益 {s["avg_return"]:+.2f}%，'
          f'盈亏比 {s["pl_ratio"]:.2f}，Sharpe {s["sharpe_approx"]:.2f}')

# ============================================================
# 8. 汇总对比
# ============================================================
print(f'\n{"=" * 90}')
print(f'  过 滤 器 对 比 汇 总')
print(f'{"=" * 90}')
header = f'{"配置":<28s} {"交易":>5s} {"胜率":>6s} {"均收益":>7s} {"盈亏比":>6s} {"Sharpe":>7s} {"最大回撤":>8s}'
print(header)
print('-' * 90)

best_key = None
best_score = -999

for key in filter_sets:
    cfg = filter_sets[key]
    s = results[key]
    if s['closed'] == 0:
        continue

    # 综合评分：胜率 * 0.3 + min(盈亏比,5)/5 * 0.3 + Sharpe/3 * 0.2 + 均收益/5 * 0.2
    score = (s['win_rate'] / 100 * 0.3 +
             min(s['pl_ratio'], 5) / 5 * 0.3 +
             min(s['sharpe_approx'], 3) / 3 * 0.2 +
             max(min(s['avg_return'], 10), -10) / 10 * 0.15 +
             0.05)  # base

    line = f'{cfg["label"]:<28s} {s["closed"]:>5d} {s["win_rate"]:>5.1f}% {s["avg_return"]:>+6.2f}% {s["pl_ratio"]:>5.2f} {s["sharpe_approx"]:>6.2f} {s["max_drawdown"]:>7.1f}%'
    print(line)

    if score > best_score:
        best_score = score
        best_key = key

# ============================================================
# 9. 最佳配置年度拆解
# ============================================================
if best_key:
    cfg = filter_sets[best_key]
    s = results[best_key]
    print(f'\n{"=" * 60}')
    print(f'  最佳配置: {cfg["label"]}')
    print(f'{"=" * 60}')
    print(f'  交易: {s["total"]}笔(已平{s["closed"]})  胜率: {s["win_rate"]:.1f}%')
    print(f'  均收益: {s["avg_return"]:+.2f}%  中位数: {s["median_return"]:+.2f}%')
    print(f'  平均盈利: {s["avg_win"]:+.2f}%  平均亏损: {s["avg_loss"]:.2f}%  盈亏比: {s["pl_ratio"]:.2f}')
    print(f'  Sharpe: {s["sharpe_approx"]:.2f}  平均持仓: {s["avg_hold"]:.0f}天')

    print(f'\n  年度表现:')
    for yr in sorted(s['annual'].keys()):
        a = s['annual'][yr]
        print(f'    {yr}: {a["trades"]}笔  胜率{a["win_rate"]:.1f}%  均收益{a["avg_return"]:+.2f}%')

    print(f'\n  最差5笔:')
    for dd in s.get('worst_drawdowns', [])[:5]:
        print(f'    {dd}')

# ============================================================
# 10. 详细对比表
# ============================================================
print(f'\n{"=" * 90}')
print(f'  年 度 胜 率 对 比')
print(f'{"=" * 90}')
years = sorted(set().union(*[set(s['annual'].keys()) for s in results.values() if s['closed'] > 0]))
yr_header = f'{"配置":<28s}'
for yr in years:
    yr_header += f' {yr:>10s}'
print(yr_header)
print('-' * (28 + 11 * len(years)))

for key in filter_sets:
    cfg = filter_sets[key]
    s = results[key]
    if s['closed'] == 0:
        continue
    line = f'{cfg["label"]:<28s}'
    for yr in years:
        a = s['annual'].get(yr, {})
        if a:
            line += f' {a["win_rate"]:>4.0f}%{a["avg_return"]:>+5.1f}%'
        else:
            line += f' {"-":>10s}'
    print(line)

print()
print('过滤完成。')
