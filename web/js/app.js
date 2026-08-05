/* A-Share Trend Compass 一级页（指数 + L1 行业列表） */

let STATE = {
  tradeDate: null,
  topData: null,
  topChart: null,
  currentWindow: '1Y',
  currentScale: 'linear',
  groups: null,
  sort: { key: null, dir: 'desc' },
  query: '',
  watchOnly: false,
};

/** 拉顶部 A 股卡片数据。 */
async function loadTopCard() {
  try {
    const r = await fetch('data/top.json').then(r => r.ok ? r.json() : null);
    if (r) {
      STATE.topData = r;
      renderTopCard(r);
    }
  } catch (e) { console.warn('top.json 加载失败:', e); }
}

function renderTopCard(top) {
  document.getElementById('top-close').textContent = top.close == null ? '--' : (+top.close).toFixed(2);
  const pct = top.pct_chg;
  document.getElementById('top-pct').innerHTML = pct == null ? '--' : renderPct(pct);
  const tempEl = document.getElementById('top-temp');
  tempEl.innerHTML = renderTempBadge(top.temperature);
  const rsEl = document.getElementById('top-rs');
  rsEl.textContent = top.rs_score == null ? '--' : top.rs_score;
  const cum = top.cumulative && top.cumulative[STATE.currentWindow];
  document.getElementById('top-cum').textContent = cum == null ? '--' : (cum > 0 ? '+' : '') + cum.toFixed(2) + '%';
  document.getElementById('top-date').textContent = top.trade_date || '';

  // 画图
  const win = top.windows[STATE.currentWindow];
  if (win && win.dates && win.dates.length > 0) {
    drawTopChart(win);
  }
}

function drawTopChart(win) {
  // 复用图表实例，避免重复 echarts.init 泄漏
  if (!STATE.topChart) {
    STATE.topChart = echarts.init(document.getElementById('top-chart'));
  }
  const chart = STATE.topChart;
  const isLog = STATE.currentScale === 'log';
  const closes = win.closes || [];
  const cum = win.cum_pct || [];
  // 线性坐标 = 点数（原始收盘价）；对数坐标 = 百分比（累计涨幅比率取对数）
  const seriesData = isLog
    ? cum.map(v => (v == null || 1 + v / 100 <= 0) ? null : 1 + v / 100)
    : closes.map(c => (c == null || c <= 0) ? null : c);
  // 对数轴必须显式给 min/max：ECharts log 轴自动范围会扩到 10 的幂（1→10），
  // 数据在 0%~20% 区间时波形被压扁；范围在对数空间内留白（ChartUtils）。
  const logRange = isLog
    ? ChartUtils.computeLogRange(seriesData.filter(v => v != null))
    : null;
  chart.setOption({
    grid: { left: 50, right: 16, top: 24, bottom: 30 },
    tooltip: {
      trigger: 'axis',
      // 显示具体点位 + 累计涨幅
      formatter: (params) => {
        if (!params || params.length === 0) return '';
        const idx = params[0].dataIndex;
        let html = `${win.dates[idx]}`;
        if (idx < closes.length && closes[idx] != null) {
          html += `<br>点位 <b>${(+closes[idx]).toFixed(2)}</b>`;
        }
        const v = cum[idx];
        if (v != null) {
          html += `<br>累计 ${v >= 0 ? '+' : ''}${(+v).toFixed(2)}%`;
        }
        return html;
      },
    },
    dataZoom: [{ type: 'inside' }],
    xAxis: { type: 'category', data: win.dates, axisLabel: { fontSize: 10 } },
    yAxis: isLog
      ? { type: 'log', name: '累计%', min: logRange.min, max: logRange.max, axisLabel: { fontSize: 10, formatter: v => (((v - 1) * 100).toFixed(0)) + '%' } }
      : { type: 'value', name: '点数', scale: true, axisLabel: { fontSize: 10 } },
    series: [{ name: '累计', type: 'line', data: seriesData, smooth: true, showSymbol: false, lineStyle: { width: 1.5 }, areaStyle: { opacity: 0.15 } }],
  }, true);
}

/** 拉一级品种列表（指数 + L1 行业）。 */
async function loadL1List() {
  const wrap = document.getElementById('list-container');
  wrap.innerHTML = '<p>加载中...</p>';
  try {
    const r = await fetch('data/index-l1.json').then(r => r.ok ? r.json() : null);
    if (!r || !r.groups) {
      wrap.innerHTML = '<p>未找到 index-l1.json</p>';
      return;
    }
    document.getElementById('date-label').textContent = `截面日期：${r.trade_date || '?'}`;
    renderL1List(r.groups);
  } catch (e) {
    console.error('index-l1.json 加载失败:', e);
    wrap.innerHTML = '<p>加载失败</p>';
  }
}

function renderL1List(groups) {
  STATE.groups = groups;
  const wrap = document.getElementById('list-container');
  const watchSet = getWatchSet();
  const match = (g) => matchQuery(g, STATE.query) && (!STATE.watchOnly || watchSet.has(g.symbol_id));
  const sections = [
    { key: 'index', title: '宽基指数', data: groups.filter(g => g.node_type === 'index' && match(g)) },
    { key: 'industry_l1', title: '申万一级行业（点击进入二级页）', data: groups.filter(g => g.node_type === 'industry_l1' && match(g)) },
  ];
  let html = '';
  for (const sec of sections) {
    if (!sec.data.length) continue;
    html += `<h3 class="section-header">${sec.title}（${sec.data.length}）</h3>`;
    html += renderL1Table(sec.key, sortRows(sec.data, STATE.sort.key, STATE.sort.dir));
  }
  wrap.innerHTML = html || '<p class="empty">无匹配的品种</p>';
  bindTableSort(wrap, STATE.sort, () => renderL1List(STATE.groups));
}

function renderL1Table(sectionKey, rows) {
  const columns = [
    ['watch', '自选', null, (r) => renderWatchStar(r)],
    ['name', '品种', null, (r) => {
      // 指数和 L1 行业都可点；L1 跳 l1.html，指数跳 detail.html
      const link = r.node_type === 'industry_l1'
        ? `l1.html?l1=${encodeURIComponent(r.symbol_id)}`
        : `detail.html?symbol=${encodeURIComponent(r.symbol_id)}`;
      return `<a href="${link}">${r.name || '-'}</a><br><small>${r.symbol_id}</small>`;
    }],
    ['close', '最新价', null, (r) => r.close == null ? '-' : (+r.close).toFixed(2)],
    ['pct_chg', '日涨幅', null, (r) => renderPct(r.pct_chg)],
    ['temperature', '温度', null, (r) => renderTempBadge(r.temperature, r.temperature_change)],
    ['rs_score', 'RS', null, (r) => renderRs(r.rs_score, r.rs_score_trend)],
    ['right_side', '右侧', '天数/子品种', (r) => {
      // 品种自身在右侧：黄色 +N（交易日天数）
      if (r.is_right_side && r.right_side_days) {
        return `<span class="right-side-badge" title="自身处于右侧第 ${r.right_side_days} 个交易日">+${r.right_side_days}</span>`;
      }
      // 自身不在右侧：中性色显示子品种右侧占比，避免被误读为自身右侧天数
      if (r.right_side_count) {
        return `<span class="muted" title="子品种中处于右侧的数量">${r.right_side_count}/${r.children_count ?? '?'}</span>`;
      }
      return '<span class="muted">-</span>';
    }],
    ['children', '子品种', null, (r) => r.children_count == null ? '-' : r.children_count],
  ];
  return renderSymbolTable(rows, { columns, sortState: STATE.sort });
}

// 切窗口 / 切坐标
document.addEventListener('click', (e) => {
  const winBtn = e.target.closest('#window-tabs button');
  if (winBtn) {
    document.querySelectorAll('#window-tabs button').forEach(b => b.classList.remove('active'));
    winBtn.classList.add('active');
    STATE.currentWindow = winBtn.dataset.w;
    if (STATE.topData) renderTopCard(STATE.topData);
    return;
  }
  const scaleBtn = e.target.closest('#scale-tabs button');
  if (scaleBtn) {
    document.querySelectorAll('#scale-tabs button').forEach(b => b.classList.remove('active'));
    scaleBtn.classList.add('active');
    STATE.currentScale = scaleBtn.dataset.scale;
    if (STATE.topData && STATE.topData.windows) {
      const win = STATE.topData.windows[STATE.currentWindow];
      if (win) drawTopChart(win);
    }
    return;
  }
});

// ============ 全局搜索（跨指数 / L1 / L2 / 概念 / 个股） ============

let GLOBAL_INDEX = null;  // 缓存 symbols.json

async function ensureGlobalIndex() {
  if (GLOBAL_INDEX) return GLOBAL_INDEX;
  const r = await fetch('data/symbols.json');
  const d = await r.json();
  GLOBAL_INDEX = d.rows || [];
  return GLOBAL_INDEX;
}

function nodeLabel(nt) {
  return ({ index: '指数', industry_l1: '一级行业', industry_l2: '二级行业', concept: '概念', stock: '个股' })[nt] || nt;
}

function resultLink(nt, sid) {
  if (nt === 'industry_l1') return `l1.html?l1=${encodeURIComponent(sid)}`;
  if (nt === 'industry_l2') return `l2.html?l2=${encodeURIComponent(sid)}`;
  return `detail.html?symbol=${encodeURIComponent(sid)}`;
}

function runGlobalSearch(q) {
  const wrap = document.getElementById('global-search-results');
  if (!q || q.length < 1) { wrap.innerHTML = '<p class="muted">输入关键词开始搜索</p>'; return; }
  ensureGlobalIndex().then(rows => {
    const ql = q.toLowerCase();
    const hits = rows.filter(r =>
      (r.name && r.name.toLowerCase().includes(ql)) ||
      (r.symbol_id && r.symbol_id.toLowerCase().includes(ql))
    ).slice(0, 50);
    if (!hits.length) {
      wrap.innerHTML = '<p class="empty">无匹配品种</p>';
      return;
    }
    // 按 node_type 分组
    const groups = {};
    for (const h of hits) {
      (groups[h.node_type] = groups[h.node_type] || []).push(h);
    }
    let html = '';
    const ORDER = ['index', 'industry_l1', 'industry_l2', 'concept', 'stock'];
    for (const nt of ORDER) {
      if (!groups[nt]) continue;
      html += `<div class="g-section"><h4>${nodeLabel(nt)}（${groups[nt].length}）</h4>`;
      for (const r of groups[nt]) {
        const temp = renderTempBadge(r.temperature);
        const pct = renderPct(r.pct_chg);
        html += `<a class="g-item" href="${resultLink(r.node_type, r.symbol_id)}">
          <span class="g-name">${r.name || r.symbol_id}</span>
          <span class="g-id">${r.symbol_id}</span>
          <span class="g-temp">${temp}</span>
          <span class="g-pct">${pct}</span>
          <span class="g-rs">${r.rs_score ?? '--'}</span>
        </a>`;
      }
      html += '</div>';
    }
    wrap.innerHTML = html;
  });
}

function openSearchModal() {
  document.getElementById('search-modal').style.display = 'flex';
  document.getElementById('global-search-input').focus();
}
function closeSearchModal() {
  document.getElementById('search-modal').style.display = 'none';
}

window.addEventListener('load', () => {
  loadTopCard();
  loadL1List();
  const searchEl = document.getElementById('search-input');
  if (searchEl) {
    searchEl.addEventListener('input', () => {
      STATE.query = searchEl.value;
      if (STATE.groups) renderL1List(STATE.groups);
    });
  }
  const watchOnlyEl = document.getElementById('watch-only');
  if (watchOnlyEl) {
    watchOnlyEl.addEventListener('change', () => {
      STATE.watchOnly = watchOnlyEl.checked;
      if (STATE.groups) renderL1List(STATE.groups);
    });
  }
  document.addEventListener('watchlist-changed', () => {
    if (STATE.groups) renderL1List(STATE.groups);
  });
  // 全局搜索
  const gsBtn = document.getElementById('global-search-btn');
  if (gsBtn) gsBtn.addEventListener('click', openSearchModal);
  const gsClose = document.getElementById('global-search-close');
  if (gsClose) gsClose.addEventListener('click', closeSearchModal);
  const gsModal = document.getElementById('search-modal');
  if (gsModal) gsModal.addEventListener('click', (e) => {
    if (e.target === gsModal) closeSearchModal();
  });
  const gsInput = document.getElementById('global-search-input');
  if (gsInput) {
    let timer;
    gsInput.addEventListener('input', (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => runGlobalSearch(e.target.value.trim()), 150);
    });
    gsInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSearchModal();
    });
  }
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      openSearchModal();
    }
  });
});

window.addEventListener('resize', () => {
  if (STATE.topChart) STATE.topChart.resize();
});
