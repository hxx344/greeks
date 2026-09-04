const $ = (id) => document.getElementById(id);
const money = (value) => `$${Number(value || 0).toLocaleString('en-US', {maximumFractionDigits: 2})}`;
const num = (value, digits = 2) => Number(value || 0).toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const utcTime = (value) => new Date(value).toLocaleTimeString('en-GB', {hour12: false, timeZone: 'UTC'});
const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'}[char]));

async function getJson(url, options) {
  const response = await fetch(url, options);
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : {}; } catch (_) { data = {detail: raw || `HTTP ${response.status}`}; }
  if (!response.ok) throw new Error(data.detail || 'Request failed');
  if (url.includes('/api/strategy/preview')) window.__latestPreview = data;
  if (url.includes('/api/trading/executions')) window.__latestExecutions = data.items || [];
  return data;
}

function selectedMap(legs) { return new Map((legs || []).map((leg) => [leg.symbol, leg])); }
function positionCell(item, selected) {
  const leg = item && selected.get(item.symbol);
  return `<td class="position">${leg ? `<span class="selected-mark ${leg.side.toLowerCase()}">${leg.side === 'Sell' ? 'S' : 'B'}</span>` : '--'}</td>`;
}
function quoteCell(item, field, className = '') {
  if (!item) return '<td class="empty-cell">--</td>';
  return `<td class="${className}">${field === 'price' ? money(item.mark_price) : num(item[field])}</td>`;
}
function deltaCell(item, side) {
  if (!item) return '<td class="empty-cell">--</td>';
  const value = Number(item.delta || 0);
  return `<td class="delta ${side}"><span class="delta-value">${value.toFixed(3)}</span><span class="delta-track"><i style="width:${Math.min(100, Math.abs(value) * 100)}%"></i></span></td>`;
}
function renderChain(items, preview) {
  const filtered = (items || []).filter((item) => item.expiry === preview?.expiry);
  const strikes = [...new Set(filtered.map((item) => Number(item.strike)))].sort((a, b) => a - b);
  const mid = strikes.length ? strikes.reduce((a, b) => a + b, 0) / strikes.length : 0;
  const strategyStrikes = (preview?.legs || []).map((leg) => Number(leg.strike)).filter((strike) => strikes.includes(strike));
  const nearby = strikes.slice().sort((a, b) => Math.abs(a - mid) - Math.abs(b - mid)).slice(0, 12);
  const focus = [...new Set([...nearby, ...strategyStrikes])].sort((a, b) => a - b);
  const selected = selectedMap(preview?.legs);
  $('chain').innerHTML = focus.map((strike) => {
    const call = filtered.find((item) => item.option_type === 'Call' && Number(item.strike) === strike);
    const put = filtered.find((item) => item.option_type === 'Put' && Number(item.strike) === strike);
    const selectedRow = [call, put].some((item) => item && selected.has(item.symbol));
    return `<tr class="${Math.abs(strike - mid) < 1 ? 'atm ' : ''}${selectedRow ? 'selected' : ''}">${quoteCell(call,'volume')}${quoteCell(call,'open_interest')}${deltaCell(call,'call')}${quoteCell(call,'bid_size')}${quoteCell(call,'bid','bid')}${quoteCell(call,'mark_price','mark')}${quoteCell(call,'ask','ask')}${quoteCell(call,'ask_size')}${positionCell(call,selected)}<td class="strike">${strike.toLocaleString()}</td>${positionCell(put,selected)}${quoteCell(put,'ask_size')}${quoteCell(put,'ask','ask')}${quoteCell(put,'mark_price','mark')}${quoteCell(put,'bid','bid')}${quoteCell(put,'bid_size')}${deltaCell(put,'put')}${quoteCell(put,'open_interest')}${quoteCell(put,'volume')}</tr>`;
  }).join('');
    $('chainCount').textContent = `${filtered.length} 个合约 · 展示 ${focus.length} 个行权价`;
  $('targetDate').textContent = preview?.expiry ? new Date(preview.expiry).toLocaleDateString('zh-CN', {month: 'short', day: 'numeric', timeZone: 'UTC'}) : '--';
  const buyCount = (preview?.legs || []).filter((leg) => leg.side === 'Buy').length;
  const sellCount = (preview?.legs || []).filter((leg) => leg.side === 'Sell').length;
  $('legSummary').textContent = `BUY ${buyCount} · SELL ${sellCount}`;
}
function payoffAt(price, preview) {
  let value = Number(preview?.net_credit_usd || 0);
  for (const leg of (preview?.legs || [])) {
    const intrinsic = leg.option_type === 'Call' ? Math.max(price - Number(leg.strike), 0) : Math.max(Number(leg.strike) - price, 0);
    value += (leg.side === 'Buy' ? 1 : -1) * intrinsic * Number(leg.qty || 1);
  }
  return value;
}
function renderPayoff(preview) {
  const canvas = $('payoffChart');
  if (!canvas || !preview?.legs?.length) return;
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio)); canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const width = rect.width; const height = rect.height;
  const strikes = preview.legs.map((leg) => Number(leg.strike));
  const lowStrike = Math.min(...strikes); const highStrike = Math.max(...strikes); const span = Math.max(1000, highStrike - lowStrike); const low = lowStrike - span * 0.32; const high = highStrike + span * 0.32;
  const samples = Array.from({length: 121}, (_, index) => { const price = low + (high - low) * index / 120; return {price, pnl: payoffAt(price, preview)}; });
  const values = samples.map((point) => point.pnl); const min = Math.min(...values, 0); const max = Math.max(...values, 0); const pad = Math.max(50, (max - min) * 0.12); const yMin = min - pad; const yMax = max + pad; const left = 48; const right = 15; const top = 18; const bottom = 28;
  const x = (price) => left + (price - low) / (high - low) * (width - left - right); const y = (pnl) => top + (yMax - pnl) / (yMax - yMin) * (height - top - bottom);
  ctx.clearRect(0, 0, width, height); ctx.font = '10px Inter, system-ui, sans-serif';
  for (let index = 0; index <= 4; index += 1) { const pnl = yMin + (yMax - yMin) * index / 4; const yp = y(pnl); ctx.strokeStyle = '#22313a'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, yp); ctx.lineTo(width - right, yp); ctx.stroke(); ctx.fillStyle = '#71818d'; ctx.fillText(`${pnl >= 0 ? '+' : ''}${Math.round(pnl).toLocaleString()}`, 5, yp + 3); }
  const zeroY = y(0); ctx.strokeStyle = '#71838b'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(left, zeroY); ctx.lineTo(width - right, zeroY); ctx.stroke(); ctx.setLineDash([]);
  ctx.beginPath(); samples.forEach((point, index) => index ? ctx.lineTo(x(point.price), y(point.pnl)) : ctx.moveTo(x(point.price), y(point.pnl))); ctx.lineTo(x(samples[samples.length - 1].price), zeroY); ctx.lineTo(x(samples[0].price), zeroY); ctx.closePath(); ctx.fillStyle = 'rgba(79, 156, 103, .18)'; ctx.fill();
  ctx.beginPath(); samples.forEach((point, index) => index ? ctx.lineTo(x(point.price), y(point.pnl)) : ctx.moveTo(x(point.price), y(point.pnl))); ctx.strokeStyle = '#d7f36b'; ctx.lineWidth = 2.4; ctx.stroke();
  for (const strike of strikes) { const xp = x(strike); ctx.strokeStyle = '#53646d'; ctx.setLineDash([2, 4]); ctx.beginPath(); ctx.moveTo(xp, top); ctx.lineTo(xp, height - bottom); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = '#aab9bf'; ctx.textAlign = 'center'; ctx.fillText(`${Math.round(strike).toLocaleString()}`, xp, height - 9); }
  const spot = Number(preview.btc_price || 0); if (spot >= low && spot <= high) { const xp = x(spot); ctx.strokeStyle = '#f2bd62'; ctx.setLineDash([5, 3]); ctx.beginPath(); ctx.moveTo(xp, top); ctx.lineTo(xp, height - bottom); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = '#f2bd62'; ctx.textAlign = 'center'; ctx.fillText(`BTC ${Math.round(spot).toLocaleString()}`, xp, 11); }
  const breaks = []; for (let index = 1; index < samples.length; index += 1) { if ((samples[index - 1].pnl < 0) !== (samples[index].pnl < 0)) { const a = samples[index - 1]; const b = samples[index]; breaks.push(a.price + (0 - a.pnl) * (b.price - a.price) / (b.pnl - a.pnl)); } }
  const maxProfit = Math.max(...values); const maxLoss = Math.min(...values); const current = spot ? payoffAt(spot, preview) : 0;
  $('payoffStats').innerHTML = `<div class="payoff-stat"><span>当前盈亏</span><strong class="${current >= 0 ? 'profit' : 'loss'}">${money(current)}</strong></div><div class="payoff-stat"><span>最大收益</span><strong class="profit">${money(maxProfit)}</strong></div><div class="payoff-stat"><span>最大风险</span><strong class="loss">${money(maxLoss)}</strong></div><div class="payoff-stat"><span>盈亏平衡</span><strong>${breaks.length ? breaks.map((point) => Math.round(point).toLocaleString()).join(' / ') : '--'}</strong></div>`;
}
function renderLegs(legs) { $('legs').innerHTML = (legs || []).map((leg) => `<div class="leg"><span class="leg-mark ${leg.side.toLowerCase()}">${leg.side === 'Sell' ? 'S' : 'B'} ${leg.option_type[0]}</span><div><div class="leg-title">${leg.side === 'Sell' ? '卖出' : '买入'} ${leg.option_type} · Δ ${Number(leg.delta).toFixed(3)}</div><div class="leg-symbol">${esc(leg.symbol)}</div></div><div class="leg-price"><strong>${money(leg.mark_price)}</strong><small>目标 ${Number(leg.target_delta).toFixed(2)}</small><small>预估费 ${money(leg.estimated_fee_usd)} / 封顶 ${money(leg.fee_cap_usd)}</small></div></div>`).join(''); }
function renderPositions(items) { $('positions').className = items.length ? 'positions' : 'positions empty'; $('positions').innerHTML = items.length ? items.map((p) => `<div class="position-row"><strong>${esc(p.symbol)}</strong><span>${esc(p.side)} · ${p.size}</span><span class="${p.unrealised_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${money(p.unrealised_pnl)}</span></div>`).join('') : '暂无持仓'; }
function renderLogs(items) { $('logs').innerHTML = (items || []).slice(0, 20).map((log) => `<div class="log-line"><span class="log-time">${utcTime(log.timestamp)}</span><span class="log-level ${log.level}">${log.level}</span><span>${esc(log.message)}</span></div>`).join(''); }
function renderExecutions(items) {
  const target = $('executions');
  if (!items || !items.length) { target.className = 'executions empty'; target.textContent = '暂无成交记录'; return; }
  const isClose = (item) => item.reduce_only === true || String(item.order_link_id || '').split('-')[1] === 'close';
  const timeBucket = (item) => { const time = new Date(item.exec_time || 0).getTime(); return Number.isFinite(time) ? Math.floor(time / 15000) : 0; };
  const groups = new Map();
  for (const item of items) {
    const link = String(item.order_link_id || '');
    const parts = link.split('-');
    let key = link;
    let label = '其他成交';
    const close = isClose(item);
    if (parts[0] === 'ic' && parts[1] === 'close' && parts[2]) { key = parts.slice(0, 3).join('-'); label = '平仓组合'; }
    else if (close) { key = `legacy-close-${timeBucket(item)}`; label = '平仓组合'; }
    else if (parts[0] === 'ic' && parts[1]) { key = parts.slice(0, 2).join('-'); label = '开仓组合'; }
    if (!groups.has(key)) groups.set(key, {label, items: [], fee: 0, cashflow: 0, currency: item.fee_currency || ''});
    const group = groups.get(key);
    group.items.push(item);
    group.fee += Number(item.exec_fee || 0);
    group.cashflow += (item.side === 'Sell' ? 1 : -1) * Number(item.exec_price || 0) * Number(item.exec_qty || 0);
    if (!group.currency) group.currency = item.fee_currency || '';
  }
  const openings = new Map();
  for (const item of items.filter((entry) => !isClose(entry))) {
    const list = openings.get(item.symbol) || [];
    list.push(item);
    openings.set(item.symbol, list);
  }
  target.className = 'executions';
  target.innerHTML = [...groups.values()].map((group) => {
    const legCount = new Set(group.items.map((item) => item.symbol)).size;
    let realized = 0;
    let matched = true;
    if (group.label === '平仓组合') {
      for (const close of group.items) {
        const candidates = openings.get(close.symbol) || [];
        const open = candidates.find((entry) => entry.side !== close.side);
        if (!open) { matched = false; continue; }
        const qty = Math.min(Number(close.exec_qty || 0), Number(open.exec_qty || 0));
        const direction = close.side === 'Sell' ? 1 : -1;
        realized += direction * (Number(close.exec_price || 0) - Number(open.exec_price || 0)) * qty - Number(close.exec_fee || 0) - Number(open.exec_fee || 0) * (qty / Math.max(Number(open.exec_qty || 1), 1e-12));
      }
    }
    const resultLabel = group.label === '平仓组合' ? '组合平仓收益' : '组合成交净额';
    const resultValue = group.label === '平仓组合' ? (matched ? realized : null) : group.cashflow - group.fee;
    const resultText = resultValue === null ? `${resultLabel} 待匹配开仓成交` : `${resultLabel} ${resultValue >= 0 ? '+' : ''}${resultValue.toFixed(6)} ${esc(group.currency)}`;
    return `<div class="execution-group"><div class="execution-group-head"><strong>${group.label} · ${legCount} 腿</strong><span>${resultText} · 手续费 -${group.fee.toFixed(6)} ${esc(group.currency)}</span></div>${group.items.map((item) => `<div class="execution-row"><strong>${esc(item.symbol)}</strong><span>${esc(item.side)} ${Number(item.exec_qty).toFixed(4)} · ${money(item.exec_price)}</span><span class="exec-fee">-${Number(item.exec_fee).toFixed(6)} ${esc(item.fee_currency)}</span></div>`).join('')}</div>`;
  }).join('');
}
async function load() {
  try {
    const requestedQty = Number($('quantity')?.value || 1);
    const previewUrl = Number.isFinite(requestedQty) && requestedQty > 0 ? `/api/strategy/preview?quantity=${encodeURIComponent(requestedQty)}` : '/api/strategy/preview';
    const [config, preview, chain, positions, logs, health, executions] = await Promise.all([getJson('/api/config'), getJson(previewUrl), getJson('/api/chain'), getJson('/api/positions'), getJson('/api/logs'), getJson('/api/account/health'), getJson('/api/trading/executions')]);
    window.__latestChain = chain.items || []; $('btcPrice').textContent = preview.btc_price ? money(preview.btc_price) : '--';
    $('environment').textContent = chain.source === 'bybit' ? (config.live_enabled ? 'LIVE' : 'MAINNET DATA / DRY') : 'UNAVAILABLE';
    $('creditValue').textContent = money(preview.net_credit_usd); $('lossValue').textContent = money(preview.max_loss_usd); $('marginValue').textContent = money(preview.estimated_margin_usd); $('marginSub').textContent = preview.margin_mode === 'PORTFOLIO_MARGIN' ? 'PM 压力测试估算' : 'Bybit Order IM'; $('maintenanceValue').textContent = money(preview.estimated_maintenance_margin_usd); $('costValue').textContent = money(preview.estimated_trading_cost_usd); $('feeSub').textContent = `Taker ${(Number(preview.estimated_fee_rate) * 100).toFixed(3)}% · 单腿上限 ${(Number(preview.fee_cap_pct) * 100).toFixed(0)}%`; if ($('rrValue')) $('rrValue').textContent = `${preview.risk_reward}x`; $('riskSub').textContent = `风险上限 ${money(config.max_risk_usd)}`;
    $('healthMode').textContent = health.available ? (health.margin_mode || 'UTA') : '仅实盘'; $('healthUnavailable').style.display = health.available ? 'none' : 'block'; $('healthContent').style.display = health.available ? 'block' : 'none'; if (health.available) { const im = Number(health.initial_margin_rate || 0) * 100; const mm = Number(health.maintenance_margin_rate || 0) * 100; $('imRate').textContent = `${im.toFixed(2)}%`; $('mmRate').textContent = `${mm.toFixed(2)}%`; $('imBar').style.width = `${Math.min(100, im)}%`; $('mmBar').style.width = `${Math.min(100, mm)}%`; $('availableBalance').textContent = money(health.available_balance_usd); $('marginBalance').textContent = money(health.margin_balance_usd); $('totalEquity').textContent = money(health.total_equity_usd); $('accountMode').textContent = health.margin_mode || '--'; }
    const quoteTime = preview.market_timestamp ? new Date(preview.market_timestamp) : new Date(); const age = Math.max(0, Math.round((Date.now() - quoteTime.getTime()) / 1000));
    $('statusValue').textContent = age > config.quote_stale_seconds ? '行情过期' : '策略就绪'; $('statusSub').textContent = `${chain.source === 'bybit' ? 'Bybit 主网行情' : '行情不可用'} · ${age}s 前`; $('expiry').textContent = `到期 ${new Date(preview.expiry).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit',timeZone:'UTC'})}`; $('chainSource').textContent = chain.source.toUpperCase();  $('refreshRate').textContent = config.market_refresh_seconds; $('nextOpen').textContent = config.open_time; $('modeTitle').textContent = config.live_enabled ? '实盘模式已启用' : '模拟模式'; $('modeText').textContent = config.live_enabled ? '确认后将向 Bybit 发送市价订单。' : '不会向交易所发送订单。'; $('openTrade').textContent = config.live_enabled ? '确认并开仓四腿' : '模拟开仓四腿'; renderChain(chain.items, preview); renderLegs(preview.legs); renderPositions(positions.items); renderLogs(logs.items); $('updateText').textContent = `行情 ${age}s · 每 ${config.market_refresh_seconds}s 更新`; $('updateDot').style.background = age > config.quote_stale_seconds ? '#ef8989' : '#d7f36b';
  } catch (error) { $('statusValue').textContent = '行情异常'; $('statusSub').textContent = error.message; $('updateText').textContent = '等待重试'; $('updateDot').style.background = '#ef8989'; }
}
async function openTrade() { const button = $('openTrade'); button.disabled = true; button.textContent = '执行中…'; try { const result = await getJson('/api/trading/open', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked, quantity:Number($('quantity').value)})}); window.alert(result.live ? '四腿实盘订单已提交' : '模拟订单已记录'); await load(); } catch (error) { window.alert(error.message); } finally { button.disabled = false; button.textContent = $('confirm').checked ? '确认并开仓四腿' : '模拟开仓四腿'; } }
function tick() { $('clock').textContent = `${new Date().toLocaleTimeString('en-GB',{hour12:false,timeZone:'UTC'})} UTC`; }
$('refresh').addEventListener('click', async () => { try { await getJson('/api/market/refresh',{method:'POST'}); await load(); } catch (error) { $('statusSub').textContent = error.message; } }); $('reloadPositions').addEventListener('click', load); $('openTrade').addEventListener('click', openTrade); $('confirm').addEventListener('change', () => { $('openTrade').textContent = $('confirm').checked ? '确认并开仓四腿' : '模拟开仓四腿'; }); tick(); setInterval(tick,1000); load(); setInterval(load,10000);
window.addEventListener('resize', () => { if (window.__latestPreview) renderPayoff(window.__latestPreview); }); setInterval(() => { if (window.__latestPreview) renderPayoff(window.__latestPreview); }, 1000);
setInterval(() => { if (window.__latestExecutions) renderExecutions(window.__latestExecutions); }, 500);
window.__desiredQty = window.localStorage.getItem('ic-quantity') || '';
const quantityField = $('quantity');
const quantityPreset = $('quantityPreset');
if (window.__desiredQty) quantityField.value = window.__desiredQty;
quantityField.addEventListener('input', () => {
  window.__desiredQty = quantityField.value;
  window.localStorage.setItem('ic-quantity', quantityField.value);
});
const refreshEstimate = () => { const value = Number(quantityField.value); if (value > 0) { window.__desiredQty = String(value); window.localStorage.setItem('ic-quantity', String(value)); load().finally(() => { quantityField.value = window.__desiredQty; }); } };
quantityPreset.addEventListener('change', () => { if (quantityPreset.value !== 'custom') { quantityField.value = quantityPreset.value; refreshEstimate(); } });
quantityField.addEventListener('change', refreshEstimate);
setInterval(() => { if (window.__desiredQty && document.activeElement !== quantityField && quantityField.value !== window.__desiredQty) quantityField.value = window.__desiredQty; }, 25);
async function closeTrade() { const button = $('closeTrade'); button.disabled = true; button.textContent = '执行中…'; try { const result = await getJson('/api/trading/close', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked})}); window.alert(result.live ? '四腿平仓订单已提交' : '模拟平仓已记录'); await load(); } catch (error) { window.alert(error.message); } finally { button.disabled = false; button.textContent = $('confirm').checked ? '确认并平仓四腿' : '模拟平仓四腿'; } }
$('closeTrade').addEventListener('click', closeTrade);
function quoteNet(quote, side, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg.side])); return (quote[side === 'Buy' ? 'quoteBuyList' : 'quoteSellList'] || []).reduce((sum, item) => { const requestedSide = requested.get(item.symbol) || 'Buy'; const takerSide = side === 'Sell' ? requestedSide : (requestedSide === 'Buy' ? 'Sell' : 'Buy'); return sum + (takerSide === 'Sell' ? 1 : -1) * Number(item.price || 0) * Number(item.qty || 0); }, 0); }
function quoteLegCount(quote) { return new Set((quote.quoteSellList || []).map((item) => item.symbol)).size; }
function quoteLegComparison(quote, side, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg])); const chain = new Map((window.__latestChain || []).map((item) => [item.symbol, item])); return (quote[side === 'Buy' ? 'quoteBuyList' : 'quoteSellList'] || []).map((item) => { const leg = requested.get(item.symbol) || {}; const takerSide = side === 'Sell' ? (leg.side || 'Buy') : ((leg.side || 'Buy') === 'Buy' ? 'Sell' : 'Buy'); const market = chain.get(item.symbol) || {}; const reference = Number(takerSide === 'Sell' ? market.bid : market.ask) || Number(market.mark_price) || 0; const quoted = Number(item.price || 0); const qty = Number(item.qty || leg.qty || 0); const diff = (takerSide === 'Sell' ? 1 : -1) * (quoted - reference) * qty; return `<div class="rfq-leg-row"><strong>${esc(item.symbol)}</strong><span>${takerSide} ${quoted.toFixed(4)} × ${qty}</span><span>链 ${reference.toFixed(4)}</span><b class="${diff >= 0 ? 'rfq-diff-positive' : 'rfq-diff-negative'}">差额 ${diff >= 0 ? '+' : ''}${diff.toFixed(4)}</b></div>`; }).join(''); }
const rfqMeta = $('rfqId')?.parentElement; if (rfqMeta && !$('rfqType')) { const type = document.createElement('span'); type.innerHTML = '类型 <b id="rfqType">--</b>'; rfqMeta.appendChild(type); }
function quoteChainNet(quote, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg])); const chain = new Map((window.__latestChain || []).map((item) => [item.symbol, item])); return (quote.quoteSellList || []).reduce((sum, item) => { const leg = requested.get(item.symbol) || {}; const market = chain.get(item.symbol) || {}; const price = Number((leg.side || 'Buy') === 'Sell' ? market.bid : market.ask) || Number(market.mark_price) || 0; return sum + ((leg.side || 'Buy') === 'Sell' ? 1 : -1) * price * Number(item.qty || leg.qty || 0); }, 0); }
function quoteNetDiff(quote, state) { return quoteNet(quote, 'Sell', state) - quoteChainNet(quote, state); }
function rfqFeeEstimate(quote) { const index = Number(window.__latestPreview?.btc_price || 0); const effectiveRate = Math.max(0.0003 * 0.5, 0.0003); return (quote.quoteSellList || []).reduce((sum, item) => { const price = Number(item.price || 0); const qty = Number(item.qty || 0); return sum + Math.min(effectiveRate * index, 0.07 * price) * qty; }, 0); }
function quoteSpread(quote, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg])); const chain = new Map((window.__latestChain || []).map((item) => [item.symbol, item])); return (quote.quoteSellList || []).reduce((sum, item) => { const leg = requested.get(item.symbol) || {}; const market = chain.get(item.symbol) || {}; const reference = Number((leg.side || 'Buy') === 'Sell' ? market.bid : market.ask) || Number(market.mark_price) || 0; return sum + Math.abs(Number(item.price || 0) - reference); }, 0); }
function renderRfq(state) {
  if (!state || !state.rfq_id || ['Canceled', 'Expired', 'Filled', 'Failed'].includes(state.status)) { $('rfqStatus').textContent = state?.status || '未创建'; $('rfqId').textContent = '--'; $('rfqType').textContent = '--'; $('rfqExpires').textContent = '--'; $('rfqQuoteCount').textContent = '0'; $('rfqQuotes').className = 'rfq-quotes empty'; $('rfqQuotes').textContent = '暂无活动 RFQ'; return; }
  $('rfqStatus').textContent = state.status || '--'; $('rfqId').textContent = state.rfq_id; $('rfqType').textContent = state.strategy_type === 'custom' ? '自定义' : (state.strategy_type || 'custom'); $('rfqExpires').textContent = state.expires_at ? new Date(Number(state.expires_at)).toLocaleTimeString('zh-CN') : '--'; const quotes = (state.quotes || []).slice().sort((a, b) => { const aCount = quoteLegCount(a); const bCount = quoteLegCount(b); const total = (state.legs || []).length; if ((aCount >= total) !== (bCount >= total)) return aCount >= total ? -1 : 1; if ((aCount > 0) !== (bCount > 0)) return aCount > 0 ? -1 : 1; return quoteNet(b, 'Sell', state) - quoteNet(a, 'Sell', state); }); $('rfqQuoteCount').textContent = String(quotes.length);
  $('rfqQuotes').className = quotes.length ? 'rfq-quotes' : 'rfq-quotes empty'; $('rfqQuotes').innerHTML = quotes.length ? quotes.map((quote) => { const legCount = quoteLegCount(quote); const complete = legCount >= (state.legs || []).length; const disabled = complete ? '' : ' disabled title="报价未覆盖全部策略腿"'; const netDiff = quoteNetDiff(quote, state); return `<div class="rfq-quote"><div><strong>${esc(quote.deskCode || '做市商')} · ${legCount}/${(state.legs || []).length} 腿</strong><span>${esc(quote.status || '--')} · 到期 ${quote.expiresAt ? new Date(Number(quote.expiresAt)).toLocaleTimeString('zh-CN') : '--'}</span></div><div class="rfq-quote-values"><span>Sell 净额 ${quoteNet(quote, 'Sell', state).toFixed(4)}</span><span class="${netDiff >= 0 ? 'rfq-diff-positive' : 'rfq-diff-negative'}">链净额差 ${netDiff >= 0 ? '+' : ''}${netDiff.toFixed(4)}</span><span class="rfq-fee">预估手续费 ${rfqFeeEstimate(quote).toFixed(6)} USDT <small>VIP0 · 50%折扣后最低0.03% · 单腿上限7%</small></span><button class="button ghost rfq-execute" data-rfq="${esc(quote.rfqId || state.rfq_id)}" data-quote="${esc(quote.quoteId || '')}" data-side="Sell"${disabled}>执行 Sell</button></div><div class="rfq-compare-title">Sell 报价方向 · 你的四腿成交方向（Sell 用 Bid1，Buy 用 Ask1）</div><div class="rfq-leg-compare">${quoteLegComparison(quote, 'Sell', state) || '<span>无 Sell 方向报价</span>'}</div></div>`; }).join('') : '等待做市商报价';
  document.querySelectorAll('.rfq-execute').forEach((button) => button.addEventListener('click', executeRfq));
}
async function loadRfq() { try { renderRfq(await getJson('/api/rfq/status')); } catch (error) { $('rfqStatus').textContent = error.message; } }
async function createRfq() { try { await getJson('/api/rfq/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked, counterparties:[], quantity:Number($('quantity').value)})}); await loadRfq(); } catch (error) { window.alert(error.message); } }
async function executeRfq(event) { const button = event.currentTarget; try { await getJson('/api/rfq/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked, rfq_id:button.dataset.rfq, quote_id:button.dataset.quote, quote_side:button.dataset.side})}); await loadRfq(); } catch (error) { window.alert(error.message); } }
async function cancelRfq() { try { const state = await getJson('/api/rfq/status?refresh=false'); if (!state.rfq_id) throw new Error('没有活动 RFQ'); await getJson('/api/rfq/cancel', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked, rfq_id:state.rfq_id})}); await loadRfq(); } catch (error) { window.alert(error.message); } }
$('rfqCreate').addEventListener('click', createRfq); $('rfqCancel').addEventListener('click', cancelRfq); setInterval(loadRfq, 3000); loadRfq();
const rfqPanel = $('rfqStatus')?.closest('.rfq-panel'); const workspace = document.querySelector('.workspace'); if (rfqPanel && workspace) workspace.before(rfqPanel);
