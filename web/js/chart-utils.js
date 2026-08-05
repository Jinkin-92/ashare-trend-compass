/* 图表坐标轴范围工具：纯函数，浏览器（window.ChartUtils）与 Node（module.exports）双端可用。
 * 对数轴范围必须在对数空间内留白——线性空间乘 0.9/1.1 会让留白在对数轴上不对称，
 * 数据集中在 1.0 附近时（0%~20% 区间）波形被压扁，这是历史 bug 的根因。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.ChartUtils = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  /**
   * 对数轴范围：输入比率序列（1+v/100，必须 >0），在对数空间按 padRatio 留白。
   * 数据波形保证占轴高约 1/(1+2*padRatio)。
   */
  function computeLogRange(ratios, padRatio) {
    const valid = (ratios || []).filter(r => r != null && isFinite(r) && r > 0);
    if (valid.length === 0) return { min: 0.5, max: 2.0 };
    const pad = padRatio == null ? 0.08 : padRatio;
    const logMin = Math.log(Math.min.apply(null, valid));
    const logMax = Math.log(Math.max.apply(null, valid));
    let span = logMax - logMin;
    if (span < 0.02) span = 0.02; // 数据近乎平坦时给最小跨度，防止轴过窄/除零
    const padLen = span * pad;
    let min = Math.exp(logMin - padLen);
    let max = Math.exp(logMax + padLen);
    if (!(min > 0)) min = 0.01;
    if (!(max > min)) max = min * 1.05;
    return { min: min, max: max };
  }

  /**
   * 线性轴范围：数据自适应 + 15% 留白（至少 ±5），向外取整到 step 的整数倍。
   * 不做硬钳制，极端行情不截断。
   */
  function computeLinearRange(values, padRatio, step) {
    const valid = (values || []).filter(v => v != null && isFinite(v));
    if (valid.length === 0) return { min: -10, max: 10 };
    const pad = padRatio == null ? 0.15 : padRatio;
    const st = step == null ? 10 : step;
    const dataMin = Math.min.apply(null, valid);
    const dataMax = Math.max.apply(null, valid);
    const padding = Math.max(5, (dataMax - dataMin) * pad);
    let min = Math.floor((dataMin - padding) / st) * st;
    let max = Math.ceil((dataMax + padding) / st) * st;
    if (max <= min) max = min + 2 * st;
    return { min: min, max: max };
  }

  return { computeLogRange: computeLogRange, computeLinearRange: computeLinearRange };
});
