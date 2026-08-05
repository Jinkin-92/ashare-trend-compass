/* A-Share Trend Compass 三级页（L2 行业下的个股列表） */

let L2_STATE = {
  l2Id: null,
  data: null,
  expandedSections: new Set(),
  sort: { key: 'temperature', dir: 'desc' },
  query: '',
  watchOnly: false,
};

const L2_PAGE_SIZE = 100;

function getL2IdFromUrl() {
  const params = new URLSearchParams(location.search);
  return params.get('l2');
}

async function loadL2Detail() {
  L2_STATE.l2Id = getL2IdFromUrl();
  if (!L2_STATE.l2Id) {
    document.getElementById('l2-name').textContent = '缺少 l2 参数';
    return;
  }
  try {
    const r = await fetch(`data/l2-${L2_STATE.l2Id}.json`).then(res => res.ok ? res.json() : null);
    if (!r) {
      document.getElementById('l2-name').textContent = `未找到 ${L2_STATE.l2Id}`;
      return;
    }
    L2_STATE.data = r;
    renderL2Header(r);
    renderTrendCharts(r);
    renderStocks(r.stocks || []);
  } catch (e) {
    console.error('l2 detail 加载失败:', e);
    document.getElementById('l2-name').textContent = '加载失败';
  }
}

// 复用 l1.js 中的趋势图渲染逻辑（同一函数体）
function renderTrendCharts(d) {
  const history = d.history || [];
  if (!history.length) return;
  const dates = history.map(h => h.date);
  const temps = history.map(h => h.temperature);
  const scores = history.map(h => h.temperature_score);
  const rs = history.map(h => h.rs_score);
  const rightDays = history.map(h => h.is_right_side ? (h.right_side_days || 0) : 0);

  const markAreaData = [];
  let runStart = 0, runTemp = temps[0];
  for (let k = 1; k <= temps.length; k++) {
    const t = k < temps.length ? temps[k] : null;
    if (t !== runTemp) {
      if (runTemp != null) {
        markAreaData.push([{ xAxis: dates[runStart] }, { xAxis: dates[k - 1] }]);
      }
      runStart = k; runTemp = t;
    }
  }

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
        fontSize: 10, hideOverlap: true,
        interval: idx => idx === 0 || idx === dates.length - 1
                       || idx === Math.floor(dates.length / 4)
                       || idx === Math.floor(dates.length / 2)
                       || idx === Math.floor(3 * dates.length / 4),
        formatter: (val) => val.slice(5),
        rotate: 30,
      },
    },
    yAxis: { type: 'value', min: 0, max: 6, interval: 1,
             axisLabel: { formatter: v => ['冻','寒','凉','平','温','热','沸'][v] } },
    series: [{
      name: '温度',
      type: 'line', step: 'end',
      data: temps.map(t => t == null ? null : ['冻','寒','凉','平','温','热','沸'].indexOf(t)),
      symbol: 'none',
      lineStyle: { color: '#5b8def', width: 2 },
      itemStyle: { color: '#5b8def' },
      markArea: { itemStyle: { color: 'rgba(91,141,239,0.10)' }, data: markAreaData, silent: true },
    }],
  });

  const rsChart = echarts.init(document.getElementById('chart-rs'));
  rsChart.setOption({
    grid: { left: 40, right: 16, top: 20, bottom: 50 },
    tooltip: { trigger: 'axis' },
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
    yAxis: { type: 'value', min: 0, max: 100, name: 'RS', nameTextStyle: { fontSize: 10 } },
    series: [{
      name: 'RS',
      type: 'line',
      data: rs.map((v, i) => ({
        value: v,
        itemStyle: {
          color: (i === 0 || v == null || rs[i - 1] == null) ? '#5b8def'
                : v > rs[i - 1] ? '#d93026' : (v < rs[i - 1] ? '#238b2c' : '#5b8def'),
        },
      })),
      symbol: 'circle', symbolSize: 4,
      smooth: false,
      lineStyle: { color: '#5b8def', width: 1.5 },
      itemStyle: { color: '#5b8def' },
      areaStyle: { color: 'rgba(91,141,239,0.10)' },
    }],
  });

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
    grid: { left: 40, right: 16, top: 20, bottom: 50 },
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
        name: '右侧区间', type: 'line', data: [], symbol: 'none', lineStyle: { opacity: 0 },
        markArea: { itemStyle: { color: 'rgba(253,126,20,0.10)' }, data: rightArea, silent: true },
      },
      {
        name: '右侧天数', type: 'line', data: rightDays, symbol: 'none',
        lineStyle: { color: '#fd7e14', width: 2 }, itemStyle: { color: '#fd7e14' },
        areaStyle: { color: 'rgba(253,126,20,0.20)' },
      },
    ],
  });
}

function renderL2Header(d) {
  document.getElementById('l2-name').textContent = `${d.name || L2_STATE.l2Id}`;
  const tempHtml = renderTempBadge(d.temperature, d.temperature_change);
  const pct = d.pct_chg;
  const pctHtml = pct == null ? '--' : renderPct(pct);
  const rsHtml = d.rs_score == null ? '--' : renderRs(d.rs_score, d.rs_score_trend);
  document.getElementById('l2-meta').innerHTML =
    `${tempHtml} &nbsp; 日涨幅 ${pctHtml} &nbsp; RS ${rsHtml} &nbsp; 成分股 ${d.children_count ?? '--'} &nbsp; 右侧 ${d.right_side_count ?? 0}/${d.children_count ?? '--'} &nbsp; <small>${d.symbol_id}</small>`;

  // 面包屑：一级行业跳转链接
  if (d.parent_id) {
    const a = document.getElementById('back-to-l1');
    a.textContent = d.parent_name || d.parent_id;
    a.href = `l1.html?l1=${encodeURIComponent(d.parent_id)}`;
  }
}

function renderStocks(items) {
  const watchSet = getWatchSet();
  const filtered = items.filter(r => matchQuery(r, L2_STATE.query) && (!L2_STATE.watchOnly || watchSet.has(r.symbol_id)));
  document.getElementById('stock-total').textContent = filtered.length;
  const wrap = document.getElementById('stock-container');
  if (items.length === 0) {
    // L2 下没有挂个股（stock.parent_id 是 L1，不是 L2）。
    // 给用户友好提示 + 跳到 L1 看该 L1 下所有 stock。
    const l1Id = L2_STATE.data && L2_STATE.data.parent_id;
    const l1Name = L2_STATE.data && L2_STATE.data.parent_name;
    wrap.innerHTML = `
      <div class="empty-state">
        <p>该二级行业下未挂载个股。</p>
        <p class="muted">由于 stock.parent_id 指向一级行业，"二级行业 → 成分股"映射未维护。</p>
        ${l1Id ? `<p>→ <a href="l1.html?l1=${encodeURIComponent(l1Id)}">查看 <b>${l1Name || l1Id}</b> 下所有个股</a></p>` : ''}
      </div>
    `;
    return;
  }
  if (filtered.length === 0) {
    wrap.innerHTML = '<p class="empty">无匹配的个股</p>';
    return;
  }
  let sorted;
  if (L2_STATE.sort.key) {
    sorted = sortRows(filtered, L2_STATE.sort.key, L2_STATE.sort.dir);
  } else {
    sorted = [...filtered].sort((a, b) => {
      const sa = a.temperature_score ?? -1;
      const sb = b.temperature_score ?? -1;
      if (sb !== sa) return sb - sa;
      return (b.right_side_days || 0) - (a.right_side_days || 0);
    });
  }
  wrap.innerHTML = renderCollapsibleTable(sorted, 'stocks', L2_PAGE_SIZE, L2_STATE.expandedSections, L2_STATE.sort);
  bindTableSort(wrap, L2_STATE.sort, () => renderStocks(L2_STATE.data ? L2_STATE.data.stocks || [] : []));
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.section-toggle');
  if (!btn) return;
  if (btn.dataset.action === 'expand') L2_STATE.expandedSections.add(btn.dataset.section);
  else L2_STATE.expandedSections.delete(btn.dataset.section);
  if (L2_STATE.data) renderStocks(L2_STATE.data.stocks || []);
});

window.addEventListener('load', () => {
  loadL2Detail();
  const searchEl = document.getElementById('search-input');
  if (searchEl) {
    searchEl.addEventListener('input', () => {
      L2_STATE.query = searchEl.value;
      if (L2_STATE.data) renderStocks(L2_STATE.data.stocks || []);
    });
  }
  const watchOnlyEl = document.getElementById('watch-only');
  if (watchOnlyEl) {
    watchOnlyEl.addEventListener('change', () => {
      L2_STATE.watchOnly = watchOnlyEl.checked;
      if (L2_STATE.data) renderStocks(L2_STATE.data.stocks || []);
    });
  }
  document.addEventListener('watchlist-changed', () => {
    if (L2_STATE.data) renderStocks(L2_STATE.data.stocks || []);
  });
});
