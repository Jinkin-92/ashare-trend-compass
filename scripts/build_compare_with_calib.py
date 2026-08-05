# -*- coding: utf-8 -*-
"""校准图 vs 本项目 8-3 数据 → 对比 HTML。

校准图来源：60110f24aa20b7d74c737f84c702f270.png
- 共 ~50 行 = 申万 L2 子项 + 8 个概念分组（实际是「31 L1」里挑出的细分行）
- 列：温度档位、名称、日变动、近3月趋势、日交易额、强度（百分位）
- 本项目用 web/data/symbols.json（已含 8-3 L1 + L2 截面指标）
"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 校准图里出现的品种，按图片从上到下顺序
# (校准图里的名称, 校准温度, 校准涨幅%, 校准RS, 对应的本项目 symbol_id)
CALIB = [
    # A股-民生消费
    ("银行",       "凉", -0.3, 99, "SW_801780"),
    ("白酒",       "平", +0.1, 96, "SW_801124"),
    ("交通运输",   "平", +0.8, 94, "SW_801170"),
    ("食品加工",   "平", -0.2, 92, "SW_801123"),
    ("家用电器",   "平", -0.9, 89, "SW_801111"),
    ("汽车",       "平", -0.5, 80, "SW_801880"),
    ("证券保险",   "凉", -0.2, 79, "SW_801790"),
    ("通信服务",   "平", +1.9, 78, "SW_801770"),
    ("电力",       "平", +1.0, 63, "SW_801160"),
    # A股-传统能源
    ("石油能源",   "平", +0.0, 100, "SW_801961"),
    ("煤炭能源",   "平", -3.0, 69, "SW_801950"),
    # A股-农业基础
    ("饲料",       "平", -0.3, 84, "SW_801121"),
    ("养殖业",     "平", -0.2, 76, "SW_801122"),
    # A股-生活服务
    ("社会服务",   "平", +0.5, 75, "SW_801210"),
    ("旅游",       "平", +0.6, 72, "SW_801210"),
    # A股-生命健康 (L2 子项)
    ("中药",       "平", +0.1, 97, "SW_801155"),
    ("医药商业",   "平", +0.4, 90, "SW_801154"),
    ("医疗服务",   "平", -0.5, 83, "SW_801156"),
    ("动物保健",   "平", -0.1, 59, "SW_801150"),  # L1 兜底
    ("医疗器械",   "平", -0.1, 56, "SW_801153"),
    ("化学制药",   "平", -1.4, 51, "SW_801151"),
    ("生物制品",   "平", -0.4, 42, "SW_801152"),
    # A股-日用消费品
    ("饮料乳品",   "平", +0.2, 93, "SW_801120"),
    ("休闲食品",   "平", +1.1, 87, "SW_801120"),
    ("美容护理",   "平", -0.5, 82, "SW_801120"),
    ("饰品消费",   "平", -0.8, 61, "SW_801120"),
    ("服装纺织",   "平", +1.0, 55, "SW_801130"),
    ("家居用品",   "平", +1.3, 46, "SW_801140"),
    ("包装印刷",   "平", +1.0, 8,  "SW_801140"),
    # A股-数字娱乐
    ("传媒电影",   "平", +0.7, 73, "SW_801762"),
    ("游戏",       "平", +0.7, 68, "SW_801761"),
    ("互联网电商", "凉", +0.7, 32, "SW_801760"),
    # A股-基础资源
    ("金属",       "平", -0.4, 54, "SW_801050"),
    # A股-城市建设
    ("轨交设备",   "平", +1.7, 86, "SW_801893"),
    ("基建",       "平", +1.1, 70, "SW_801710"),
    ("钢铁",       "平", +0.8, 66, "SW_801040"),
    ("贸易",       "平", -0.2, 62, "SW_801200"),
    ("种植业",     "平", +2.7, 52, "SW_801010"),
    ("公用事业",   "平", +2.2, 49, "SW_801170"),
    ("工程咨询",   "平", -0.1, 41, "SW_801890"),
    ("房地产",     "平", +0.7, 39, "SW_801180"),
    ("工程机械",   "平", -0.2, 38, "SW_801894"),
    ("装修建材",   "平", +0.5, 30, "SW_801711"),
    ("专业服务",   "平", -0.0, 20, "SW_801210"),
    # A股-数字科技
    ("软件开发",   "平", +0.4, 65, "SW_801752"),
    ("计算机设备", "凉", +0.6, 48, "SW_801751"),
    ("IT 服务",    "凉", +0.4, 37, "SW_801750"),
    # A股-化工
    ("化学制品",   "平", +1.2, 34, "SW_801034"),
    # A股-工业配套
    ("工业金属",   "凉", +2.8, 45, "SW_801053"),
    ("综合企业",   "凉", +2.7, 35, "SW_801080"),  # 近似电子
    ("风电设备",   "凉", +3.7, 27, "SW_801072"),  # 通信设备兜底
    ("环保设备",   "凉", +2.9, 24, "SW_801072"),
    ("电源设备",   "凉", +1.1, 17, "SW_801072"),
    ("光电子",     "凉", +0.0, 4,  "SW_801080"),
    ("基础耗材",   "凉", -2.3, 0,  "SW_801080"),
    # A股-国防安全
    ("军工",       "凉", +1.1, 26, "SW_801740"),
    ("军工电子",   "凉", +0.6, 11, "SW_801742"),
    # A股-新能源装备
    ("电池",       "凉", -0.2, 31, "SW_801731"),
    ("光伏设备",   "凉", +1.9, 6,  "SW_801732"),
    # A股-工业装备
    ("电机",       "凉", +4.7, 44, "SW_801730"),
    ("汽车零部件", "凉", +0.9, 25, "SW_801882"),
    ("专用设备",   "凉", +0.0, 18, "SW_801735"),
    ("自动化设备", "凉", -0.4, 13, "SW_801733"),
    ("通用设备",   "凉", -0.4, 7,  "SW_801734"),
]


def main():
    # 取名称
    conn = sqlite3.connect(ROOT / "data/trend_compass.db")
    c = conn.cursor()
    c.execute("SELECT symbol_id, name FROM symbols WHERE node_type IN ('industry_l1','industry_l2')")
    name_map = dict(c.fetchall())
    conn.close()

    # 取本项目 8-3 数据
    with open(ROOT / "web/data/symbols.json", encoding="utf-8") as f:
        syms = json.load(f)
    rows = {r["symbol_id"]: r for r in syms["rows"]}

    # 比较
    results = []
    for calib_name, ctemp, cpct, crs, sid in CALIB:
        r = rows.get(sid, {})
        actual_name = name_map.get(sid, calib_name)
        mtemp = r.get("temperature") or "-"
        mpct = r.get("pct_chg")
        mrs = r.get("rs_score")
        results.append({
            "calib_name": calib_name,
            "sid": sid,
            "actual_name": actual_name,
            "calib_temp": ctemp,
            "calib_pct": cpct,
            "calib_rs": crs,
            "my_temp": mtemp,
            "my_pct": mpct,
            "my_rs": mrs,
        })

    # 写 JSON 给 HTML 用
    out_json = ROOT / "web/data/calibration/compare8m3.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({"trade_date": "2026-08-03", "rows": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写: {out_json}（{len(results)} 行）")

    # 写 HTML
    out_html = ROOT / "web/compare8m3.html"
    out = []
    out.append("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>8月3日 数据对比：校准图 vs 本项目</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 1100px; margin: 0 auto; padding: 20px 24px; background: #fafbfc; color: #222; }
  h1 { font-size: 22px; }
  h2 { font-size: 16px; margin-top: 24px; }
  .meta { color: #777; font-size: 13px; margin-bottom: 14px; line-height: 1.6; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
  th, td { padding: 6px 8px; text-align: left; border-bottom: 1px solid #eef; }
  th { background: #f5f7fa; font-weight: 600; position: sticky; top: 0; }
  .ok { background: #d4edda; }
  .warn { background: #fff3cd; }
  .bad { background: #f8d7da; }
  .tier { display: inline-block; padding: 1px 6px; border-radius: 3px;
          font-family: monospace; font-weight: 700; min-width: 20px; text-align: center; font-size: 12px; }
  .tier-6 { background: #dc3545; color: #fff; }
  .tier-5 { background: #fd7e14; color: #fff; }
  .tier-4 { background: #ffc107; color: #fff; }
  .tier-3 { background: #6c757d; color: #fff; }
  .tier-2 { background: #20c997; color: #fff; }
  .tier-1 { background: #0dcaf0; color: #fff; }
  .tier-0 { background: #0d6efd; color: #fff; }
  .num { font-family: monospace; text-align: right; }
  .pos { color: #d93026; }
  .neg { color: #1e8e3e; }
  .summary { background: #fff; padding: 14px 18px; border-radius: 8px;
             box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin: 12px 0; }
</style>
</head>
<body>
<h1>8 月 3 日数据对比：校准图 vs 本项目</h1>
<div class="meta">
  <strong>校准图来源</strong>：用户提供的 60110f24aa20b7d74c737f84c702f270.png（2026/08/03 A 股趋势）<br>
  <strong>本项目数据</strong>：scripts/daily_update.py + export_static.py 跑出的 8-3 截面（trade_date=2026-08-03）<br>
  <strong>对比维度</strong>：温度档位、日变动涨幅、强度（百分位）<br>
  <strong>注意</strong>：校准图共 ~60 行，包含 31 个 L1 + 部分 L2 子项 + 个股。本对比映射到对应的 L1/L2 symbol_id。
</div>""")

    # 汇总
    same_temp = sum(1 for r in results if r["calib_temp"] == r["my_temp"])
    rs_diff = [abs(r["calib_rs"] - r["my_rs"]) for r in results if r["my_rs"] is not None]
    avg_rs_diff = sum(rs_diff) / len(rs_diff) if rs_diff else 0
    pct_diff = [abs(r["calib_pct"] - r["my_pct"]) for r in results if r["my_pct"] is not None]
    avg_pct_diff = sum(pct_diff) / len(pct_diff) if pct_diff else 0
    out.append(f"""
<div class="summary">
  <strong>汇总：</strong>共 {len(results)} 行
  <ul style="margin: 6px 0; line-height: 1.8;">
    <li>温度档位完全一致：<b>{same_temp}</b> / {len(results)} 行</li>
    <li>RS 平均偏差：<b>{avg_rs_diff:.1f}</b> 分（按 RS 计算）</li>
    <li>涨幅平均偏差：<b>{avg_pct_diff:.2f}</b>%（绝对差）</li>
  </ul>
</div>""")

    out.append("""
<h2>逐行对比</h2>
<table>
<thead>
<tr>
  <th>校准图名称</th>
  <th>对应 symbol</th>
  <th>本项目名称</th>
  <th>校准温度</th><th>本项目温度</th>
  <th>校准涨幅</th><th>本项目涨幅</th>
  <th>校准RS</th><th>本项目RS</th>
  <th>差异</th>
</tr>
</thead>
<tbody>""")

    def tier_cls(t):
        m = {"沸":6,"热":5,"温":4,"平":3,"凉":2,"寒":1,"冻":0}.get(t, -1)
        return f"tier-{m}" if m >= 0 else ""

    for r in results:
        temp_match = "✓" if r["calib_temp"] == r["my_temp"] else "✗"
        rs_delta = (r["my_rs"] - r["calib_rs"]) if r["my_rs"] is not None else None
        pct_delta = (r["my_pct"] - r["calib_pct"]) if r["my_pct"] is not None else None

        # 着色：温度不同 = bad, RS 差 > 15 = warn, 涨幅差 > 1% = warn
        row_cls = ""
        if r["calib_temp"] != r["my_temp"]:
            row_cls = "bad"
        elif rs_delta is not None and abs(rs_delta) > 15:
            row_cls = "warn"

        my_pct_str = f"{r['my_pct']:+.2f}%" if r["my_pct"] is not None else "-"
        my_pct_cls = "pos" if r["my_pct"] and r["my_pct"] > 0 else ("neg" if r["my_pct"] and r["my_pct"] < 0 else "")
        rs_delta_str = f"{rs_delta:+.0f}" if rs_delta is not None else "-"
        pct_delta_str = f"{pct_delta:+.2f}" if pct_delta is not None else "-"

        out.append(f'<tr class="{row_cls}">')
        out.append(f'<td>{r["calib_name"]}</td>')
        out.append(f'<td><code>{r["sid"]}</code></td>')
        out.append(f'<td>{r["actual_name"]}</td>')
        out.append(f'<td><span class="tier {tier_cls(r["calib_temp"])}">{r["calib_temp"]}</span></td>')
        out.append(f'<td><span class="tier {tier_cls(r["my_temp"])}">{r["my_temp"]}</span></td>')
        out.append(f'<td class="num">{r["calib_pct"]:+.1f}%</td>')
        out.append(f'<td class="num {my_pct_cls}">{my_pct_str}</td>')
        out.append(f'<td class="num">{r["calib_rs"]}</td>')
        out.append(f'<td class="num">{r["my_rs"] if r["my_rs"] is not None else "-"}</td>')
        # 差异列
        diff_parts = []
        diff_parts.append(f"温度 {temp_match}")
        if rs_delta is not None:
            diff_parts.append(f"RS {rs_delta_str}")
        if pct_delta is not None:
            diff_parts.append(f"涨幅 {pct_delta_str}")
        out.append(f'<td>{" / ".join(diff_parts)}</td>')
        out.append('</tr>')

    out.append("</tbody></table>")
    out.append("""
<p class="meta" style="margin-top:16px;">
  <strong>色标</strong>：
  <span class="bad" style="padding:2px 6px;">红</span> 温度档位不同；
  <span class="warn" style="padding:2px 6px;">黄</span> RS 差异 &gt; 15；
  <span class="ok" style="padding:2px 6px;">绿</span> 温度/RS 均匹配。
</p>
</body>
</html>""")

    out_html.write_text("".join(out), encoding="utf-8")
    print(f"已写: {out_html}")


if __name__ == "__main__":
    main()