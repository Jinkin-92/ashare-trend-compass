/* A-Share Trend Compass 列表页共享工具 */

const TEMP_COLORS = {
  '沸': '#8b0000',
  '热': '#d93026',
  '温': '#ff8c00',
  '平': '#808080',
  '凉': '#87ceeb',
  '寒': '#1e90ff',
  '冻': '#00008b',
};

const TEMP_LEVELS = ['沸', '热', '温', '平', '凉', '寒', '冻'];

const RS_TREND_LABEL = { '↑': '↑', '↓': '↓', '↓↓': '↓↓', 'flat': '' };

/** 把温度档渲染为带颜色的色块 HTML。 */
function renderTempBadge(temp, change) {
  if (!temp) return '<span class="temp-empty">-</span>';
  const color = TEMP_COLORS[temp] || '#000';
  const chg = change ? `<small class="temp-change">${change}</small>` : '';
  return `<span class="temp-badge" style="color:${color};font-weight:bold;">${temp}</span>${chg}`;
}

/** 涨幅渲染：涨红跌绿（A 股惯例）。 */
function renderPct(pct) {
  if (pct == null) return '<span class="pct-empty">-</span>';
  const cls = pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat';
  const sign = pct > 0 ? '+' : '';
  return `<span class="pct ${cls}">${sign}${pct.toFixed(2)}%</span>`;
}

/** 渲染"是否右侧"。 */
function renderRightSide(flag, days) {
  if (flag) return `<span class="right-side-badge">+${days || 0}</span>`;
  return '<span class="muted">-</span>';
}

/** 渲染相对强度（数值 + 趋势箭头）。 */
function renderRs(rs, trend) {
  if (rs == null) return '<span class="muted">-</span>';
  const arrow = RS_TREND_LABEL[trend] || '';
  return `${rs}${arrow ? ' ' + arrow : ''}`;
}

/** 通用品种表格（行数组 + 列定义）。 */
function renderSymbolTable(rows, opts = {}) {
  const {
    columns = [
      ['watch', '自选', null, (r) => renderWatchStar(r)],
      ['name', '品种', null, (r) => `<a href="detail.html?symbol=${encodeURIComponent(r.symbol_id)}">${r.name || '-'}</a><br><small>${r.symbol_id}</small>`],
      ['close', '最新价', 'CNY', (r) => r.close == null ? '-' : (+r.close).toFixed(2)],
      ['pct_chg', '日涨幅', '%', (r) => renderPct(r.pct_chg)],
      ['temperature', '温度', null, (r) => renderTempBadge(r.temperature, r.temperature_change)],
      ['right_side', '右侧天数', '交易日', (r) => renderRightSide(r.is_right_side, r.right_side_days)],
      ['rs_score', 'RS', '1-99', (r) => renderRs(r.rs_score, r.rs_score_trend)],
    ],
    emptyText = '无数据',
    sortState = null,
  } = opts;

  if (!rows || rows.length === 0) {
    return `<p class="empty">${emptyText}</p>`;
  }

  const head = columns.map(([k, label, unit]) => {
    const unitHtml = unit ? `<small>${unit}</small>` : '';
    let arrow = '';
    if (sortState && sortState.key === k) arrow = sortState.dir === 'asc' ? ' ▲' : ' ▼';
    return `<th data-key="${k}">${label}${unitHtml}${arrow}</th>`;
  }).join('');

  const body = rows.map(r =>
    `<tr class="${r.is_right_side ? 'right-side' : ''}">${
      columns.map(([k, , , fn]) => `<td>${fn ? fn(r) : (r[k] ?? '-')}</td>`).join('')
    }</tr>`
  ).join('');

  return `<table class="symbol-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/** 温度档到排序值的映射（沸最高）。 */
const TEMP_SORT_VALUE = { '沸': 6, '热': 5, '温': 4, '平': 3, '凉': 2, '寒': 1, '冻': 0 };

/** 提取行的排序键值（数值优先，缺失排最后）。 */
function sortKeyOf(row, key) {
  switch (key) {
    case 'watch':
      return isWatched(row.symbol_id) ? 1 : 0;
    case 'name':
      return row.name || row.symbol_id || '';
    case 'temperature':
      return row.temperature_score ?? TEMP_SORT_VALUE[row.temperature] ?? -Infinity;
    case 'right_side':
      // 在右侧的排前面，再按天数；L1/L2 行用 right_side_count
      return (row.is_right_side ? 1e6 : 0) + (row.right_side_days || row.right_side_count || 0);
    case 'children':
    case 'stocks':
      return row.children_count ?? -Infinity;
    default:
      return row[key] ?? -Infinity;
  }
}

/** 按 key + 方向排序（不改动原数组）。dir: 'desc' | 'asc'。 */
function sortRows(rows, key, dir) {
  if (!key) return rows;
  const m = dir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = sortKeyOf(a, key);
    const vb = sortKeyOf(b, key);
    if (typeof va === 'string' || typeof vb === 'string') {
      return m * String(va).localeCompare(String(vb), 'zh');
    }
    return m * (va - vb);
  });
}

/** 给容器内的表头绑定点击排序。state={key,dir} 会被就地修改，onSort 触发重渲染。 */
function bindTableSort(containerEl, state, onSort) {
  containerEl.querySelectorAll('th[data-key]').forEach(th => {
    th.onclick = () => {
      const key = th.dataset.key;
      if (state.key === key) {
        state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.key = key;
        state.dir = 'desc';
      }
      onSort();
    };
  });
}

/** 名称 / 代码 模糊匹配（大小写不敏感）。 */
function matchQuery(row, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return true;
  return (row.name || '').toLowerCase().includes(q) || (row.symbol_id || '').toLowerCase().includes(q);
}

/* ---------- 自选股（localStorage 持久化） ---------- */

const WATCH_KEY = 'trend-compass-watchlist';
let _watchSet = null;

function getWatchlist() {
  try {
    return JSON.parse(localStorage.getItem(WATCH_KEY) || '[]');
  } catch (e) {
    return [];
  }
}

/** 缓存的 symbol_id 集合，渲染期高频查询用。 */
function getWatchSet() {
  if (!_watchSet) _watchSet = new Set(getWatchlist().map(w => w.symbol_id));
  return _watchSet;
}

function isWatched(symbolId) {
  return getWatchSet().has(symbolId);
}

/** 切换自选状态，返回切换后是否在自选。 */
function toggleWatch(symbolId, name) {
  const list = getWatchlist();
  const i = list.findIndex(w => w.symbol_id === symbolId);
  if (i >= 0) list.splice(i, 1);
  else list.push({ symbol_id: symbolId, name: name || symbol_id });
  localStorage.setItem(WATCH_KEY, JSON.stringify(list));
  _watchSet = new Set(list.map(w => w.symbol_id));
  return i < 0;
}

/** 星标单元格。 */
function renderWatchStar(r) {
  const on = isWatched(r.symbol_id);
  return `<span class="watch-star ${on ? 'on' : ''}" data-symbol="${r.symbol_id}" data-name="${r.name || r.symbol_id}" title="加入/移出自选">${on ? '★' : '☆'}</span>`;
}

// 星标点击：切换 + 广播，各页监听 watchlist-changed 重渲染
document.addEventListener('click', (e) => {
  const star = e.target.closest('.watch-star');
  if (!star) return;
  e.preventDefault();
  e.stopPropagation();
  toggleWatch(star.dataset.symbol, star.dataset.name);
  document.dispatchEvent(new CustomEvent('watchlist-changed'));
});

/** 折叠按钮：默认显示 N 条 + 展开剩余。 */
function renderCollapsibleTable(rows, sectionKey, pageSize = 50, expandedSet = null, sortState = null) {
  if (!rows || rows.length === 0) return '';
  const isExpanded = expandedSet ? expandedSet.has(sectionKey) : false;
  const showCount = isExpanded ? rows.length : Math.min(pageSize, rows.length);
  const visible = rows.slice(0, showCount);
  const hidden = rows.length - showCount;
  let html = renderSymbolTable(visible, { sortState });
  if (hidden > 0) {
    const label = isExpanded ? '收起' : `展开剩余 ${hidden} 条`;
    const action = isExpanded ? 'collapse' : 'expand';
    html += `<div class="section-footer"><button class="section-toggle" data-action="${action}" data-section="${sectionKey}">${label}</button></div>`;
  }
  return html;
}
