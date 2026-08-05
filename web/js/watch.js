/* A-Share Trend Compass 自选股页面 */

let WATCH_STATE = {
  watchlist: [],
  rowsById: {},
  query: '',
  tradeDate: null,
};

async function loadWatchDetail() {
  // 1) 加载当前日期 + 全品种截面（用于交叉查指标）
  const idxRes = await fetch('data/index-l1.json');
  const idxData = await idxRes.json();
  WATCH_STATE.tradeDate = idxData.trade_date;
  document.getElementById('date-label').textContent = `截面日期：${WATCH_STATE.tradeDate}`;

  // 2) 加载 symbols.json（含所有品种的温度/RS/右侧）
  // symbols.json 可能很大；只在全市场搜索时拉
  const symRes = await fetch('data/symbols.json');
  const symData = await symRes.json();
  const rows = symData.rows || [];
  WATCH_STATE.rowsById = {};
  for (const r of rows) WATCH_STATE.rowsById[r.symbol_id] = r;

  // 3) 读取自选
  WATCH_STATE.watchlist = getWatchlist();
  renderWatch();
}

function renderWatch() {
  const items = WATCH_STATE.watchlist.map(w => {
    const row = WATCH_STATE.rowsById[w.symbol_id] || { symbol_id: w.symbol_id, name: w.name };
    return { ...row, _is_watched: true };
  });
  // 过滤
  const filtered = items.filter(r => matchQuery(r, WATCH_STATE.query));
  // 排序：温度高的在前
  const sorted = sortRows(filtered, 'temperature', 'desc');

  // 汇总
  document.getElementById('total-count').textContent = items.length;
  const rightCount = items.filter(r => r.is_right_side).length;
  document.getElementById('right-count').textContent = rightCount;
  const rsVals = items.map(r => r.rs_score).filter(v => v != null && isFinite(v));
  const avgRs = rsVals.length ? Math.round(rsVals.reduce((a, b) => a + b, 0) / rsVals.length) : null;
  document.getElementById('avg-rs').textContent = avgRs ?? '--';

  // 温度分布图
  renderDistChart(items);

  // 表格
  const wrap = document.getElementById('watch-container');
  if (!items.length) {
    wrap.innerHTML = '<p class="empty">还没有自选品种。在任何页面点击 ★ 即可加入。</p>';
    return;
  }
  if (!filtered.length) {
    wrap.innerHTML = '<p class="empty">无匹配的自选品种</p>';
    return;
  }
  wrap.innerHTML = renderSymbolTable(sorted, { emptyText: '无自选' });

  // 绑定搜索
  document.getElementById('search-input').value = WATCH_STATE.query;
  document.getElementById('search-input').oninput = (e) => {
    WATCH_STATE.query = e.target.value;
    renderWatch();
  };
  document.getElementById('clear-btn').onclick = () => {
    if (confirm('确定清空所有自选？')) {
      localStorage.removeItem(WATCH_KEY);
      WATCH_STATE.watchlist = [];
      renderWatch();
    }
  };
}

function renderDistChart(items) {
  const chart = echarts.init(document.getElementById('dist-chart'));
  const counts = { '冻': 0, '寒': 0, '凉': 0, '平': 0, '温': 0, '热': 0, '沸': 0 };
  for (const r of items) if (r.temperature && counts[r.temperature] != null) counts[r.temperature]++;
  const labels = TEMP_LEVELS;
  const data = labels.map(t => counts[t] || 0);
  chart.setOption({
    grid: { left: 40, right: 20, top: 16, bottom: 30 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: labels },
    yAxis: { type: 'value', minInterval: 1, name: '数量' },
    series: [{
      name: '温度分布',
      type: 'bar',
      data: data.map((v, i) => ({ value: v, itemStyle: { color: TEMP_COLORS[labels[i]] } })),
      label: { show: true, position: 'top' },
    }],
  });
}

window.addEventListener('load', loadWatchDetail);
document.addEventListener('watchlist-changed', () => {
  WATCH_STATE.watchlist = getWatchlist();
  if (Object.keys(WATCH_STATE.rowsById).length) renderWatch();
});