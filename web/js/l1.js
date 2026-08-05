/* A-Share Trend Compass 二级页（L1 行业详情） */

let L1_STATE = {
  l1Id: null,
  data: null,
  expandedSections: new Set(),
  sort: { key: null, dir: 'desc' },
  query: '',
  watchOnly: false,
};

const L1_PAGE_SIZE = 50;

function getL1IdFromUrl() {
  const params = new URLSearchParams(location.search);
  return params.get('l1');
}

async function loadL1Detail() {
  L1_STATE.l1Id = getL1IdFromUrl();
  if (!L1_STATE.l1Id) {
    document.getElementById('l1-name').textContent = '缺少 l1 参数';
    return;
  }

  try {
    const r = await fetch(`data/l1-${L1_STATE.l1Id}.json`).then(r => r.ok ? r.json() : null);
    if (!r) {
      document.getElementById('l1-name').textContent = `未找到 ${L1_STATE.l1Id}`;
      return;
    }
    L1_STATE.data = r;
    renderL1Header(r);
    renderTrendCharts(r);
    renderL2Section(r.l2_children || []);
  } catch (e) {
    console.error('l1 detail 加载失败:', e);
    document.getElementById('l1-name').textContent = '加载失败';
  }
}

const TREND_TIER_COLORS = {
  '冻': '#0d6efd', '寒': '#0dcaf0', '凉': '#20c997', '平': '#6c757d',
  '温': '#ffc107', '热': '#fd7e14', '沸': '#dc3545',
};
const TREND_TIER_LIST = ['冻','寒','凉','平','温','热','沸'];

function renderTrendCharts(d) {
  const history = d.history || [];
  if (!history.length) return;
  const dates = history.map(h => h.date);
  const temps = history.map(h => h.temperature);
  const scores = history.map(h => h.temperature_score);
  const rs = history.map(h => h.rs_score);
  const rightDays = history.map(h => h.is_right_side ? (h.right_side_days || 0) : 0);

  // markArea: 连续相同温度合并
  const markAreaData = [];
  let runStart = 0, runTemp = temps[0];
  for (let k = 1; k <= temps.length; k++) {
    const t = k < temps.length ? temps[k] : null;
    if (t !== runTemp) {
      if (runTemp != null) {
        markAreaData.push([{ xAxis: dates[runStart] }, { xAxis: dates[k - 1] }]);
      }
      runStart = k;
      runTemp = t;
    }
  }

  // 1. 温度档位
  const tempChart = echarts.init(document.getElementById('chart-temp'));
  tempChart.setOption({
    grid: { left: 40, right: 16, top: 20, bottom: 50 },
    tooltip: { trigger: 'axis',
      formatter: (params) => {
        const i = dates.indexOf(params[0].axisValue);
        return dates[i]
          + `<br/>温度: <b>${temps[i] || '--'}</b>`
          + (scores[i] != null ? `（${(+scores[i]).toFixed(1)} 分）` : '')
          + (rs[i] != null ? `<br/>RS: ${rs[i]}` : '')
          + (history[i].is_right_side ? `<br/>右侧: +${history[i].right_side_days || 0} 天` : '');
      },
    },
    xAxis: {
      type: 'category', data: dates,
      axisLabel: {
        fontSize: 10,
        hideOverlap: true,
        // 60 天最多显示约 8 个日期，避免挤在一起
        interval: idx => idx === 0 || idx === dates.length - 1
                       || idx === Math.floor(dates.length / 4)
                       || idx === Math.floor(dates.length / 2)
                       || idx === Math.floor(3 * dates.length / 4),
        formatter: (val) => val.slice(5),  // 只显示 MM-DD
        rotate: 30,
      },
    },
    yAxis: { type: 'value', min: 0, max: 6, interval: 1,
             axisLabel: { formatter: v => TREND_TIER_LIST[v] } },
    series: [{
      name: '温度',
      type: 'line', step: 'end',
      data: temps.map(t => t == null ? null : TREND_TIER_LIST.indexOf(t)),
      symbol: 'none',
      lineStyle: { color: '#5b8def', width: 2 },
      itemStyle: { color: '#5b8def' },
      markArea: { itemStyle: { color: 'rgba(91,141,239,0.10)' }, data: markAreaData, silent: true },
    }],
  });

  // 2. RS 排名（A 股配色：涨红跌绿）
  const rsChart = echarts.init(document.getElementById('chart-rs'));
  // 计算每个点的涨跌颜色（基于 daily_price 的 pct_chg 字段，这里没有）→ 改为基于与前一日 RS 变化
  const rsColors = rs.map((v, i) => {
    if (i === 0 || v == null || rs[i - 1] == null) return '#5b8def';
    return v > rs[i - 1] ? '#d93026' : (v < rs[i - 1] ? '#238b2c' : '#5b8def');
  });
  const rsAreaColors = rs.map((v, i) => {
    if (i === 0 || v == null || rs[i - 1] == null) return 'rgba(91,141,239,0.10)';
    return v > rs[i - 1] ? 'rgba(217,48,38,0.10)' : (v < rs[i - 1] ? 'rgba(35,139,44,0.10)' : 'rgba(91,141,239,0.10)');
  });
  rsChart.setOption({
    grid: { left: 40, right: 16, top: 20, bottom: 50 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category', data: dates,
      axisLabel: {
        fontSize: 10,
        hideOverlap: true,
        interval: idx => idx === 0 || idx === dates.length - 1
                       || idx === Math.floor(dates.length / 4)
                       || idx === Math.floor(dates.length / 2)
                       || idx === Math.floor(3 * dates.length / 4),
        formatter: (val) => val.slice(5),
        rotate: 30,
      },
    },
    yAxis: { type: 'value', min: 0, max: 100, name: 'RS', nameTextStyle: { fontSize: 10 } },
    series: [{
      name: 'RS',
      type: 'line',
      data: rs.map((v, i) => ({ value: v, itemStyle: { color: rsColors[i] } })),
      symbol: 'circle',
      symbolSize: 4,
      smooth: false,
      lineStyle: { color: '#5b8def', width: 1.5 },
      itemStyle: { color: '#5b8def' },
      areaStyle: { color: 'rgba(91,141,239,0.10)' },
    }],
  });

  // 3. 右侧天数（按段着色 + 右侧区间背景高亮）
  const rightChart = echarts.init(document.getElementById('chart-right'));
  const rightArea = [];
  let inRight = false, areaStart = 0;
  for (let k = 0; k < history.length; k++) {
    const ir = history[k].is_right_side;
    if (ir && !inRight) { areaStart = k; inRight = true; }
    else if (!ir && inRight) {
      rightArea.push([{ xAxis: dates[areaStart] }, { xAxis: dates[k - 1] }]);
      inRight = false;
    }
  }
  if (inRight) rightArea.push([{ xAxis: dates[areaStart] }, { xAxis: dates[dates.length - 1] }]);
  rightChart.setOption({
    grid: { left: 36, right: 12, top: 16, bottom: 30 },
    tooltip: { trigger: 'axis',
      formatter: (params) => {
        const i = dates.indexOf(params[0].axisValue);
        const ir = history[i].is_right_side;
        return dates[i] + (ir ? `<br/>右侧: +${history[i].right_side_days || 0} 天` : '<br/>未在右侧');
      },
    },
    xAxis: {
      type: 'category', data: dates,
      axisLabel: {
        fontSize: 10, hideOverlap: true,
        interval: idx => idx === 0 || idx === dates.length - 1
                       || idx === Math.floor(dates.length / 4)
                       || idx === Math.floor(dates.length / 2)
                       || idx === Math.floor(3 * dates.length / 4),
        formatter: (val) => val.slice(5),
        rotate: 30,
      },
    },
    yAxis: { type: 'value', min: 0, name: '天数', nameTextStyle: { fontSize: 10 } },
    series: [
      {
        name: '右侧区间',
        type: 'line', data: [], symbol: 'none', lineStyle: { opacity: 0 },
        markArea: { itemStyle: { color: 'rgba(253,126,20,0.10)' }, data: rightArea, silent: true },
      },
      {
        name: '右侧天数',
        type: 'line',
        data: rightDays,
        symbol: 'none',
        lineStyle: { color: '#fd7e14', width: 2 },
        itemStyle: { color: '#fd7e14' },
        areaStyle: { color: 'rgba(253,126,20,0.20)' },
      },
    ],
  });
}

function renderL1Header(d) {
  document.getElementById('l1-name').textContent = d.name || L1_STATE.l1Id;
  const tempHtml = renderTempBadge(d.temperature, d.temperature_change);
  const pct = d.pct_chg;
  const pctHtml = pct == null ? '--' : renderPct(pct);
  const rsHtml = d.rs_score == null ? '--' : renderRs(d.rs_score, d.rs_score_trend);
  document.getElementById('l1-meta').innerHTML =
    `${tempHtml} &nbsp; 日涨幅 ${pctHtml} &nbsp; RS ${rsHtml} &nbsp; 二级行业 ${d.children_count ?? '--'} &nbsp; 右侧 ${d.right_side_count ?? 0}/${d.children_count ?? '--'} &nbsp; <small>${d.symbol_id}</small>`;
}

function renderL2Section(items) {
  const wrap = document.getElementById('l2-container');
  if (!items.length) {
    wrap.innerHTML = '<p class="muted">该一级行业下无二级行业。</p>';
    return;
  }
  const watchSet = getWatchSet();
  const filtered = items.filter(r => matchQuery(r, L1_STATE.query) && (!L1_STATE.watchOnly || watchSet.has(r.symbol_id)));
  if (!filtered.length) {
    wrap.innerHTML = '<p class="empty">无匹配的二级行业</p>';
    return;
  }
  const sorted = sortRows(filtered, L1_STATE.sort.key, L1_STATE.sort.dir);
  // L2 列表不分页：最多 130 个
  const columns = [
    ['watch', '自选', null, (r) => renderWatchStar(r)],
    ['name', '二级行业', null, (r) => `<a href="l2.html?l2=${encodeURIComponent(r.symbol_id)}">${r.name || '-'}</a><br><small>${r.symbol_id}</small>`],
    ['close', '最新价', null, (r) => r.close == null ? '-' : (+r.close).toFixed(2)],
    ['pct_chg', '日涨幅', null, (r) => renderPct(r.pct_chg)],
    ['temperature', '温度', null, (r) => renderTempBadge(r.temperature, r.temperature_change)],
    ['rs_score', 'RS', null, (r) => renderRs(r.rs_score, r.rs_score_trend)],
    ['right_side', '右侧', '品种数', (r) => r.is_right_side ? `<span class="right-side-badge">+${r.right_side_days || 0}</span>` : '<span class="muted">-</span>'],
    ['stocks', '成分股', null, (r) => r.children_count == null ? '-' : r.children_count],
  ];
  wrap.innerHTML = renderSymbolTable(sorted, { columns, emptyText: '无二级行业', sortState: L1_STATE.sort });
  bindTableSort(wrap, L1_STATE.sort, () => renderL2Section(L1_STATE.data ? L1_STATE.data.l2_children || [] : []));
}

window.addEventListener('load', () => {
  loadL1Detail();
  const searchEl = document.getElementById('search-input');
  if (searchEl) {
    searchEl.addEventListener('input', () => {
      L1_STATE.query = searchEl.value;
      if (L1_STATE.data) renderL2Section(L1_STATE.data.l2_children || []);
    });
  }
  const watchOnlyEl = document.getElementById('watch-only');
  if (watchOnlyEl) {
    watchOnlyEl.addEventListener('change', () => {
      L1_STATE.watchOnly = watchOnlyEl.checked;
      if (L1_STATE.data) renderL2Section(L1_STATE.data.l2_children || []);
    });
  }
  document.addEventListener('watchlist-changed', () => {
    if (L1_STATE.data) renderL2Section(L1_STATE.data.l2_children || []);
  });
});
