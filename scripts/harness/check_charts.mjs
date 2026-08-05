// 对数/线性坐标 harness：图表轴范围的自检门禁。
// 三层检查：
//   1) ChartUtils 纯函数单测（对数空间留白、边缘情形）
//   2) detail.js renderReturnChart 功能性测试（stub DOM/ECharts，真实跑线性/对数两种模式）
//   3) 静态检查（app.js 对数轴必须显式 min/max；HTML 必须先引入 chart-utils.js）
// 用法：node scripts/harness/check_charts.mjs   （任一 FAIL 即 exit 1）
import { createRequire } from 'module';
import vm from 'vm';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const ChartUtils = require(path.join(ROOT, 'web/js/chart-utils.js'));

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`PASS  ${name}`);
  } else {
    failures++;
    console.log(`FAIL  ${name}${detail ? ' — ' + detail : ''}`);
  }
}

// ---------------------------------------------------------------------------
// 1) ChartUtils 单测
// ---------------------------------------------------------------------------
function logTightness(range, data) {
  const lo = Math.log(Math.min(...data)), hi = Math.log(Math.max(...data));
  return (hi - lo) / (Math.log(range.max) - Math.log(range.min));
}

// 用户场景：累计涨幅 0%~20%（比率 1.0~1.2）
{
  const ratios = [1.0, 1.05, 1.1, 1.15, 1.2];
  const r = ChartUtils.computeLogRange(ratios);
  check('log范围>0且min<max（0-20%场景）', r.min > 0 && r.max > r.min, JSON.stringify(r));
  const t = logTightness(r, ratios);
  check('log轴数据占比≥75%（0-20%场景不压扁）', t >= 0.75, `占比=${(t * 100).toFixed(1)}%`);
}
// 大波动场景：-40%~+150%（比率 0.6~2.5）
{
  const ratios = [0.6, 0.9, 1.0, 1.8, 2.5];
  const r = ChartUtils.computeLogRange(ratios);
  const t = logTightness(r, ratios);
  check('log轴数据占比≥75%（大波动场景）', r.min > 0 && t >= 0.75, `占比=${(t * 100).toFixed(1)}%`);
}
// 平坦数据：全部 1.0
{
  const r = ChartUtils.computeLogRange([1.0, 1.0, 1.0]);
  check('log平坦数据不崩溃且min>0', r.min > 0 && r.max > r.min && isFinite(r.min) && isFinite(r.max), JSON.stringify(r));
}
// 单点 / 空 / 非法值
{
  const r1 = ChartUtils.computeLogRange([1.3]);
  check('log单点数据', r1.min > 0 && r1.max > r1.min, JSON.stringify(r1));
  const r0 = ChartUtils.computeLogRange([]);
  check('log空数据回退默认', r0.min > 0 && r0.max > r0.min);
  const rn = ChartUtils.computeLogRange([null, -1, 0, NaN]);
  check('log全非法数据回退默认', rn.min > 0 && rn.max > rn.min);
}
// 线性范围：含数据且有留白
{
  const r = ChartUtils.computeLinearRange([0, 20]);
  check('linear范围覆盖数据（0-20%场景）', r.min <= 0 && r.max >= 20, JSON.stringify(r));
  const r2 = ChartUtils.computeLinearRange([-55, 137]);
  check('linear范围覆盖数据（大波动）', r2.min <= -55 && r2.max >= 137, JSON.stringify(r2));
  const rf = ChartUtils.computeLinearRange([5, 5]);
  check('linear平坦数据不崩溃', rf.max > rf.min, JSON.stringify(rf));
}

// ---------------------------------------------------------------------------
// 2) detail.js renderReturnChart 功能性测试（stub DOM + ECharts）
// ---------------------------------------------------------------------------
const captured = {}; // domId -> 最后一次 setOption 的 option
const domStub = () => ({ textContent: '', innerHTML: '' });
const sandbox = {
  console,
  URLSearchParams,
  fetch: () => Promise.reject(new Error('harness 不发网络请求')),
  document: {
    getElementById: () => domStub(),
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  echarts: {
    getInstanceByDom: () => null,
    init: (dom) => ({ setOption: (opt) => { captured[dom.id || 'return-chart'] = opt; }, resize: () => {} }),
  },
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/js/chart-utils.js'), 'utf-8'), sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/js/detail.js'), 'utf-8'), sandbox);

// 构造 0%~20% 场景数据（close 100 → 120，250 个交易日正弦波动）
const n = 250;
const dates = Array.from({ length: n }, (_, i) => `2025-01-${String(i + 1).padStart(2, '0')}`);
const closes = Array.from({ length: n }, (_, i) => 100 + 20 * (i / n) + 3 * Math.sin(i / 7));
const data = { dates, close: closes, name: 'TEST', is_right_side: dates.map(() => false) };

vm.runInContext(`DETAIL_STATE.currentScale = 'linear'; renderReturnChart(${JSON.stringify(data)}, null);`, sandbox);
const optLinear = captured['return-chart'];
vm.runInContext(`DETAIL_STATE.currentScale = 'log'; renderReturnChart(${JSON.stringify(data)}, null);`, sandbox);
const optLog = captured['return-chart'];

check('linear模式 yAxis.type=value（点数轴）', optLinear && optLinear.yAxis.type === 'value');
check('linear模式 series=原始收盘价（点数为Y轴）',
  optLinear && optLinear.series[0].data.every((v, i) => v === null || Math.abs(v - closes[i]) < 1e-9));
check('log模式 yAxis.type=log（百分比轴）', optLog && optLog.yAxis.type === 'log');
check('log模式 series=累计涨幅比率', optLog && optLog.series[0].data.every(v => v === null || (v > 0.9 && v < 1.4)));
check('log模式显式min/max且min>0', optLog && optLog.yAxis.min > 0 && optLog.yAxis.max > optLog.yAxis.min,
  optLog && `min=${optLog.yAxis.min} max=${optLog.yAxis.max}`);
check('linear/log series 数据确实不同（切换生效）',
  optLinear && optLog && JSON.stringify(optLinear.series[0].data) !== JSON.stringify(optLog.series[0].data));
if (optLog) {
  const sd = optLog.series[0].data.filter(v => v != null);
  const t = logTightness({ min: optLog.yAxis.min, max: optLog.yAxis.max }, sd);
  check('log轴数据占比≥70%（renderReturnChart 实测不压扁）', t >= 0.70, `占比=${(t * 100).toFixed(1)}%`);
  // tooltip 必须能显示点位（收盘价）与累计涨幅
  const tip = optLog.tooltip.formatter([{ dataIndex: n - 1, axisValue: dates[n - 1], value: sd[sd.length - 1], marker: '', seriesName: 'TEST' }]);
  check('tooltip 显示收盘价点位', typeof tip === 'string' && tip.includes('收盘价'), String(tip).slice(0, 80));
  check('tooltip 显示累计涨幅', typeof tip === 'string' && tip.includes('累计'), String(tip).slice(0, 80));
}
if (optLinear && optLog) {
  // 百分比辅助线只在对数坐标下画；线性（点数）坐标不画
  const linLines = optLinear.series[0].markLine.data.length;
  const logLines = optLog.series[0].markLine.data.length;
  check('辅助线仅对数模式生成', linLines === 0 && logLines > 0, `linear=${linLines} log=${logLines}`);
  const tipLin = optLinear.tooltip.formatter([{ dataIndex: n - 1, axisValue: dates[n - 1], value: optLinear.series[0].data[n - 1], marker: '', seriesName: 'TEST' }]);
  check('linear模式 tooltip 显示收盘价+累计', tipLin.includes('收盘价') && tipLin.includes('累计'), String(tipLin).slice(0, 80));
}

// ---------------------------------------------------------------------------
// 3) 静态检查
// ---------------------------------------------------------------------------
const appSrc = fs.readFileSync(path.join(ROOT, 'web/js/app.js'), 'utf-8');
const logAxisMatch = appSrc.match(/type:\s*'log'[^}]*/);
check('app.js 对数轴显式 min/max', !!(logAxisMatch && logAxisMatch[0].includes('min:') && logAxisMatch[0].includes('max:')),
  logAxisMatch ? logAxisMatch[0].slice(0, 120) : '未找到 log 轴配置');
check('app.js 使用 ChartUtils', appSrc.includes('ChartUtils.computeLogRange'));

for (const page of ['index.html', 'detail.html']) {
  const html = fs.readFileSync(path.join(ROOT, 'web', page), 'utf-8');
  const iUtils = html.indexOf('chart-utils.js');
  const iApp = html.indexOf('js/app.js') >= 0 ? html.indexOf('js/app.js') : html.indexOf('js/detail.js');
  check(`${page} 先引入 chart-utils.js`, iUtils >= 0 && iApp >= 0 && iUtils < iApp);
}

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
