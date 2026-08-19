/* 热点看板：温→热 / 热·沸（L2 + 个股，四组） */

const STATE = {
  data: null,
  // 默认折叠条数；个股 186 条太长，只展开前 30
  expandedSet: new Set(),
  pageSize: 30,
};

/* ============ 入口 ============ */
window.addEventListener('load', () => {
  loadBoard();
  bindGlobalSearch();
});

async function loadBoard() {
  try {
    const r = await fetch('data/hotlist.json').then(r => r.ok ? r.json() : null);
    if (!r) throw new Error('hotlist.json 拉取失败');
    STATE.data = r;
    renderAll(r);
  } catch (e) {
    console.error(e);
    document.getElementById('date-label').textContent = '加载失败';
  }
}

/* ============ 顶部摘要 ============ */
function renderAll(d) {
  // 日期标签
  const l2d = d.l2_effective_date || '--';
  const sd = d.stock_effective_date || '--';
  document.getElementById('date-label').textContent = `截面日期：${d.trade_date}　|　L2 行业数据截至 ${l2d}，个股数据截至 ${sd}`;

  // 摘要数字
  document.getElementById('s-l2w').textContent = d.l2_warm_to_hot.length;
  document.getElementById('s-l2h').textContent = d.l2_hot_or_boil.length;
  document.getElementById('s-sw').textContent = d.stock_warm_to_hot.length;
  document.getElementById('s-sh').textContent = d.stock_hot_or_boil.length;

  // 详细分组
  renderSection({
    bodyId: 'body-l2-warm', metaId: 'meta-l2-warm',
    rows: d.l2_warm_to_hot, nodeType: 'industry_l2',
    sectionKey: 'l2-warm',
    effectiveDate: l2d,
    isBreakout: true,
  });
  renderSection({
    bodyId: 'body-l2-hot', metaId: 'meta-l2-hot',
    rows: d.l2_hot_or_boil, nodeType: 'industry_l2',
    sectionKey: 'l2-hot',
    effectiveDate: l2d,
    isBreakout: false,
  });
  renderSection({
    bodyId: 'body-stock-warm', metaId: 'meta-stock-warm',
    rows: d.stock_warm_to_hot, nodeType: 'stock',
    sectionKey: 'stock-warm',
    effectiveDate: sd,
    isBreakout: true,
  });
  renderSection({
    bodyId: 'body-stock-hot', metaId: 'meta-stock-hot',
    rows: d.stock_hot_or_boil, nodeType: 'stock',
    sectionKey: 'stock-hot',
    effectiveDate: sd,
    isBreakout: false,
  });
}

/* ============ 单个分组渲染 ============ */
function renderSection({ bodyId, metaId, rows, sectionKey, effectiveDate, isBreakout }) {
  const metaEl = document.getElementById(metaId);
  const bodyEl = document.getElementById(bodyId);

  if (!rows || rows.length === 0) {
    metaEl.textContent = `共 0 条　·　${effectiveDate}`;
    bodyEl.innerHTML = '<p class="row-empty">暂无符合条件的品种</p>';
    return;
  }

  metaEl.textContent = `共 ${rows.length} 条　·　数据截至 ${effectiveDate}`;

  // 个股列表太长时折叠；L2 全部展开
  const needCollapse = rows.length > STATE.pageSize && !sectionKey.startsWith('l2-');
  const tableWrap = document.createElement('div');
  bodyEl.innerHTML = '';
  bodyEl.appendChild(tableWrap);

  let footerEl = null, toggleBtn = null;
  if (needCollapse) {
    footerEl = document.createElement('div');
    footerEl.className = 'section-footer';
    toggleBtn = document.createElement('button');
    toggleBtn.className = 'section-toggle';
    footerEl.appendChild(toggleBtn);
    bodyEl.appendChild(footerEl);
  }

  const refresh = () => {
    const isExpanded = STATE.expandedSet.has(sectionKey);
    const visibleNow = (needCollapse && !isExpanded) ? rows.slice(0, STATE.pageSize) : rows;
    tableWrap.innerHTML = renderHotTable(visibleNow, { isBreakout });
    if (toggleBtn) {
      const hiddenNow = rows.length - visibleNow.length;
      toggleBtn.textContent = isExpanded ? '收起' : `展开剩余 ${hiddenNow} 条`;
      toggleBtn.onclick = () => {
        if (STATE.expandedSet.has(sectionKey)) STATE.expandedSet.delete(sectionKey);
        else STATE.expandedSet.add(sectionKey);
        refresh();
      };
    }
  };
  refresh();
}

/* ============ 表格 ============ */
function renderHotTable(rows, { isBreakout }) {
  const columns = [
    ['watch', '自选', null, (r) => renderWatchStar(r)],
    ['name', '品种', null, (r) => {
      const link = r.node_type === 'industry_l2'
        ? `l2.html?l2=${encodeURIComponent(r.symbol_id)}`
        : `detail.html?symbol=${encodeURIComponent(r.symbol_id)}`;
      return `<a href="${link}">${r.name || '-'}</a><br><small>${r.symbol_id}</small>`;
    }],
    ['parent', '所属', null, (r) => r.parent_name
      ? `<small>${r.parent_name}${r.parent_id ? ' <small>(' + r.parent_id + ')</small>' : ''}</small>`
      : '<span class="muted">-</span>'],
    ['temperature', '今日', null, (r) => renderTempBadge(r.temperature)],
    ['prev', '上一日', null, (r) => {
      const prev = r.prev_temperature;
      if (!prev) return '<span class="muted">-</span>';
      // 温→热 这条高亮副标签
      if (isBreakout && prev === '温') {
        return `<span class="temp-badge" style="color:${TEMP_COLORS['温']};font-weight:bold;">温</span> <span class="badge-pill break" style="font-size:0.65rem;">→热</span>`;
      }
      return `<span class="temp-badge" style="color:${TEMP_COLORS[prev] || '#000'};">${prev}</span>`;
    }],
    ['close', '最新价', null, (r) => r.close == null ? '-' : (+r.close).toFixed(2)],
    ['pct_chg', '日涨幅', null, (r) => renderPct(r.pct_chg)],
    ['rs_score', 'RS', null, (r) => renderRs(r.rs_score, r.rs_score_trend)],
    ['right_side', '右侧', '天数', (r) => {
      if (r.is_right_side && r.right_side_days) {
        return `<span class="right-side-badge">+${r.right_side_days}</span>`;
      }
      return '<span class="muted">-</span>';
    }],
  ];

  if (!rows || rows.length === 0) {
    return '<p class="empty">无数据</p>';
  }

  const head = columns.map(([k, label, unit]) => {
    const unitHtml = unit ? `<small>${unit}</small>` : '';
    return `<th data-key="${k}">${label}${unitHtml}</th>`;
  }).join('');

  const body = rows.map(r =>
    `<tr class="${r.is_right_side ? 'right-side ' : ''}${isBreakout ? 'break-row' : ''}">${
      columns.map(([k, , , fn]) => `<td>${fn ? fn(r) : (r[k] ?? '-')}</td>`).join('')
    }</tr>`
  ).join('');

  return `<table class="symbol-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/* ============ 全局搜索（与 index.html 同步实现） ============ */
let GLOBAL_INDEX = null;
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
    const groups = {};
    for (const h of hits) (groups[h.node_type] = groups[h.node_type] || []).push(h);
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
function openSearchModal() { document.getElementById('search-modal').style.display = 'flex'; document.getElementById('global-search-input').focus(); }
function closeSearchModal() { document.getElementById('search-modal').style.display = 'none'; }

function bindGlobalSearch() {
  const btn = document.getElementById('global-search-btn');
  if (btn) btn.addEventListener('click', openSearchModal);
  const close = document.getElementById('global-search-close');
  if (close) close.addEventListener('click', closeSearchModal);
  const modal = document.getElementById('search-modal');
  if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) closeSearchModal(); });
  const input = document.getElementById('global-search-input');
  if (input) {
    let timer;
    input.addEventListener('input', (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => runGlobalSearch(e.target.value.trim()), 150);
    });
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSearchModal(); });
  }
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault(); openSearchModal();
    }
  });
}