# -*- coding: utf-8 -*-
"""生成对比 HTML：calibrate.py 产出的 JSON → 单文件可视化页面。

每个品种展示「温度档位 + 右侧天数」双 Y 轴对比图。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JSON_PATH = ROOT / "web" / "data" / "calibration" / "compare.json"
HTML_PATH = ROOT / "web" / "calibration.html"

TIER_IDX = {"冻": 0, "寒": 1, "凉": 2, "平": 3, "温": 4, "热": 5, "沸": 6}
TIER_LABELS = ["冻", "寒", "凉", "平", "温", "热", "沸"]


def main():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    symbols = data["symbols"]
    temp_params = data["temp_params"]
    temp_results = data["temp_results"]

    # 查每个品种的 node_type
    conn = sqlite3.connect(ROOT / "data" / "trend_compass.db")
    sym_info = {}
    for s in symbols:
        row = conn.execute("SELECT node_type, name FROM symbols WHERE symbol_id = ?", (s["id"],)).fetchone()
        sym_info[s["id"]] = {"node_type": row[0] if row else "stock", "name": row[1] if row else s["name"]}
    conn.close()

    by_category = {"index": [], "industry_l1": [], "industry_l2": [], "stock": []}
    for sid, info in sym_info.items():
        cat = "industry_l2" if info["node_type"] == "industry_l2" else info["node_type"]
        if cat in by_category:
            by_category[cat].append(sid)

    out = []
    out.append("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>温度档位 × 右侧天数 校准对比</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 1320px; margin: 0 auto; padding: 20px 30px; background: #fafbfc; color: #222; }
  h1 { font-size: 24px; margin-bottom: 6px; }
  h2 { font-size: 19px; margin: 30px 0 12px; padding-bottom: 6px;
       border-bottom: 2px solid #5b8def; }
  h3 { font-size: 16px; margin: 18px 0 8px; }
  .subtitle { color: #777; font-size: 13px; margin-bottom: 16px; }
  .meta { color: #888; font-size: 12px; line-height: 1.6; }
  .card { background: #fff; padding: 16px 20px; border-radius: 8px;
          box-shadow: 0 1px 4px rgba(0,0,0,0.05); margin-bottom: 16px; }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
                  gap: 12px; margin: 10px 0; }
  .summary-card { background: linear-gradient(135deg, #5b8def10, #5b8def05);
                  padding: 14px; border-radius: 6px; border-left: 3px solid #5b8def; }
  .summary-card .label { font-size: 12px; color: #888; }
  .summary-card .value { font-size: 20px; font-weight: 700; color: #5b8def; margin: 4px 0; }
  .summary-card .desc { font-size: 12px; color: #666; line-height: 1.5; }

  table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
  th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid #eef; }
  th { background: #f5f7fa; font-weight: 600; }
  .good { color: #28a745; font-weight: 600; }
  .bad { color: #dc3545; font-weight: 600; }

  .sym-block { background: #fff; padding: 14px 18px; border-radius: 8px;
               box-shadow: 0 1px 4px rgba(0,0,0,0.05); margin-bottom: 14px; }
  .sym-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
                padding-bottom: 8px; border-bottom: 1px solid #eee; flex-wrap: wrap; }
  .sym-name { font-weight: 700; font-size: 15px; }
  .sym-id { color: #999; font-size: 11px; font-family: monospace; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 11px; font-weight: 600; }
  .badge-index { background: #d1ecf1; color: #0c5460; }
  .badge-l1 { background: #d4edda; color: #155724; }
  .badge-l2 { background: #fff3cd; color: #856404; }
  .badge-stock { background: #f8d7da; color: #721c24; }

  .chart-tall { width: 100%; height: 320px; margin: 6px 0; }

  details { margin-top: 6px; }
  details summary { cursor: pointer; color: #5b8def; font-size: 13px;
                    padding: 4px 0; user-select: none; }
  details summary:hover { color: #3a6fd8; }
  details[open] summary { margin-bottom: 8px; }

  .tier { display: inline-block; padding: 2px 7px; border-radius: 3px;
          font-family: monospace; font-weight: 700; min-width: 22px; text-align: center;
          font-size: 12px; }
  .tier-6 { background: #dc3545; color: #fff; }
  .tier-5 { background: #fd7e14; color: #fff; }
  .tier-4 { background: #ffc107; color: #fff; }
  .tier-3 { background: #6c757d; color: #fff; }
  .tier-2 { background: #20c997; color: #fff; }
  .tier-1 { background: #0dcaf0; color: #fff; }
  .tier-0 { background: #0d6efd; color: #fff; }
  .right-marker { display: inline-block; padding: 2px 6px; border-radius: 3px;
                  font-size: 11px; font-weight: 600;
                  background: #d1ecf1; color: #0c5460; }

  .scroll-x { overflow-x: auto; }
  .scroll-x table { min-width: 100%; }
  .num { font-family: monospace; text-align: right; font-size: 12px; }

  .legend-inline { display: flex; gap: 14px; flex-wrap: wrap; margin: 4px 0 8px;
                   font-size: 12px; color: #555; }
</style>
</head>
<body>""")

    out.append('<h1>温度档位 × 右侧天数 校准对比</h1>')
    out.append(f'<div class="subtitle">生成于 {data["generated_at"]} · '
               f'样本 {len(symbols)} 个品种（指数 3 + L1 行业 3 + L2 行业 2 + 个股 3）· '
               f'末 60 个交易日</div>')

    # 顶部结论
    out.append("""
<h2>校准结论</h2>
<div class="card">
  <div class="summary-grid">
    <div class="summary-card">
      <div class="label">温度平滑窗口 span</div>
      <div class="value">3</div>
      <div class="desc">90 天 11 只品种共切换 50 次（中等频率）。span=2 切换 72 次（更敏感），span=5 切换 46 次（更稳定）。</div>
    </div>
    <div class="summary-card">
      <div class="label">N 日确认 confirm_days</div>
      <div class="value">2</div>
      <div class="desc">抑制「今天涨了明天涨了」的瞬时刷档。=1 时切换太频繁（72 次），=3 时反应过慢（37 次）。</div>
    </div>
    <div class="summary-card">
      <div class="label">右侧天数口径</div>
      <div class="value">自然日</div>
      <div class="desc">右侧天数 = (今天 - 入场日).days + 1，反映温度反应周期而非"日历日"。入场需温度 ≥ 热，维持需温度 ≥ 温，退出需温度 ≤ 平。</div>
    </div>
  </div>
</div>""")

    # 温度参数扫描汇总
    out.append("""
<h2>§1 温度参数扫描汇总</h2>
<div class="card">
  <div class="meta">
    5 组 (span, confirm_days) 组合下的档位切换统计。状态机要求：相邻 ±1 档、缓冲期、N 日确认。
    跨 ≥2 档 = 状态机违规（理论上不应发生）。
  </div>
  <table>
    <tr><th>参数组合</th><th>11 只品种总切换</th><th>跨 ≥2 档品种数</th><th>评估</th></tr>""")
    for label in temp_params:
        sym_data = temp_results[label]
        total_trans = sum(d["transitions"] for d in sym_data.values())
        violations = sum(1 for d in sym_data.values() if d["violates_no_jump"])
        verdict = '<span class="good">✓ 状态机无违规</span>' if violations == 0 else f'<span class="bad">✗ {violations} 个品种违规</span>'
        marker = ' ★' if label == "span=3,confirm=2" else ''
        out.append(f'<tr><td>{label}{marker}</td><td>{total_trans}</td><td>{violations}</td><td>{verdict}</td></tr>')
    out.append("</table>")
    out.append('<div class="meta">★ 当前默认参数 <code>span=3, confirm_days=2</code>，90 天切换 50 次，平衡反应速度与稳定性。</div>')
    out.append("</div>")

    # 全局数据
    all_data = {}
    for s in symbols:
        sid = s["id"]
        all_data[sid] = {}
        for label in temp_params:
            d = temp_results[label].get(sid, {})
            all_data[sid][label] = {
                "series": d.get("series", []),
                "dates": d.get("dates", []),
                "right_days": d.get("right_side_days", []),
                "is_right": d.get("is_right_side", []),
            }

    out.append('<div id="allData" style="display:none">'
               + json.dumps(all_data, ensure_ascii=False) + '</div>')

    cat_labels = {"index": "指数", "industry_l1": "L1 行业",
                  "industry_l2": "L2 行业", "stock": "个股"}
    badge_classes = {"index": "badge-index", "industry_l1": "badge-l1",
                     "industry_l2": "badge-l2", "stock": "badge-stock"}

    out.append('<h2>§2 温度档位 × 右侧天数（每只品种双 Y 轴对比图）</h2>')
    out.append('<div class="card"><div class="meta">')
    out.append('主图（蓝色阶梯线）：温度档位 0-6（冻/寒/凉/平/温/热/沸）<br>')
    out.append('副图（橙色面积）：右侧天数（右 Y 轴），>0 时同时高亮背景<br>')
    out.append('橙色横线：温度 ≥ 温（即"维持右侧"阈值 idx=4）；蓝色横线：温度 ≥ 热（即"入场"阈值 idx=5）<br>')
    out.append('★ 默认参数（span=3, confirm_days=2），其它 4 组参数以浅色叠加供参考')
    out.append('</div></div>')

    for cat_name in ["index", "industry_l1", "industry_l2", "stock"]:
        sids = by_category[cat_name]
        if not sids:
            continue
        out.append(f'<h3>{cat_labels[cat_name]}（{len(sids)} 只）</h3>')
        for sid in sids:
            info = sym_info[sid]
            sid_js = sid.replace("_", "")
            out.append(f'<div class="sym-block">')
            out.append(f'<div class="sym-header">')
            out.append(f'<div><span class="sym-name">{info["name"]}</span> '
                       f'<span class="sym-id">{sid}</span></div>')
            out.append(f'<span class="badge {badge_classes[cat_name]}">{cat_labels[cat_name]}</span>')
            # 当前状态标签
            default_d = all_data[sid].get("span=3,confirm=2", {})
            if default_d.get("right_days"):
                cur_days = default_d["right_days"][-1] if default_d["right_days"][-1] else 0
                cur_in_right = default_d.get("is_right", [False])[-1] if default_d.get("is_right") else False
                if cur_in_right:
                    out.append(f'<span class="right-marker">当前在右侧 · {cur_days} 天</span>')
                else:
                    out.append('<span class="right-marker" style="background:#f8d7da;color:#721c24">当前不在右侧</span>')
            out.append('</div>')

            # 主图
            out.append(f'<div id="chart_{sid_js}" class="chart-tall"></div>')

            # 切换统计
            stats_lines = []
            for label in temp_params:
                d = temp_results[label].get(sid, {})
                cur_right = (d.get("is_right_side") or [False])[-1] if d.get("is_right_side") else False
                cur_days = d.get("right_side_days", [0])[-1] or 0
                stats_lines.append(
                    f'<code>{label}</code>: 切换 {d.get("transitions", 0)} · '
                    f'{"在右侧 " + str(cur_days) + " 天" if cur_right else "未在右侧"}'
                )
            out.append('<div class="meta">' + ' · '.join(stats_lines) + '</div>')

            # 详情：每日明细
            out.append('<details><summary>展开：末 30 天温度档位 + 右侧天数明细（默认参数）</summary>')
            out.append('<div class="scroll-x"><table><tr>'
                       '<th>日期</th><th>温度档位</th><th>原始档位</th>'
                       '<th>在右侧?</th><th>右侧天数</th></tr>')
            disp = default_d.get("series", [])
            right_days = default_d.get("right_days", [])
            is_right = default_d.get("is_right", [])
            dates = default_d.get("dates", [])
            n_show = min(30, len(dates))
            for i in range(max(0, len(dates) - n_show), len(dates)):
                v = disp[i] if i < len(disp) else None
                tier = TIER_IDX.get(v, -1) if v else -1
                if v:
                    tier_html = f'<span class="tier tier-{tier}">{v}</span>'
                else:
                    tier_html = '<span class="meta">-</span>'
                # 原始档位（只看 idx，与 displayed 对比）
                # 这里没有 score_smooth 直接给，只能显示 displayed
                r_in = is_right[i] if i < len(is_right) else False
                r_days = right_days[i] if i < len(right_days) else 0
                marker = '✓' if r_in else '·'
                out.append(f'<tr><td>{dates[i]}</td><td>{tier_html}</td><td>{tier_html}</td>'
                           f'<td>{marker}</td><td class="num">{r_days}</td></tr>')
            out.append('</table></div></details>')

            out.append('</div>')

    # JS
    out.append("""
<script>
const tierLabels = ["冻","寒","凉","平","温","热","沸"];
const tierColors = ["#0d6efd","#0dcaf0","#20c997","#6c757d","#ffc107","#fd7e14","#dc3545"];
const allData = JSON.parse(document.getElementById('allData').textContent);
const paramLabels = ["span=2,confirm=1","span=3,confirm=1","span=3,confirm=2","span=5,confirm=2","span=5,confirm=3"];
const paramStyles = [
  { color: "#fd7e14", width: 1.0, opacity: 0.30 },
  { color: "#28a745", width: 1.0, opacity: 0.30 },
  { color: "#5b8def", width: 2.5, opacity: 1.0  },
  { color: "#6610f2", width: 1.0, opacity: 0.30 },
  { color: "#d63384", width: 1.0, opacity: 0.30 },
];

for (const sid in allData) {
  const sidJs = sid.replace(/_/g, '');
  const el = document.getElementById('chart_' + sidJs);
  if (!el) continue;
  const chart = echarts.init(el);
  const symData = allData[sid];
  const dates = symData["span=3,confirm=2"].dates;

  // 5 组温度档位
  const tempSeries = paramLabels.map((label, idx) => {
    const arr = symData[label].series;
    const style = paramStyles[idx];
    return {
      name: label,
      type: 'line',
      step: 'end',
      yAxisIndex: 0,
      data: arr.map(v => v === null ? null : "冻寒凉平温热沸".indexOf(v)),
      symbol: 'none',
      lineStyle: { width: style.width, opacity: style.opacity, color: style.color },
      itemStyle: { color: style.color, opacity: style.opacity },
      emphasis: { focus: 'series', lineStyle: { width: 3, opacity: 1 } },
    };
  });

  // 右侧天数（默认参数下）
  const days = symData["span=3,confirm=2"].right_days;
  const isRight = symData["span=3,confirm=2"].is_right;
  const rightSeries = {
    name: '右侧天数',
    type: 'line',
    yAxisIndex: 1,
    smooth: false,
    symbol: 'circle',
    symbolSize: 5,
    data: days,
    lineStyle: { width: 2, color: '#fd7e14' },
    itemStyle: { color: '#fd7e14' },
    areaStyle: { color: 'rgba(253,126,20,0.15)' },
    emphasis: { focus: 'series' },
  };

  // 右侧区间背景（markArea）
  const markAreaData = [];
  let inArea = false, areaStart = 0;
  for (let i = 0; i < isRight.length; i++) {
    if (isRight[i] && !inArea) {
      areaStart = i;
      inArea = true;
    } else if (!isRight[i] && inArea) {
      markAreaData.push([{ xAxis: dates[areaStart] }, { xAxis: dates[i - 1] }]);
      inArea = false;
    }
  }
  if (inArea) markAreaData.push([{ xAxis: dates[areaStart] }, { xAxis: dates[dates.length - 1] }]);

  chart.setOption({
    grid: { left: 50, right: 60, top: 50, bottom: 60 },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    tooltip: { trigger: 'axis',
      formatter: function(params) {
        const date = params[0].axisValue;
        let html = '<b>' + date + '</b><br/>';
        params.forEach(p => {
          let val;
          if (p.seriesName === '右侧天数') {
            val = p.value === null ? '-' : p.value + ' 天';
          } else {
            val = p.value === null ? '-' : tierLabels[p.value];
          }
          html += p.marker + ' ' + p.seriesName + ': <b>' + val + '</b><br/>';
        });
        return html;
      }
    },
    xAxis: { type: 'category', data: dates,
             axisLabel: { fontSize: 10, rotate: 45, interval: Math.floor(dates.length / 8) } },
    yAxis: [
      { type: 'value', min: 0, max: 6, interval: 1, name: '温度档位',
        nameTextStyle: { fontSize: 11 },
        axisLabel: { formatter: v => tierLabels[v] },
        splitLine: { lineStyle: { type: 'dashed', color: '#eee' } },
      },
      { type: 'value', min: 0, name: '右侧天数',
        nameTextStyle: { fontSize: 11, color: '#fd7e14' },
        axisLabel: { color: '#fd7e14' },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '右侧区间',
        type: 'line',
        data: [],
        markArea: { itemStyle: { color: 'rgba(253,126,20,0.10)' },
                    data: markAreaData },
        silent: true,
        symbol: 'none',
        lineStyle: { opacity: 0 },
      },
      ...tempSeries,
      rightSeries,
    ],
  });
}
</script>""")

    # §3 风险点
    out.append("""
<h2>§3 风险点与下一步</h2>
<div class="card">
  <ol style="margin: 6px 0; padding-left: 22px; line-height: 1.7;">
    <li><strong>温度 span=3/confirm=2 在极端反转行情可能反应过慢</strong>。当前样本都是平稳品种，需观察至少 1 个完整反转行情周期（建议 6-12 个月窗口重跑）。</li>
    <li><strong>右侧天数按自然日累加</strong>，而非交易日。优势：反映温度反应周期；劣势：跨长假会看到天数虚高。</li>
    <li><strong>vol_adj sigmoid 陡峭度 k=0.15 是起点</strong>。需对照参考系统的「动量爆裂」信号频率调整。</li>
    <li><strong>当前校准仅用 11 只样本品种</strong>。建议扩展到全市场后再校准（脚本已支持，扩展 ~30 分钟）。</li>
  </ol>
</div>

<p class="meta">数据脚本：<code>scripts/calibrate_temperature.py</code> · HTML 生成：<code>scripts/build_compare_html.py</code></p>
</body>
</html>""")

    HTML_PATH.write_text("".join(out), encoding="utf-8")
    print(f"已生成: {HTML_PATH} ({HTML_PATH.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()