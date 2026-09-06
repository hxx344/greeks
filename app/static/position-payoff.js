/* Linear, cash-settled BTC options. Entry costs only; fees are shown separately. */
function positionPayoffAt(price, legs) {
  return legs.reduce((pnl, leg) => pnl + leg.quantity *
    ((leg.type === 'C' ? Math.max(price - leg.strike, 0) : Math.max(leg.strike - price, 0)) - leg.entry), 0);
}

function positionPayoffGroups(items) {
  const groups = new Map();
  let excluded = 0;
  for (const item of items || []) {
    if (Number(item.size) === 0) continue;
    const match = /^BTC-(\d{1,2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})-(\d+(?:\.\d+)?)-(C|P)(?:-(USDT|USDC))?$/.exec(String(item.symbol || '').toUpperCase());
    if (!match || !['Buy', 'Sell'].includes(item.side) || !Number.isFinite(Number(item.size)) || Number(item.size) <= 0 ||
        item.avg_price === null || item.avg_price === undefined || item.avg_price === '' || !Number.isFinite(Number(item.avg_price)) || Number(item.avg_price) < 0) { excluded++; continue; }
    const month = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'].indexOf(match[2]);
    const expiry = Date.UTC(2000 + Number(match[3]), month, Number(match[1]), 8);
    if (new Date(expiry).getUTCMonth() !== month || Number(match[4]) <= 0) { excluded++; continue; }
    const currency = match[6] || 'USDC';
    const source = item.source || 'bybit';
    const key = `${expiry}/${currency}/${source}`;
    if (!groups.has(key)) groups.set(key, {key, expiry, currency, source, legs: [], floating: 0});
    const group = groups.get(key);
    group.legs.push({strike: Number(match[4]), type: match[5], quantity: Number(item.size) * (item.side === 'Buy' ? 1 : -1), entry: Number(item.avg_price)});
    group.floating = group.floating !== null && item.unrealised_pnl != null && Number.isFinite(Number(item.unrealised_pnl)) ? group.floating + Number(item.unrealised_pnl) : null;
  }
  return {groups: [...groups.values()].sort((a, b) => a.expiry - b.expiry || a.key.localeCompare(b.key)), excluded};
}

function analyzePositionPayoff(group, spot) {
  const legs = group.legs;
  const strikes = [...new Set(legs.map(leg => leg.strike))].sort((a, b) => a - b);
  const nodes = [0, ...strikes];
  const values = nodes.map(price => positionPayoffAt(price, legs));
  const slope = legs.filter(leg => leg.type === 'C').reduce((sum, leg) => sum + leg.quantity, 0);
  const tolerance = 1e-8;
  const max = slope > tolerance ? Infinity : Math.max(...values);
  const min = slope < -tolerance ? -Infinity : Math.min(...values);
  const roots = [];
  for (let i = 0; i < nodes.length; i++) {
    if (Math.abs(values[i]) < tolerance) roots.push(nodes[i]);
    if (i && values[i - 1] * values[i] < 0) roots.push(nodes[i - 1] - values[i - 1] * (nodes[i] - nodes[i - 1]) / (values[i] - values[i - 1]));
  }
  if (Math.abs(slope) > tolerance) {
    const root = nodes.at(-1) - values.at(-1) / slope;
    if (root > nodes.at(-1)) roots.push(root);
  }
  const breaks = [...new Set(roots.map(value => Number(value.toFixed(6))))].sort((a, b) => a - b);
  const net = new Map();
  for (const leg of legs) {
    const key = `${leg.type}/${leg.strike}`;
    net.set(key, {...leg, quantity: (net.get(key)?.quantity || 0) + leg.quantity});
  }
  const netLegs = [...net.values()].filter(leg => Math.abs(leg.quantity) > tolerance).sort((a, b) => a.strike - b.strike);
  const condor = netLegs.length === 4 && netLegs.map(leg => leg.type).join('') === 'PPCC' &&
    netLegs.every((leg, i) => i === 0 || leg.strike > netLegs[i - 1].strike) &&
    netLegs.every((leg, i) => (i === 0 || i === 3 ? leg.quantity > 0 : leg.quantity < 0) && Math.abs(Math.abs(leg.quantity) - netLegs[0].quantity) < tolerance);
  const current = Number.isFinite(spot) && spot > 0 ? positionPayoffAt(spot, legs) : null;
  let stage = '等待现价', tone = 'neutral';
  if (current !== null) {
    tone = current > .005 ? 'profit' : current < -.005 ? 'loss' : 'neutral';
    if (Math.abs(current) <= .005) stage = '盈亏平衡';
    else if (breaks.some(price => Math.abs(spot - price) / spot <= .001)) stage = '盈亏平衡附近';
    else if (condor && current > 0 && Math.abs(current - max) < tolerance) stage = '最大盈利区';
    else if (condor && current < 0 && Math.abs(current - min) < tolerance) stage = '最大亏损区';
    else stage = current > 0 ? (condor ? '盈利缓冲区' : '到期盈利区') : (condor ? '亏损扩大区' : '到期亏损区');
  }
  const anchors = [...strikes, ...breaks, ...(current !== null ? [spot] : [])];
  const pad = Math.max((Math.max(...anchors) - Math.min(...anchors)) * .16, strikes[0] * .03);
  const low = Math.max(0, Math.min(...anchors) - pad), high = Math.max(...anchors) + pad;
  const points = [low, ...strikes, ...breaks, ...(current !== null ? [spot] : []), high]
    .filter(price => price >= low && price <= high).sort((a, b) => a - b).map(price => ({price, pnl: positionPayoffAt(price, legs)}));
  return {strikes, breaks, max, min, current, stage, tone, condor, low, high, points};
}

function positionExpiryLabel(expiry, now = Date.now()) {
  const minutes = Math.ceil((expiry - now) / 60000);
  if (minutes <= 0) return '已到期 · 等待持仓更新';
  const days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), mins = minutes % 60;
  return `${minutes <= 1440 ? '临近到期' : '持仓中'} · 剩余 ${days ? `${days}天 ` : ''}${hours}小时 ${mins}分`;
}

function positionPayoffSvg(model, spot, index, width = 920) {
  const height = width < 600 ? 270 : 292, left = width < 600 ? 52 : 72, right = 28, top = 34, bottom = 42;
  const values = model.points.map(point => point.pnl);
  const min = Math.min(0, ...values), max = Math.max(0, ...values), pad = Math.max((max - min) * .16, .1);
  const x = price => left + (price - model.low) / (model.high - model.low) * (width - left - right);
  const y = pnl => top + (max + pad - pnl) / (max - min + 2 * pad) * (height - top - bottom);
  const n = value => Number(value.toFixed(2));
  const path = model.points.map((point, i) => `${i ? 'L' : 'M'}${n(x(point.price))},${n(y(point.pnl))}`).join(' ');
  const fills = {profit: [], loss: []};
  for (let i = 1; i < model.points.length; i++) {
    const a = model.points[i - 1], b = model.points[i];
    const tone = (a.pnl + b.pnl) / 2 >= 0 ? 'profit' : 'loss';
    fills[tone].push(`M${n(x(a.price))},${n(y(0))} L${n(x(a.price))},${n(y(a.pnl))} L${n(x(b.price))},${n(y(b.pnl))} L${n(x(b.price))},${n(y(0))} Z`);
  }
  const roughStep = Math.max((max - min) / 4, .05);
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const step = [1, 2, 5, 10].find(value => value * magnitude >= roughStep) * magnitude;
  const gridValues = Array.from({length: Math.floor(max / step) - Math.ceil(min / step) + 1}, (_, i) => (Math.ceil(min / step) + i) * step);
  const grid = gridValues.map(pnl => {
    return `<line class="pp-grid" x1="${left}" x2="${width - right}" y1="${n(y(pnl))}" y2="${n(y(pnl))}"/><text x="${left - 12}" y="${n(y(pnl) + 4)}" text-anchor="end">${num(pnl, Math.abs(pnl) < 10 ? 2 : 0)}</text>`;
  }).join('');
  const tickCount = width < 600 ? 3 : 5;
  const ticks = Array.from({length: tickCount}, (_, i) => {
    const price = model.low + (model.high - model.low) * i / (tickCount - 1);
    return `<text x="${n(x(price))}" y="${height - 12}" text-anchor="middle">${num(price, 0)}</text>`;
  }).join('');
  const strikeLines = model.strikes.map(price => `<line class="pp-strike" x1="${n(x(price))}" x2="${n(x(price))}" y1="${top}" y2="${height - bottom}"/>`).join('');
  const breaks = model.breaks.map(price => `<circle class="pp-break" cx="${n(x(price))}" cy="${n(y(0))}" r="4"><title>盈亏平衡 ${num(price)}</title></circle>`).join('');
  const marker = model.current === null ? '' : `<line class="pp-spot" x1="${n(x(spot))}" x2="${n(x(spot))}" y1="${top}" y2="${height - bottom}"/><circle class="pp-spot-dot" cx="${n(x(spot))}" cy="${n(y(model.current))}" r="6"/><text class="pp-spot-label" x="${n(Math.max(left + 70, Math.min(width - right - 70, x(spot))))}" y="19" text-anchor="middle">BTC 现价 ${num(spot, 0)}</text>`;
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="持仓到期盈亏曲线。横轴 BTC 到期价格，纵轴盈亏。${model.stage}">${grid}${strikeLines}<path class="pp-profit-fill" d="${fills.profit.join(' ')}"/><path class="pp-loss-fill" d="${fills.loss.join(' ')}"/><line class="pp-zero" x1="${left}" x2="${width-right}" y1="${n(y(0))}" y2="${n(y(0))}"/><path class="pp-line" d="${path}"/>${breaks}${marker}${ticks}</svg>`;
}

function renderPositionPayoff() {
  const root = $('positionPayoffContent');
  if (!root) return;
  if (!window.__positionSnapshot || window.__positionError) {
    root.innerHTML = `<div class="empty">${window.__positionError ? '持仓更新失败，暂不显示曲线与阶段，等待重新同步。' : '等待持仓同步…'}</div>`;
    $('positionPayoffCount').textContent = '等待同步';
    return;
  }
  const {groups, excluded} = positionPayoffGroups(window.__positionSnapshot);
  $('positionPayoffCount').textContent = `${groups.length} 个到期组合`;
  const market = window.__positionMarket;
  const fresh = market && market.price > 0 && Date.now() - market.timestamp <= market.staleSeconds * 1000;
  const spot = fresh ? market.price : null;
  root.innerHTML = (excluded ? `<p class="pp-warning">${excluded} 笔持仓无法识别或成本数据不完整，未纳入下列曲线。</p>` : '') + (!groups.length ? '<div class="empty">暂无可计算的 BTC 期权持仓。持仓同步后将自动显示曲线。</div>' : groups.map((group, i) => {
    const model = analyzePositionPayoff(group, spot);
    const expired = group.expiry <= Date.now();
    const stage = expired ? '已到期 · 等待结算确认' : model.stage;
    const date = new Date(group.expiry).toISOString().slice(0, 10);
    const metric = (label, value, tone = '') => `<div><span>${label}</span><strong class="${tone}">${value}</strong></div>`;
    return `<article class="pp-group"><div class="pp-heading"><div><h3>${date}<span>08:00 UTC · ${esc(group.currency)}${group.source === 'demo' ? ' · 模拟持仓' : ''}</span></h3><p>${model.condor ? '铁鹰组合' : '期权组合'} · ${group.legs.length} 笔持仓 · 按实际数量与开仓均价</p></div><span class="pp-stage" data-tone="${expired ? 'neutral' : model.tone}">${stage}</span></div><div class="pp-timing"><span>${positionExpiryLabel(group.expiry)}</span><span>${fresh ? '现价随行情更新' : '行情不可用或已过期，暂不判断当前阶段'}</span></div><div class="pp-chart" tabindex="0" role="region" aria-label="持仓盈亏曲线，可横向滚动">${positionPayoffSvg(expired ? {...model, current: null} : model, spot, i, Math.max(300, Math.min(920, root.clientWidth || 920)))}</div><div class="pp-metrics">${metric('现价对应到期盈亏', !expired && model.current !== null ? money(model.current) : '--', model.tone)}${metric('当前浮动盈亏', group.floating === null ? '--' : money(group.floating), group.floating >= 0 ? 'profit' : 'loss')}${metric('最大到期收益', model.max === Infinity ? '无上限' : money(Math.max(0, model.max)))}${metric('最大到期亏损', model.min === -Infinity ? '无上限' : money(Math.max(0, -model.min)))}</div><div class="pp-boundaries"><span>盈亏平衡点 <b>${model.breaks.length ? model.breaks.map(value => money(value)).join(' / ') : '无独立交点'}</b></span><span>行权价 <b>${model.strikes.map(value => num(value, 0)).join(' / ')}</b></span></div></article>`;
  }).join(''));
}
