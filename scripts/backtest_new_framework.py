# -*- coding: utf-8 -*-
"""新多尺度框架回测：温→热买入，热→温/温→平卖出。
覆盖全部 SW L2 行业。"""

import sys
sys.path.insert(0, '.')
import numpy as np
import pandas as pd
from collections import Counter, defaultdict
from src.db import get_session
from sqlalchemy import text

# ============================================================
# 1. 加载 L2 行业品种列表
# ============================================================
with get_session() as s:
    symb_rows = s.execute(text(
        "SELECT symbol_id, name FROM symbols WHERE node_type='industry_l2'"
    )).all()

symb_ids = [r[0] for r in symb_rows]
symb_names = {r[0]: r[1] for r in symb_rows}
print(f'L2 行业数量: {len(symb_ids)}')

# ============================================================
# 2. 框架计算函数
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

def compute_temperature(close, high, low):
    """返回 (smooth_temp, state_series)。"""
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
    dd = np.full(n, 0.0)
    valid_dd = ~np.isnan(hi250)
    dd[valid_dd] = (close[valid_dd] - hi250[valid_dd]) / (hi250[valid_dd] + 1e-9)
    dd_score = np.tanh(dd * 3.5)

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
    THRESHOLDS = [
        (-65, -45, '冻'), (-45, -25, '寒'), (-25, -10, '凉'),
        (-10, 12, '平'), (12, 35, '温'), (35, 55, '热'), (55, 65, '沸'),
    ]
    LEVEL_ORDER = ['冻', '寒', '凉', '平', '温', '热', '沸']
    LEVEL_IDX = {l: i for i, l in enumerate(LEVEL_ORDER)}

    def raw_level(s):
        for lo, hi, label in THRESHOLDS:
            if lo <= s < hi:
                return label
        return '沸' if s >= 55 else '冻'

    CONFIRM = {
        '沸': {'enter': 4, 'exit': 8}, '冻': {'enter': 4, 'exit': 8},
        '热': {'enter': 3, 'exit': 5}, '寒': {'enter': 3, 'exit': 5},
        '温': {'enter': 2, 'exit': 3}, '凉': {'enter': 2, 'exit': 3},
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

    return smooth_temp, state


# ============================================================
# 3. 回测引擎
# ============================================================

def backtest_symbol(sid, name, close, high, low, dates):
    """对一个品种模拟交易，返回交易记录列表。"""
    smooth_temp, state = compute_temperature(close, high, low)

    trades_A = []  # 热→温 卖出
    trades_B = []  # 温→平 卖出

    in_position = False
    entry_price = None
    entry_date = None
    entry_idx = None

    for i in range(1, len(state)):
        prev_s = state[i - 1]
        curr_s = state[i]

        if not in_position:
            # 温→热：买入
            if prev_s == '温' and curr_s == '热':
                in_position = True
                entry_price = close[i]
                entry_date = dates[i]
                entry_idx = i
        else:
            # 热→温 或 温→平：卖出
            exit_reason = None
            if prev_s == '热' and curr_s == '温':
                exit_reason = 'A_热→温'
            elif prev_s == '温' and curr_s == '平':
                exit_reason = 'B_温→平'
            # 额外安全网：如果跌入凉/寒/冻，强制退出
            elif curr_s in ('凉', '寒', '冻') and prev_s in ('温', '热'):
                exit_reason = 'SAFE_凉寒冻'

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

                if exit_reason == 'A_热→温':
                    trades_A.append(trade_rec)
                elif exit_reason == 'B_温→平':
                    trades_B.append(trade_rec)
                else:
                    # SAFE exit counts in both as it's a forced exit
                    trades_A.append(trade_rec)
                    trades_B.append(trade_rec)

                in_position = False
                entry_price = None
                entry_date = None
                entry_idx = None

    # Handle open position at end of data
    if in_position:
        exit_price = close[-1]
        exit_date = dates[-1]
        ret = (exit_price / entry_price - 1.0) * 100
        hold_days = len(state) - 1 - entry_idx
        trade_rec = {
            'symbol': sid,
            'name': name,
            'entry_date': str(entry_date),
            'exit_date': str(exit_date),
            'entry_price': entry_price,
            'exit_price': exit_price,
            'return_pct': ret,
            'hold_days': hold_days,
            'exit_reason': 'OPEN_未平仓',
        }
        trades_A.append(trade_rec)
        trades_B.append(trade_rec)

    return trades_A, trades_B


# ============================================================
# 4. 执行回测
# ============================================================

all_trades_A = []
all_trades_B = []

with get_session() as s:
    for idx, sid in enumerate(symb_ids):
        rows = s.execute(text(
            'SELECT trade_date, close, high, low FROM daily_price WHERE symbol_id=:sid ORDER BY trade_date'
        ), {'sid': sid}).all()

        if len(rows) < 200:
            continue  # 数据太短，跳过

        df = pd.DataFrame(rows, columns=['trade_date', 'close', 'high', 'low'])
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        dates = df['trade_date'].values

        tA, tB = backtest_symbol(sid, symb_names[sid], close, high, low, dates)
        all_trades_A.extend(tA)
        all_trades_B.extend(tB)

        if (idx + 1) % 20 == 0:
            print(f'  进度: {idx + 1}/{len(symb_ids)} ({symb_names[sid]}) ... A={len(all_trades_A)} B={len(all_trades_B)}')

# ============================================================
# 5. 统计
# ============================================================

def trade_stats(trades, label):
    if not trades:
        print(f'\n─── {label} ───')
        print('  无交易记录')
        return

    df = pd.DataFrame(trades)
    closed = df[df['exit_reason'] != 'OPEN_未平仓']
    all_ret = df['return_pct']

    print(f'\n{"=" * 60}')
    print(f'  {label}')
    print(f'{"=" * 60}')
    print(f'  总交易数: {len(df)}')
    print(f'  已平仓: {len(closed)}')
    print(f'  持仓中: {len(df) - len(closed)}')

    # Win rate
    wins = (closed['return_pct'] > 0).sum()
    losses = (closed['return_pct'] < 0).sum()
    breakeven = (closed['return_pct'] == 0).sum()
    win_rate = wins / len(closed) * 100 if len(closed) > 0 else 0
    print(f'  胜: {wins} | 负: {losses} | 平: {breakeven} | 胜率: {win_rate:.1f}%')

    # Return stats
    print(f'  平均收益: {all_ret.mean():.2f}%')
    print(f'  中位数收益: {all_ret.median():.2f}%')
    print(f'  最大收益: {all_ret.max():.2f}%')
    print(f'  最大亏损: {all_ret.min():.2f}%')
    print(f'  标准差: {all_ret.std():.2f}%')

    # P/L ratio
    avg_win = closed[closed['return_pct'] > 0]['return_pct'].mean() if wins > 0 else 0
    avg_loss = abs(closed[closed['return_pct'] < 0]['return_pct'].mean()) if losses > 0 else 0
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    print(f'  平均盈利: {avg_win:+.2f}%  |  平均亏损: {-avg_loss:.2f}%  |  盈亏比: {pl_ratio:.2f}')

    # Cumulative return (assuming equal weighting)
    if len(all_ret) > 0:
        cum_ret = (1 + all_ret / 100).prod() - 1
        print(f'  等权累积收益: {cum_ret*100:.2f}%')

    # Hold days
    print(f'  平均持仓天数: {df["hold_days"].mean():.1f}')
    print(f'  中位持仓天数: {df["hold_days"].median():.1f}')

    # Exit reason distribution
    print(f'  退出原因分布:')
    for reason, count in df['exit_reason'].value_counts().items():
        print(f'    {reason}: {count}')

    # Top 5 best / worst
    print(f'\n  Top 5 最佳:')
    for _, row in df.nlargest(5, 'return_pct').iterrows():
        print(f'    {row["name"]:12s} {row["entry_date"]} → {row["exit_date"]}  {row["return_pct"]:+.2f}% ({row["hold_days"]}天) [{row["exit_reason"]}]')

    print(f'\n  Top 5 最差:')
    for _, row in df.nsmallest(5, 'return_pct').iterrows():
        print(f'    {row["name"]:12s} {row["entry_date"]} → {row["exit_date"]}  {row["return_pct"]:+.2f}% ({row["hold_days"]}天) [{row["exit_reason"]}]')

    return {
        'total': len(df),
        'closed': len(closed),
        'win_rate': win_rate,
        'avg_return': all_ret.mean(),
        'median_return': all_ret.median(),
        'avg_win': avg_win,
        'avg_loss': -avg_loss,
        'pl_ratio': pl_ratio,
        'avg_hold_days': df['hold_days'].mean(),
        'median_hold_days': df['hold_days'].median(),
    }


stats_A = trade_stats(all_trades_A, '策略 A: 温→热买，热→温卖')
stats_B = trade_stats(all_trades_B, '策略 B: 温→热买，温→平卖')

# ============================================================
# 6. 对比总结
# ============================================================
print(f'\n{"=" * 60}')
print(f'  对 比 总 结')
print(f'{"=" * 60}')
if stats_A and stats_B:
    print(f'  {"指标":<20} {"策略 A (热→温卖)":>20} {"策略 B (温→平卖)":>20}')
    print(f'  {"-" * 60}')
    metrics = [
        ('总交易数', 'total'),
        ('胜率', 'win_rate'),
        ('平均收益(%)', 'avg_return'),
        ('中位数收益(%)', 'median_return'),
        ('平均盈利(%)', 'avg_win'),
        ('平均亏损(%)', 'avg_loss'),
        ('盈亏比', 'pl_ratio'),
        ('平均持仓(天)', 'avg_hold_days'),
        ('中位持仓(天)', 'median_hold_days'),
    ]
    for label, key in metrics:
        va = stats_A[key]
        vb = stats_B[key]
        if isinstance(va, float):
            print(f'  {label:<20} {va:>20.2f} {vb:>20.2f}')
        else:
            print(f'  {label:<20} {va:>20} {vb:>20}')
