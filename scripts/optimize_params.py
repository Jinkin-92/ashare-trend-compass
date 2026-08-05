#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Temperature parameter optimization script."""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from src.db import get_session
from sqlalchemy import text
from src.indicators.temperature import (
    calculate_ma, calculate_atr, run_temperature_state_machine,
    TEMPERATURE_LEVELS
)
import src.indicators.temperature as temp_mod

# Load reference + symbols
ref = pd.read_csv('docs/calibration/2026-08-04-reference.csv')
ALIAS = {
    '石油能源': '石油石化', '煤炭能源': '煤炭', '金属': '有色金属',
    '军工': '国防军工', '证券保险': '非银金融', 'IT服务': 'IT服务Ⅱ',
    '传媒电影': '影视院线', '电子元件': '元件', '军工电子': '军工电子Ⅱ',
    '白酒': '白酒Ⅱ', '中药': '中药Ⅱ', '游戏': '游戏Ⅱ',
    '工程咨询': '工程咨询服务Ⅱ', '综合企业': '综合Ⅱ', '电力': '电力',
    '种植业': '种植业', '公用事业': '公用事业', '基建': '基础建设',
}

with get_session() as s:
    syms = pd.DataFrame(
        s.execute(text(
            "SELECT symbol_id, name FROM symbols "
            "WHERE node_type IN ('industry_l1','industry_l2')"
        )).all(),
        columns=['symbol_id', 'name']
    )

by_name = dict(zip(syms['name'], syms['symbol_id']))
stripped = {}
for name, sid in zip(syms['name'], syms['symbol_id']):
    stripped.setdefault(name.rstrip('ⅡⅢ'), (sid, name))


def get_sid(rn):
    rn = rn.strip()
    if rn in by_name:
        return by_name[rn]
    if rn in stripped:
        return stripped[rn][0]
    a = ALIAS.get(rn)
    if a and a in by_name:
        return by_name[a]
    if a and a.rstrip('ⅡⅢ') in stripped:
        return stripped[a.rstrip('ⅡⅢ')][0]
    return None


ref['_sid'] = ref['name'].apply(get_sid)
matched = ref.dropna(subset=['_sid']).copy()

LEVELS = TEMPERATURE_LEVELS
lv_map = {t: i for i, t in enumerate(LEVELS)}

# Preload price data
price_cache = {}
with get_session() as s:
    for sid in matched['_sid'].unique():
        rows = s.execute(
            text("SELECT close, high, low FROM daily_price WHERE symbol_id=:sid ORDER BY trade_date"),
            {'sid': sid}
        ).all()
        if rows:
            price_cache[sid] = pd.DataFrame(rows, columns=['close', 'high', 'low'])


def compute_scores(mf, ms, rw, rw_hi, rw_lo, dw, rwt, vc, sp):
    """Compute smoothed scores for all matched sectors."""
    results = {}
    for _, row in matched.iterrows():
        sid = row['_sid']
        if sid not in price_cache:
            continue
        df = price_cache[sid]
        close, high, low = df['close'], df['high'], df['low']
        n = len(close)

        ma_f = calculate_ma(close, mf)
        ma_s = calculate_ma(close, ms)

        atr_long_w = 60 if n >= 250 else (20 if n >= 60 else 5)
        atr_short = calculate_atr(high, low, close, window=max(2, atr_long_w // 6))
        atr_long = calculate_atr(high, low, close, window=atr_long_w)
        atr_ratio = (atr_short / atr_long.replace({0: np.nan})).fillna(1.0)

        weights = np.linspace(rw_hi, rw_lo, len(rw))
        weights = weights / weights.sum()
        rocs = []
        for w in rw:
            r = (close / close.shift(w) - 1) * 100
            r = np.sign(r) * np.minimum(np.abs(r), 30)
            rocs.append(r)
        roc_val = pd.concat(rocs, axis=1).mul(weights, axis=1).sum(axis=1)

        direction = (
            (close / ma_s - 1).clip(-0.05, 0.05) / 0.05 * 0.7
            + (ma_f / ma_s - 1).clip(-0.03, 0.03) / 0.03 * 0.3
        )

        x = (atr_ratio - 1.5) / 0.15
        gate = 1.0 / (1.0 + np.exp(-x))
        va = direction * vc * gate

        score_raw = direction * dw + roc_val * rwt + va
        score_smooth = score_raw.ewm(span=sp, min_periods=1).mean()

        max_w = max(rw)
        valid_start = max_w
        last_score = score_smooth.iloc[-1] if n > valid_start else np.nan

        results[row['name']] = (last_score, score_smooth, valid_start, n, row['ref_temperature'])
    return results


def evaluate(scores_data, thresholds, confirm_days=2, buffer_days=3):
    """Evaluate a threshold set against reference."""
    boil, hot, warm, cool_b, cold_b, freeze = thresholds

    def raw_bucket(score):
        if score >= boil:
            return 6
        if score >= hot:
            return 5
        if score >= warm:
            return 4
        if score > cool_b:
            return 3
        if score > cold_b:
            return 2
        if score > freeze:
            return 1
        return 0

    orig_bucket = temp_mod._raw_bucket_idx
    orig_confirm = temp_mod.CONFIRM_DAYS
    orig_buffer = temp_mod.EXTREME_BUFFER_DAYS

    temp_mod._raw_bucket_idx = raw_bucket
    temp_mod.CONFIRM_DAYS = confirm_days
    temp_mod.EXTREME_BUFFER_DAYS = buffer_days

    results = []
    for name, (score, smooth, valid_start, n, ref_t) in scores_data.items():
        if pd.isna(score) or n <= valid_start:
            continue
        displayed = run_temperature_state_machine(smooth.iloc[valid_start:])
        local_t = displayed[-1] if displayed else None
        ref_lv = lv_map.get(ref_t)
        local_lv = lv_map.get(local_t) if local_t else None
        if ref_lv is not None and local_lv is not None:
            results.append({
                'name': name, 'ref': ref_t, 'local': local_t,
                'score': score,
                'diff': abs(local_lv - ref_lv),
                'signed': local_lv - ref_lv,
            })

    temp_mod._raw_bucket_idx = orig_bucket
    temp_mod.CONFIRM_DAYS = orig_confirm
    temp_mod.EXTREME_BUFFER_DAYS = orig_buffer

    if not results:
        return None
    df_r = pd.DataFrame(results)
    n = len(df_r)
    exact = (df_r['diff'] == 0).sum()
    adj = (df_r['diff'] <= 1).sum()
    return exact, adj, n, df_r


# Compute scores for Set C (best so far)
print("=== Set C: MA5/MA10, ROC(5,10,20,60,120), dir*20, roc*0.7, vol*8, span=3 ===")
scores_C = compute_scores(5, 10, (5, 10, 20, 60, 120), 0.5, 0.1, 20, 0.7, 8, 3)

threshold_sets = [
    ('orig',       (50, 25, 3, -19, -50, -80)),
    ('shift10',    (40, 15, -7, -29, -60, -90)),
    ('shift15',    (35, 10, -12, -34, -65, -95)),
    ('shift20',    (30, 5, -17, -39, -70, -100)),
    ('opt1',       (15, 8, 0, -15, -40, -70)),
    ('opt2',       (12, 5, -3, -15, -35, -65)),
    ('opt3',       (10, 3, -5, -20, -40, -70)),
    ('opt4',       (8, 2, -8, -22, -45, -75)),
    ('opt5',       (18, 6, -3, -18, -38, -68)),
    ('opt6',       (20, 8, -2, -20, -40, -70)),
    ('opt7',       (14, 5, -5, -18, -35, -65)),
    ('opt8',       (16, 6, -2, -16, -38, -68)),
]

best = None
for tname, thresholds in threshold_sets:
    for cd in [2, 1]:
        for bd in [3, 2, 1]:
            result = evaluate(scores_C, thresholds, cd, bd)
            if result:
                exact, adj, n, df_r = result
                sm = df_r['signed'].mean()
                pct = adj / n
                if pct >= 0.75:
                    print(f"  {tname:8s} cd={cd} bd={bd}: "
                          f"exact={exact}/{n}={exact/n*100:.1f}% "
                          f"±1={adj}/{n}={pct*100:.1f}% "
                          f"signed={sm:+.2f}")
                    if best is None or exact > best[1]:
                        best = (tname, exact, adj, n, thresholds, cd, bd, df_r)

# Show best detail
if best:
    tname, exact, adj, n, thresholds, cd, bd, df_r = best
    print(f"\n=== Best: {tname} cd={cd} bd={bd} "
          f"exact={exact}/{n}={exact/n*100:.1f}% ±1={adj}/{n}={adj/n*100:.1f}% ===")
    print(f"  Thresholds: {thresholds}")
    for _, r in df_r.sort_values('ref').iterrows():
        flag = 'OK' if r['diff'] == 0 else ('~ ' if r['diff'] <= 1 else 'X  ')
        print(f"  {flag} {r['name']:8s} ref={r['ref']:2s} "
              f"local={r['local']:2s} score={r['score']:7.1f} diff={r['diff']}")

# Also test Set D (reduced dir weight) with best thresholds
print("\n=== Set D: MA5/MA10, ROC(5,10,20,60,120), dir*12, roc*1.0, vol*4, span=2 ===")
scores_D = compute_scores(5, 10, (5, 10, 20, 60, 120), 0.5, 0.1, 12, 1.0, 4, 2)
for tname, thresholds in threshold_sets:
    for cd in [2, 1]:
        for bd in [3, 2, 1]:
            result = evaluate(scores_D, thresholds, cd, bd)
            if result:
                exact, adj, n, df_r = result
                pct = adj / n
                if pct >= 0.80:
                    sm = df_r['signed'].mean()
                    print(f"  {tname:8s} cd={cd} bd={bd}: "
                          f"exact={exact}/{n}={exact/n*100:.1f}% "
                          f"±1={adj}/{n}={pct*100:.1f}% "
                          f"signed={sm:+.2f}")
