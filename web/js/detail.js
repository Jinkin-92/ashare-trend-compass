/* A-Share Trend Compass 详情页脚本 */
/* 依赖 list-shared.js（TEMP_COLORS / TEMP_LEVELS / 自选股工具） */

let DETAIL_STATE = { currentScale: 'linear' };

/** 获取/复用图表实例，避免重复 echarts.init 造成泄漏。 */
function getChart(domId) {
  const dom = document.getElementById(domId);
  let chart = echarts.getInstanceByDom(dom);
  if (!chart) {
    chart = echarts.init(dom);
    window.addEventListener('resize', () => chart.resize());
  }
  return chart;
}

function getSymbol() {
  const params = new URLSearchParams(window.location.search);
  return params.get('symbol') || 'IDX_000001';
}

async function init() {
  const symbol = getSymbol();
  document.getElementById('symbol-name').textContent = `品种详情：${symbol}`;

  // 并行拉 指标历史 + 基准
  let data, bench;
  try {
    [data, bench] = await Promise.all([
      fetch(`data/indicators/indicator-${symbol}.json`).then(r => r.ok ? r.json() : null),
      fetch('data/benchmark.json').then(r => r.ok ? r.json() : null),
    ]);
  } catch (e) {
    console.error('加载失败:', e);
  }

  if (!data || !data.dates || data.dates.length === 0) {
    document.getElementById('symbol-meta').textContent = `未找到 ${symbol} 的指标数据`;
    return;
  }

  document.getElementById('symbol-meta').textContent =
    `${data.name || symbol}（${data.node_type || ''}） · 截面日期 ${data.dates[data.dates.length - 1]} · 共 ${data.dates.length} 个交易日`;

  renderDetailWatch(symbol, data.name);
  document.addEventListener('watchlist-changed', () => renderDetailWatch(symbol, data.name));

  renderReturnChart(data, bench);
  renderTempTimeline(data);
  renderRsChart(data);

  // 坐标切换
  document.querySelectorAll('#scale-tabs button').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('#scale-tabs button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      DETAIL_STATE.currentScale = btn.dataset.scale;
      renderReturnChart(data, bench);
    };
  });
}

function renderReturnChart(data, bench) {
  const dates = data.dates;
  const closes = data.close || [];
  const base = closes[0];
  if (!base || base <= 0) {
    document.getElementById('return-chart').innerHTML = '<p>无 close 数据</p>';
    return;
  }
  // 累计涨幅（%），过滤 null/0
  const cumPct = closes.map(c => c == null || c <= 0 ? null : (c / base - 1) * 100);
  const validCum = cumPct.filter(v => v != null);
  if (validCum.length === 0) {
    document.getElementById('return-chart').innerHTML = '<p>无 close 数据</p>';
    return;
  }

  // 找出右侧区间
  const rightRanges = [];
  let rangeStart = null;
  for (let i = 0; i < dates.length; i++) {
    if (data.is_right_side && data.is_right_side[i]) {
      if (rangeStart === null) rangeStart = dates[i];
    } else if (rangeStart !== null) {
      rightRanges.push([rangeStart, dates[i - 1]]);
      rangeStart = null;
    }
  }
  if (rangeStart !== null) rightRanges.push([rangeStart, dates[dates.length - 1]]);

  const isLog = DETAIL_STATE.currentScale === 'log';

  // 对数 Y 轴：series 用比率（1+v/100），≤0 的点无法画在对数轴上，置 null。
  // 范围在对数空间内留白（ChartUtils），避免数据集中在 1.0 附近时波形被压扁。
  const ratios = validCum.map(v => 1 + v / 100).filter(r => r > 0);
  const logRange = ChartUtils.computeLogRange(ratios);
  const yMinLog = logRange.min;
  const yMaxLog = logRange.max;

  // 等距辅助线（每 20% 一条）只在对数（百分比）坐标下有意义；
  // 线性（点数）坐标下价位尺度与百分比不同，不画。
  const auxiliaryLines = [];
  if (isLog) {
    const auxLo = (yMinLog - 1) * 100;
    const auxHi = (yMaxLog - 1) * 100;
    for (let v = Math.ceil(auxLo / 20) * 20; v <= auxHi; v += 20) {
      const yVal = 1 + v / 100;
      if (yVal <= 0) continue;
      auxiliaryLines.push({
        yAxis: yVal,
        lineStyle: { type: 'dashed', color: '#bbb' },
        label: { formatter: (v >= 0 ? '+' : '') + v + '%', position: 'insideEnd', fontSize: 10, color: '#888' }
      });
    }
  }

  const chart = getChart('return-chart');
  // 两个坐标系的核心区别：
  // - 线性坐标：Y 轴是点数（原始收盘价），看绝对价位与价格形态
  // - 对数坐标：Y 轴是百分比（累计涨幅的比率 1+v/100 取对数），等距=等比例
  const seriesData = isLog
    ? cumPct.map(v => (v == null || 1 + v / 100 <= 0) ? null : 1 + v / 100)
    : closes.map(c => (c == null || c <= 0) ? null : c);

  // 基准对齐：线性模式换算到主品种同一起点的"点数"（base*(1+cum/100)），对数模式用比率
  const benchData = (bench && bench.dates) ? alignBenchToDates(bench, dates) : [];
  const benchSeriesData = isLog
    ? benchData.map(v => (v == null || 1 + v / 100 <= 0) ? null : 1 + v / 100)
    : benchData.map(v => (v == null) ? null : base * (1 + v / 100));

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      // 显示具体 close 数值 + 累计涨幅百分比
      formatter: (params) => {
        if (!params || params.length === 0) return '';
        const idx = params[0].dataIndex;
        const date = params[0].axisValue;
        let html = `<div style="font-weight:bold;margin-bottom:4px">${date}</div>`;
        // 主品种 close + 累计涨幅
        if (idx < data.close.length && data.close[idx] != null) {
          html += `<div style="margin-bottom:6px;padding:4px;background:#fffbe6;border-left:3px solid #d4a017">`;
          html += `<div style="font-weight:bold">${data.name}</div>`;
          html += `<div>收盘价: <b>${(+data.close[idx]).toFixed(2)}</b></div>`;
          const pct = cumPct[idx];
          if (pct != null) {
            const sign = pct >= 0 ? '+' : '';
            html += `<div>累计: <b>${sign}${pct.toFixed(2)}%</b></div>`;
          }
          html += `</div>`;
        }
        params.slice(1).forEach(p => {
          const v = p.value;
          if (v == null) {
            html += `<div>${p.marker} ${p.seriesName}: -</div>`;
          } else {
            // 基准：线性模式是同起点点数，对数模式是比率，统一换算回涨幅
            const pct = isLog ? ((v - 1) * 100) : ((v / base - 1) * 100);
            const sign = pct >= 0 ? '+' : '';
            html += `<div>${p.marker} ${p.seriesName}: ${sign}${pct.toFixed(2)}%</div>`;
          }
        });
        return html;
      },
    },
    legend: { top: 0, data: [data.name, bench ? bench.name : null].filter(Boolean) },
    grid: { left: 60, right: 30, top: 40, bottom: 56 },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 16, bottom: 6 },
    ],
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    yAxis: isLog
      ? {
          type: 'log',
          name: '累计涨幅（对数坐标）',
          nameTextStyle: { fontSize: 11 },
          axisLabel: { formatter: v => (((v - 1) * 100).toFixed(0)) + '%' },
          splitLine: { show: false },
          min: yMinLog,
          max: yMaxLog,
        }
      : {
          type: 'value',
          name: '点数（线性坐标）',
          nameTextStyle: { fontSize: 11 },
          scale: true,
          axisLabel: { fontSize: 10 },
          splitLine: { show: false },
        },
    series: [
      {
        name: data.name,
        type: 'line',
        data: seriesData,
        showSymbol: false,
        smooth: true,
        lineStyle: { color: '#d4a017', width: 2 },
        areaStyle: { color: 'rgba(212, 160, 23, 0.10)' },
        markArea: {
          itemStyle: { color: 'rgba(240, 230, 140, 0.35)' },
          data: rightRanges.map(([s, e]) => [{ xAxis: s }, { xAxis: e }]),
        },
        markLine: { silent: true, symbol: 'none', data: auxiliaryLines },
      },
      ...(benchData.length > 0 ? [{
        name: bench.name,
        type: 'line',
        data: benchSeriesData,
        showSymbol: false,
        smooth: true,
        lineStyle: { color: '#888', width: 1, type: 'dashed' },
      }] : []),
    ],
  }, true);
}

/** 详情页头部自选按钮：星标 + 文字（"加入自选" / "已在自选"）。 */
function renderDetailWatch(symbol, name) {
  const el = document.getElementById('watch-toggle');
  if (!el) return;
  const on = isWatched(symbol);
  const star = on ? '★' : '☆';
  const label = on ? '已在自选' : '加入自选';
  const cls = on ? 'watch-btn on' : 'watch-btn';
  el.innerHTML = `<button class="${cls}" data-symbol="${symbol}" data-name="${name || symbol}" title="点击加入/移出自选清单">${star} <span class="watch-btn-label">${label}</span></button>`;
  el.querySelector('button').onclick = () => {
    toggleWatch(symbol, name);
  };
}

/** 把基准 cum_pct 对齐到品种的 dates 数组。基准缺失日期用 null。 */
function alignBenchToDates(bench, dates) {
  if (!bench || !bench.dates) return [];
  const m = new Map();
  for (let i = 0; i < bench.dates.length; i++) {
    m.set(bench.dates[i], bench.cum_pct[i]);
  }
  return dates.map(d => m.has(d) ? m.get(d) : null);
}

function renderTempTimeline(data) {
  const dates = data.dates;
  const temps = data.temperature || [];
  const scores = data.temperature_score || [];

  // 连续相同温度合并成色带
  const markAreaData = [];
  let runStart = null, runTemp = null;
  for (let k = 0; k <= temps.length; k++) {
    const t = k < temps.length ? temps[k] : null;
    if (t && runTemp !== t) {
      if (runTemp !== null) markAreaData.push([{ xAxis: dates[runStart] }, { xAxis: dates[k - 1] }]);
      runStart = k;
      runTemp = t;
    } else if (!t && runTemp !== null) {
      markAreaData.push([{ xAxis: dates[runStart] }, { xAxis: dates[k - 1] }]);
      runTemp = null;
    }
  }

  const chart = getChart('temp-timeline-chart');
  chart.setOption({
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const date = params[0].axisValue;
        const idx = dates.indexOf(date);
        if (idx < 0) return date;
        const t = temps[idx];
        const score = scores[idx];
        const days = data.right_side_days ? data.right_side_days[idx] : null;
        const rs = data.rs_score ? data.rs_score[idx] : null;
        return `${date}<br/>温度: <b>${t || '--'}</b>`
          + (score != null ? `（${(+score).toFixed(1)} 分）` : '')
          + (days != null && data.is_right_side && data.is_right_side[idx] ? '<br/>右侧: +' + days + '天' : '')
          + (rs != null ? '<br/>RS: ' + rs : '');
      },
    },
    grid: { left: 50, right: 20, top: 30, bottom: 30 },
    dataZoom: [{ type: 'inside' }],
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'value',
      min: 0, max: 7,
      interval: 1,
      axisLabel: { formatter: v => TEMP_LEVELS[7 - v] || '', fontSize: 10 },
      splitLine: { lineStyle: { type: 'dashed', color: '#eee' } },
    },
    series: [{
      type: 'line',
      data: temps.map(t => t ? (7 - TEMP_LEVELS.indexOf(t)) : null),
      showSymbol: false,
      step: 'middle',
      lineStyle: { color: '#999', width: 1 },
      markArea: {
        silent: true,
        itemStyle: { opacity: 0.6 },
        data: markAreaData.map(([s, e]) => {
          const t = temps[dates.indexOf(s.xAxis)] || '平';
          return [{ ...s, itemStyle: { color: TEMP_COLORS[t] || '#999' } }, e];
        }),
      },
    }],
  }, true);
}

function renderRsChart(data) {
  const dates = data.dates;
  const rs = (data.rs_score || []).map(v => v == null ? null : +v);

  // 找 RS 变化超过 5 分的位置打箭头
  const marks = [];
  for (let i = 5; i < rs.length; i++) {
    const a = rs[i - 5], b = rs[i];
    if (a == null || b == null) continue;
    const diff = b - a;
    if (diff > 5) marks.push({ xAxis: dates[i], yAxis: rs[i], symbol: 'triangle', symbolSize: 10, itemStyle: { color: '#198754' }, label: { show: false } });
    else if (diff < -15) marks.push({ xAxis: dates[i], yAxis: rs[i], symbol: 'triangle', symbolRotate: 180, symbolSize: 12, itemStyle: { color: '#dc3545' }, label: { show: false } });
    else if (diff < -5) marks.push({ xAxis: dates[i], yAxis: rs[i], symbol: 'triangle', symbolRotate: 180, symbolSize: 10, itemStyle: { color: '#fd7e14' }, label: { show: false } });
  }

  const chart = getChart('rs-chart');
  chart.setOption({
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        if (!params || params.length === 0) return '';
        const p = params[0];
        return `${p.axisValue}<br/>${p.marker} RS: <b>${p.value != null ? p.value : '-'}</b> / 99`;
      },
    },
    grid: { left: 50, right: 20, top: 30, bottom: 30 },
    dataZoom: [{ type: 'inside' }],
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'value',
      min: 0, max: 100,
      splitLine: { lineStyle: { type: 'dashed', color: '#eee' } },
    },
    series: [{
      name: 'RS',
      type: 'line',
      data: rs,
      showSymbol: false,
      smooth: true,
      lineStyle: { color: '#0d6efd', width: 2 },
      markPoint: { data: marks, symbolOffset: [0, -8] },
    }],
  }, true);
}

init();
