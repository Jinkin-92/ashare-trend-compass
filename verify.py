#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段 4 验证脚本：检查所有 JSON 端点 + 模拟前端渲染。

用法：python verify.py
"""
import json
import re
from pathlib import Path

web = Path('web/data')


def load(name):
    p = web / name
    if not p.exists():
        return None
    text = p.read_text(encoding='utf-8')
    # 严格检查 NaN/Infinity 泄漏
    if 'NaN' in text or 'Infinity' in text:
        return f'INVALID JSON: {name} contains NaN/Infinity'
    return json.loads(text)


def check(label, cond, detail=''):
    mark = '[OK]' if cond else '[FAIL]'
    print(f'  {mark} {label}{": " + detail if detail else ""}')
    return cond


print('=' * 60)
print('阶段 4 验证')
print('=' * 60)

# 1. JSON 端点
print('\n[1] JSON 端点 + 合法性')
d_top = load('top.json')
check('top.json', d_top is not None and isinstance(d_top, dict))

d_idx = load('index-l1.json')
check('index-l1.json groups', d_idx and len(d_idx.get('groups', [])) > 0, f"{len(d_idx.get('groups', [])) if d_idx else 0} 组")

# index-l1 完整性
if d_idx:
    indexes = [g for g in d_idx['groups'] if g['node_type'] == 'index']
    l1s = [g for g in d_idx['groups'] if g['node_type'] == 'industry_l1']
    check('index-l1 包含 7 指数', len(indexes) == 7, f'实际 {len(indexes)}')
    check('index-l1 包含 31 L1', len(l1s) == 31, f'实际 {len(l1s)}')
    # 至少有一些指标不为空
    with_temp = [g for g in d_idx['groups'] if g.get('temperature')]
    check('index-l1 有温度数据', len(with_temp) > 10, f'{len(with_temp)} 行有温度')

# 2. L1 详情完整性
print('\n[2] L1 详情（l1-*.json）')
l1_files = sorted(web.glob('l1-*.json'))
check('l1-*.json 数量 = 31', len(l1_files) == 31, f'实际 {len(l1_files)}')

if l1_files:
    # 任选一个 L1（l1 页设计为只展示 l2_children，不含 stocks 列表）
    sample = load(l1_files[0].name)
    if sample and not isinstance(sample, str):
        check('l1 样本包含 l2_children', isinstance(sample.get('l2_children'), list) and len(sample.get('l2_children', [])) > 0,
              f"{len(sample.get('l2_children', []))} 个")

# 3. L2 详情（数量与 symbols 表 industry_l2 一致；成分股必须挂载）
print('\n[3] L2 详情（l2-*.json）')
l2_files = sorted(web.glob('l2-*.json'))
try:
    import sqlite3
    _con = sqlite3.connect('data/trend_compass.db')
    # 8 个 L2 指数申万官网无行情（data_status=no_data），exporter 按设计不导出
    _l2_expected = _con.execute(
        "SELECT COUNT(*) FROM symbols WHERE node_type='industry_l2' AND COALESCE(data_status,'ok') != 'no_data'"
    ).fetchone()[0]
    _con.close()
except Exception:
    _l2_expected = None
if _l2_expected:
    check(f'l2-*.json 数量 = 可导出 L2 数（{_l2_expected}）', len(l2_files) == _l2_expected, f'实际 {len(l2_files)}')
else:
    check('l2-*.json 数量 > 100', len(l2_files) > 100, f'实际 {len(l2_files)}')

if l2_files:
    empty_stocks = []
    for f in l2_files:
        d = load(f.name)
        if d and not isinstance(d, str) and not d.get('stocks'):
            empty_stocks.append(f.name)
    check('所有 l2-*.json 均挂载成分股', len(empty_stocks) == 0,
          f'{len(empty_stocks)} 个空: {empty_stocks[:5]}')

# 4. detail 详情
print('\n[4] 详情数据（indicator-*.json）')
ind_files = sorted((web / 'indicators').glob('indicator-*.json'))
check('indicator-*.json 至少 100 个', len(ind_files) > 100, f'实际 {len(ind_files)}')

# 5. 静态 HTML 可访问性
print('\n[5] 关键文件')
for name in ['index.html', 'l1.html', 'l2.html', 'detail.html',
             'js/list-shared.js', 'js/app.js', 'js/l1.js', 'js/l2.js', 'css/style.css']:
    p = Path('web') / name
    check(name, p.exists())

# 6. 数据合理范围
print('\n[6] 数据合理性')
if d_idx and 'groups' in d_idx:
    pcts = [g['pct_chg'] for g in d_idx['groups'] if g.get('pct_chg') is not None]
    if pcts:
        check('pct_chg 范围合理 (-15, +15)', all(-15 < p < 15 for p in pcts), f'范围 [{min(pcts):.2f}, {max(pcts):.2f}]')

print('\n' + '=' * 60)
print('验证完成')
