#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""趋势动物参考切片 vs 本系统指标 对比工具（温度/RS 校准用）。

用法：
    python scripts/calib_compare.py docs/calibration/2026-07-28-reference.csv
    python scripts/calib_compare.py docs/calibration/2026-07-21-reference.csv docs/calibration/2026-07-28-reference.csv

输出：
    docs/calibration/<date>-diff.csv  每个可匹配品种的 ref vs local 对照
    stdout 汇总：温度完全一致率 / ±1 档率 / RS 平均绝对误差

匹配规则：参考名 → symbols(L1/L2) 名称，先精确、再去后缀(Ⅱ/Ⅲ)、再别名表。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from src.db import get_session

# 趋势动物名称 → symbols.name 别名（无法靠去后缀匹配上的）
ALIAS = {
    "石油能源": "石油石化",
    "煤炭能源": "煤炭",
    "金属": "有色金属",
    "军工": "国防军工",
    "证券保险": "非银金融",
    "服装纺织": "纺织服饰",
    "通信服务": "通信",
    "IT服务": "IT服务Ⅱ",
    "传媒电影": "影视院线",
    "饰品消费": "饰品",
    "贸易": "贸易Ⅱ",
    "汽车": "汽车",
    "电力": "电力",
    "种植业": "种植业",
    "公用事业": "公用事业",
    "基建": "基础建设",
    "工程咨询": "工程咨询服务Ⅱ",
    "电机": "电机Ⅱ",
    "综合企业": "综合Ⅱ",
    "基础耗材": "其他轻工制造",  # 占位：申万无同名，匹配不上则计入 unmatched
    "军工电子": "军工电子Ⅱ",
    "电子元件": "元件",
    "白酒": "白酒Ⅱ",
    "中药": "中药Ⅱ",
    "游戏": "游戏Ⅱ",
    "食品加工": "食品加工",
    "旅游": "旅游及景区",
    "金属 ": "有色金属",
}

LEVELS = ["冻", "寒", "凉", "平", "温", "热", "沸"]


def load_symbols():
    with get_session() as s:
        rows = s.execute(
            text("SELECT symbol_id, name, node_type FROM symbols WHERE node_type IN ('industry_l1','industry_l2')")
        ).all()
    return pd.DataFrame(rows, columns=["symbol_id", "name", "node_type"])


def match_symbols(ref_names, symbols):
    """参考名 → (symbol_id, matched_name)。"""
    by_name = dict(zip(symbols["name"], symbols["symbol_id"]))
    stripped = {}
    for name, sid in zip(symbols["name"], symbols["symbol_id"]):
        key = name.rstrip("ⅡⅢ")
        stripped.setdefault(key, (sid, name))
    out = {}
    for rn in ref_names:
        rn = rn.strip()
        if rn in by_name:
            out[rn] = (by_name[rn], rn)
        elif rn in stripped:
            out[rn] = stripped[rn]
        elif ALIAS.get(rn) and ALIAS[rn] in by_name:
            out[rn] = (by_name[ALIAS[rn]], ALIAS[rn])
        elif ALIAS.get(rn) and ALIAS[rn].rstrip("ⅡⅢ") in stripped:
            out[rn] = stripped[ALIAS[rn].rstrip("ⅡⅢ")]
    return out


def compare(ref_csv: str) -> dict:
    ref_csv = Path(ref_csv)
    trade_date = ref_csv.name.split("-reference")[0]  # e.g. 2026-07-28
    ref = pd.read_csv(ref_csv)
    symbols = load_symbols()
    mapping = match_symbols(ref["name"], symbols)
    unmatched = sorted(set(ref["name"].str.strip()) - set(mapping))

    ref["_sid"] = ref["name"].str.strip().map(lambda n: mapping.get(n, (None,))[0])
    matched = ref.dropna(subset=["_sid"]).copy()

    with get_session() as s:
        ind = pd.read_sql(
            text(
                "SELECT symbol_id, temperature, temperature_score, rs_score "
                "FROM daily_indicator WHERE trade_date = :d"
            ),
            s.bind,
            params={"d": trade_date},
        )
    m = matched.merge(ind, left_on="_sid", right_on="symbol_id", how="left")

    m["ref_lv"] = m["ref_temperature"].map({t: i for i, t in enumerate(LEVELS)})
    m["local_lv"] = m["temperature"].map({t: i for i, t in enumerate(LEVELS)})
    m["lv_diff"] = (m["local_lv"] - m["ref_lv"]).abs()
    m["rs_diff"] = m["rs_score"] - m["ref_rs"]

    out_csv = ref_csv.with_name(f"{trade_date}-diff.csv")
    m[["group", "name", "ref_temperature", "ref_rs", "_sid", "temperature", "temperature_score", "rs_score", "lv_diff", "rs_diff"]].to_csv(
        out_csv, index=False, encoding="utf-8-sig"
    )

    valid = m.dropna(subset=["local_lv"])
    n = len(valid)
    exact = (valid["lv_diff"] == 0).sum()
    adj = (valid["lv_diff"] <= 1).sum()
    rs_mae = valid["rs_diff"].abs().mean()
    rs_med = valid["rs_diff"].abs().median()
    print(f"== {trade_date} ==")
    print(f"参考品种 {len(ref)}，匹配 symbols {len(matched)}，当日有指标 {n}；未匹配: {unmatched}")
    print(f"温度完全一致 {exact}/{n} = {exact/n*100:.1f}%   ±1档 {adj}/{n} = {adj/n*100:.1f}%")
    print(f"温度偏差分布: {valid['lv_diff'].value_counts().sort_index().to_dict()}  (正=本地偏热, 负=偏冷)")
    print(f"RS 差(本地-参考): 均值 {valid['rs_diff'].mean():+.1f}  MAE {rs_mae:.1f}  中位 {rs_med:.0f}")
    print(f"diff -> {out_csv}\n")
    return {"date": trade_date, "n": n, "exact": exact / max(n, 1), "adj": adj / max(n, 1), "rs_mae": rs_mae, "df": m}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for csv in sys.argv[1:]:
        compare(csv)
