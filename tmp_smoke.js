// 临时烟测脚本：验证 JSON 端点 + 模拟 l2 空态
const fs = require('fs');
const path = require('path');

const webData = 'web/data';
function load(name) {
  return JSON.parse(fs.readFileSync(path.join(webData, name), 'utf-8'));
}

const l2 = load('l2-SW_801012.json');
console.log('l2-SW_801012 stocks:', l2.stocks.length, '(预期 0 → empty-state)');
console.log('  parent:', l2.parent_id, l2.parent_name);

if (l2.stocks.length === 0) {
  const html = '<div class="empty-state">' +
    '<p>该二级行业下未挂载个股。</p>' +
    '<p class="muted">stock.parent_id 指向一级行业，二级行业 → 成分股映射未维护。</p>' +
    '<p>→ <a href="l1.html?l1=' + l2.parent_id + '">查看 <b>' + l2.parent_name +
    '</b> 下所有个股</a></p></div>';
  console.log('  空态 HTML 长度:', html.length);
}

const l1 = load('l1-SW_801890.json');
console.log('\nl1-SW_801890:', l1.name);
console.log('  l2_children:', l1.l2_children.length, '个');
console.log('  stocks:', l1.stocks.length, '只');
const nullTemp = l1.stocks.filter(s => s.temperature == null).length;
console.log('  temperature null:', nullTemp, '/', l1.stocks.length, '(sync 没完，正常)');

const idx = load('index-l1.json');
console.log('\nindex-l1: groups=', idx.groups.length, 'trade_date=', idx.trade_date);
console.log('  index:', idx.groups.filter(g => g.node_type === 'index').length);
console.log('  industry_l1:', idx.groups.filter(g => g.node_type === 'industry_l1').length);
console.log('  index 样例:', JSON.stringify(idx.groups[0]));
console.log('  L1 样例:', JSON.stringify(idx.groups.find(g => g.node_type === 'industry_l1')));
