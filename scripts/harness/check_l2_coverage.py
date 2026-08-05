#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L2 → 个股挂载覆盖率 harness（门禁）。

约束：
- C1: 沪深活跃个股（近 60 天有行情）l2_industry_id 覆盖率 ≥ 98%（退市/长期停牌股申万成分接口不含，不计入分母）
- C2: 每个 industry_l2 至少挂 1 只个股
- C3: 每个 industry_l1 至少挂 1 只个股（经 L2 父链汇聚）
- C4: stock.parent_id == stock.l2_industry_id（挂载一致性）
- C5: 抽样 5 个 L2 实时调 akshare index_component_sw 比对：库中该 L2 个股集合
      必须等于 源成分 ∩ 本表个股集合（不允许挂错/漏挂）

用法：python scripts/harness/check_l2_coverage.py
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from collections import Counter, defaultdict

from sqlalchemy import select

from src.classification import NODE_TYPE_INDUSTRY_L1, NODE_TYPE_INDUSTRY_L2, NODE_TYPE_STOCK
from src.db import get_session
from src.models import Symbol

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"PASS  {name}")
    else:
        failures += 1
        print(f"FAIL  {name}  {detail}")


def main():
    with get_session() as session:
        stocks = session.execute(
            select(Symbol.symbol_id, Symbol.parent_id, Symbol.l2_industry_id)
            .where(Symbol.node_type == NODE_TYPE_STOCK)
        ).all()
        l2_rows = session.execute(
            select(Symbol.symbol_id, Symbol.parent_id).where(Symbol.node_type == NODE_TYPE_INDUSTRY_L2)
        ).all()
        l1_ids = {r[0] for r in session.execute(
            select(Symbol.symbol_id).where(Symbol.node_type == NODE_TYPE_INDUSTRY_L1)
        ).all()}
        # 近 60 天有行情的个股才算活跃；退市/长期停牌股申万成分接口已不含，无法映射，不计入覆盖率分母
        from sqlalchemy import text as _text

        active_ids = {r[0] for r in session.execute(_text(
            "SELECT DISTINCT symbol_id FROM daily_price WHERE trade_date >= date('now', '-60 day')"
        ))}

    total = len(stocks)
    # 北交所（43/83/87/88/89/92 开头）不在申万行业分类体系内，单独统计，不计入覆盖率分母
    bj_prefixes = ("43", "83", "87", "88", "89", "92")
    mainland = [(s, p, l2) for s, p, l2 in stocks if not s.startswith(bj_prefixes) and s in active_ids]
    inactive = [(s, p, l2) for s, p, l2 in stocks if not s.startswith(bj_prefixes) and s not in active_ids]
    bse = [(s, p, l2) for s, p, l2 in stocks if s.startswith(bj_prefixes)]
    mapped = [(s, p, l2) for s, p, l2 in stocks if l2]
    unmapped_mainland = sorted(s for s, p, l2 in mainland if not l2)
    cov = sum(1 for _, _, l2 in mainland if l2) / len(mainland) * 100 if mainland else 0
    check(
        f"C1 沪深活跃个股 L2 覆盖率 ≥98%（{len(mainland) - len(unmapped_mainland)}/{len(mainland)} = {cov:.2f}%）",
        cov >= 98,
        f"未映射 {len(unmapped_mainland)} 只: {unmapped_mainland[:20]}",
    )
    print(f"INFO  沪深非活跃个股 {len(inactive)} 只（退市/长期停牌，申万成分外，不计入分母）")
    bse_mapped = sum(1 for _, _, l2 in bse if l2)
    print(f"INFO  北交所个股 {len(bse)} 只（申万体系外），其中 {bse_mapped} 只有 L2 挂载")

    l2_count = Counter(l2 for _, _, l2 in mapped)
    empty_l2 = [l2 for l2, _ in l2_rows if l2_count.get(l2, 0) == 0]
    check(f"C2 每个 L2 至少 1 只个股（共 {len(l2_rows)} 个 L2）", len(empty_l2) == 0, f"空 L2: {empty_l2[:20]}")

    l2_to_l1 = dict(l2_rows)
    l1_count = Counter()
    for l2, cnt in l2_count.items():
        l1 = l2_to_l1.get(l2)
        if l1:
            l1_count[l1] += cnt
    empty_l1 = [l1 for l1 in l1_ids if l1_count.get(l1, 0) == 0]
    check(f"C3 每个 L1 至少 1 只个股（共 {len(l1_ids)} 个 L1）", len(empty_l1) == 0, f"空 L1: {sorted(empty_l1)}")

    bad_parent = [(s, p, l2) for s, p, l2 in mapped if p != l2]
    check(
        "C4 stock.parent_id == l2_industry_id",
        len(bad_parent) == 0,
        f"{len(bad_parent)} 只不一致，样例: {bad_parent[:5]}",
    )

    # C5: 抽样实时比对源数据
    import akshare as ak

    random.seed(42)
    sample_l2 = random.sample([l2 for l2, _ in l2_rows], k=min(5, len(l2_rows)))
    stock_ids = {s for s, _, _ in stocks}
    db_by_l2 = defaultdict(set)
    for s, _, l2 in mapped:
        db_by_l2[l2].add(s)
    for l2_id in sample_l2:
        df = ak.index_component_sw(symbol=l2_id.replace('SW_', '', 1))
        col = '证券代码' if '证券代码' in df.columns else df.columns[1]
        src = {str(c).strip().split('.')[0].zfill(6) for c in df[col].tolist()}
        expected = src & stock_ids
        actual = db_by_l2.get(l2_id, set())
        check(
            f"C5 {l2_id} 库内成分 == 源成分∩本表（{len(actual)}/{len(expected)}）",
            actual == expected,
            f"多挂: {sorted(actual - expected)[:5]} 漏挂: {sorted(expected - actual)[:5]}",
        )

    print("\n全部通过" if failures == 0 else f"\n{failures} 项约束失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
