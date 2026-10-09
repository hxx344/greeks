const $ = (id) => document.getElementById(id);
const money = (value) => `$${Number(value || 0).toLocaleString('en-US', {maximumFractionDigits: 2})}`;
const num = (value, digits = 2) => Number(value || 0).toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const utcTime = (value) => new Date(value).toLocaleTimeString('en-GB', {hour12: false, timeZone: 'UTC'});
const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'}[char]));
const optionalMoney = (value) => value === null || value === undefined ? '--' : money(value);
const marginLabel = (value) => ({REGULAR_MARGIN: '常规保证金', PORTFOLIO_MARGIN: '组合保证金', ISOLATED_MARGIN: '逐仓保证金'}[value] || value || 'UTA');
const selectedStrategyMode = () => $('strategyMode').value === 'short_strangle' ? 'short_strangle' : 'iron_condor';
const strategyName = (mode) => mode === 'short_strangle' ? '双腿卖出' : '四腿铁鹰';
const previewStrategyMode = (preview) => preview?.strategy_mode ?? 'iron_condor';
const unboundedPreview = (preview) => preview?.unbounded_loss === true || previewStrategyMode(preview) === 'short_strangle';
let strategySelectionInitialized = false;

function renderStrategySelection() {
  const mode = selectedStrategyMode(), naked = mode === 'short_strangle', config = window.__strategyConfig;
  $('confirmText').textContent = naked ? '我已核对双腿与报价，并了解无保护腿、亏损无上限' : '我已核对四腿、报价和风险上限';
  $('strategyDescription').textContent = naked ? '卖出 Call + Put · 无保护腿' : '双侧卖出 · 远端保护';
  $('strategyRisk').dataset.unbounded = String(naked);
  $('strategyRisk').textContent = naked ? `亏损无上限；独立保证金预算 ${config ? money(config.max_margin_usd ?? 2500) : '--'}，不代表最大亏损。` : '买入远端保护腿，按最大亏损控制风险。';
  $('riskSub').textContent = naked ? '无保护腿 · 最大亏损无上限' : `风险上限 ${config ? money(config.max_risk_usd) : '--'}`;
  if (config) $('automaticMode').textContent = `自动任务：${strategyName(config.strategy_mode)} · ${config.auto_open ? '已启用' : '未启用'}`;
}
function hasCurrentStrategyPreview() {
  const selection = window.__previewSelection;
  return Boolean(window.__latestPreview && selection && selection.mode === selectedStrategyMode()
    && selection.quantity === Number($('quantity').value)
    && previewStrategyMode(window.__latestPreview) === selection.mode);
}
function openingSelection() {
  if (!hasCurrentStrategyPreview() || window.__strategyUnavailable || window.__marketReadOnly) throw new Error('请等待当前结构和数量的预览完成，再开仓或创建询价。');
  if (window.__tradingBlocked || window.__openingBlocked) throw new Error('当前禁止新开仓，请检查交易或询价状态。');
  if (window.__liveEnabled && !$('confirm').checked) throw new Error('请核对当前结构、报价与风险后勾选确认。');
  return {strategy_mode: selectedStrategyMode(), quantity: Number($('quantity').value), confirm_live: $('confirm').checked === true};
}
function invalidateStrategySelection(message) {
  $('confirm').checked = false;
  window.__strategyUnavailable = true;
  clearStrategyDisplay(message);
  renderStrategySelection();
  updateTradeControls();
}

function showNotice(message, title = '操作结果', tone = 'info') {
  const notice = $('notice');
  $('noticeTitle').textContent = title;
  $('noticeText').textContent = message;
  notice.dataset.tone = tone;
  notice.hidden = false;
}
function showTradeResult(payload, action) {
  const complete = payload.results?.length && payload.results.every((item) => ['filled', 'simulated'].includes(item.status));
  showNotice(tradeResultMessage(payload, action), `${action}结果`, complete ? 'info' : 'error');
}
$('dismissNotice')?.addEventListener('click', () => { $('notice').hidden = true; });
$('toggleDepth')?.addEventListener('click', () => {
  const full = $('chainPanel').dataset.depth !== 'full';
  $('chainPanel').dataset.depth = full ? 'full' : 'compact';
  $('toggleDepth').textContent = full ? '精简盘口 ↙' : '完整盘口 ↗';
  $('toggleDepth').setAttribute('aria-pressed', String(full));
  for (const heading of document.querySelectorAll('.call-head, .put-head')) heading.colSpan = full ? 9 : 5;
});

async function getJson(url, options) {
  const response = await fetch(url, options);
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : {}; } catch (_) { data = {detail: raw || `HTTP ${response.status}`}; }
  if (!response.ok) throw new Error(data.detail || 'Request failed');
  if (options?.method?.toUpperCase() === 'POST') window.ProjectHub.changed();
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
  const expiryTime = Date.parse(preview?.expiry || '');
  const filtered = (items || []).filter((item) => Date.parse(item.expiry) === expiryTime);
  const strikes = [...new Set(filtered.map((item) => Number(item.strike)))].sort((a, b) => a - b);
  const mid = Number(preview?.btc_price) || (strikes.length ? strikes.reduce((a, b) => a + b, 0) / strikes.length : 0);
  const atmStrike = strikes.reduce((nearest, strike) => Math.abs(strike - mid) < Math.abs(nearest - mid) ? strike : nearest, strikes[0]);
  const strategyStrikes = (preview?.legs || []).map((leg) => Number(leg.strike)).filter((strike) => strikes.includes(strike));
  const nearby = strikes.slice().sort((a, b) => Math.abs(a - mid) - Math.abs(b - mid)).slice(0, 12);
  const focus = [...new Set([...nearby, ...strategyStrikes])].sort((a, b) => a - b);
  const selected = selectedMap(preview?.legs);
  $('chain').innerHTML = focus.map((strike) => {
    const call = filtered.find((item) => item.option_type === 'Call' && Number(item.strike) === strike);
    const put = filtered.find((item) => item.option_type === 'Put' && Number(item.strike) === strike);
    const selectedRow = [call, put].some((item) => item && selected.has(item.symbol));
    return `<tr class="${strike === atmStrike ? 'atm ' : ''}${selectedRow ? 'selected' : ''}">${quoteCell(call,'volume')}${quoteCell(call,'open_interest')}${deltaCell(call,'call')}${quoteCell(call,'bid_size')}${quoteCell(call,'bid','bid')}${quoteCell(call,'mark_price','mark')}${quoteCell(call,'ask','ask')}${quoteCell(call,'ask_size')}${positionCell(call,selected)}<td class="strike">${strike.toLocaleString()}</td>${positionCell(put,selected)}${quoteCell(put,'ask_size')}${quoteCell(put,'ask','ask')}${quoteCell(put,'mark_price','mark')}${quoteCell(put,'bid','bid')}${quoteCell(put,'bid_size')}${deltaCell(put,'put')}${quoteCell(put,'open_interest')}${quoteCell(put,'volume')}</tr>`;
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
  if (!canvas) return;
  const unbounded = Boolean(preview && unboundedPreview(preview));
  $('payoffRisk').hidden = !unbounded;
  $('payoffRisk').textContent = unbounded ? '亏损无上限 · 图中仅展示有限价格范围，曲线边缘不是亏损上限。' : '';
  canvas.setAttribute('aria-label', `到期盈亏曲线，非当前持仓浮动盈亏${unbounded ? '。无保护腿，亏损无上限；绘图区仅展示有限价格范围' : ''}`);
  if (!preview?.legs?.length) { canvas.width = canvas.width; $('payoffStats').textContent = '等待可用策略'; return; }
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio)); canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const width = rect.width; const height = rect.height;
  const strikes = preview.legs.map((leg) => Number(leg.strike));
  const lowStrike = Math.min(...strikes); const highStrike = Math.max(...strikes); const span = Math.max(1000, highStrike - lowStrike); const low = Math.max(0, lowStrike - span * 0.32); const high = highStrike + span * 0.32;
  const samples = Array.from({length: 121}, (_, index) => { const price = low + (high - low) * index / 120; return {price, pnl: payoffAt(price, preview)}; });
  const values = samples.map((point) => point.pnl); const min = Math.min(...values, 0); const max = Math.max(...values, 0); const pad = Math.max(50, (max - min) * 0.12); const yMin = min - pad; const yMax = max + pad; const left = 48; const right = 15; const top = unbounded ? 66 : 18; const bottom = 28;
  const x = (price) => left + (price - low) / (high - low) * (width - left - right); const y = (pnl) => top + (yMax - pnl) / (yMax - yMin) * (height - top - bottom);
  ctx.clearRect(0, 0, width, height); ctx.font = '10px Segoe UI, system-ui, sans-serif';
  for (let index = 0; index <= 4; index += 1) { const pnl = yMin + (yMax - yMin) * index / 4; const yp = y(pnl); ctx.strokeStyle = '#26352a'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, yp); ctx.lineTo(width - right, yp); ctx.stroke(); ctx.fillStyle = '#8d9e91'; ctx.fillText(`${pnl >= 0 ? '+' : ''}${Math.round(pnl).toLocaleString()}`, 5, yp + 3); }
  const zeroY = y(0); ctx.strokeStyle = '#667e6c'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(left, zeroY); ctx.lineTo(width - right, zeroY); ctx.stroke(); ctx.setLineDash([]);
  ctx.beginPath(); samples.forEach((point, index) => index ? ctx.lineTo(x(point.price), y(point.pnl)) : ctx.moveTo(x(point.price), y(point.pnl))); ctx.lineTo(x(samples[samples.length - 1].price), zeroY); ctx.lineTo(x(samples[0].price), zeroY); ctx.closePath();
  ctx.save(); ctx.clip();
  ctx.fillStyle = 'rgba(155, 205, 176, .10)'; ctx.fillRect(left, top, width - left - right, zeroY - top);
  ctx.fillStyle = 'rgba(223, 159, 153, .06)'; ctx.fillRect(left, zeroY, width - left - right, height - bottom - zeroY);
  ctx.restore();
  ctx.beginPath(); samples.forEach((point, index) => index ? ctx.lineTo(x(point.price), y(point.pnl)) : ctx.moveTo(x(point.price), y(point.pnl))); ctx.strokeStyle = '#bedcc6'; ctx.lineWidth = 2.4; ctx.stroke();
  for (const strike of strikes) { const xp = x(strike); ctx.strokeStyle = '#405e49'; ctx.setLineDash([2, 4]); ctx.beginPath(); ctx.moveTo(xp, top); ctx.lineTo(xp, height - bottom); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = '#a9bbaf'; ctx.textAlign = 'center'; ctx.fillText(`${Math.round(strike).toLocaleString()}`, xp, height - 9); }
  const spot = Number(preview.btc_price || 0); if (spot >= low && spot <= high) { const xp = x(spot); ctx.strokeStyle = '#d6bc86'; ctx.setLineDash([5, 3]); ctx.beginPath(); ctx.moveTo(xp, top); ctx.lineTo(xp, height - bottom); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = '#d6bc86'; ctx.textAlign = 'center'; ctx.fillText(`BTC ${Math.round(spot).toLocaleString()}`, xp, 11); }
  const breaks = []; for (let index = 1; index < samples.length; index += 1) { if ((samples[index - 1].pnl < 0) !== (samples[index].pnl < 0)) { const a = samples[index - 1]; const b = samples[index]; breaks.push(a.price + (0 - a.pnl) * (b.price - a.price) / (b.pnl - a.pnl)); } }
  const maxProfit = Number(preview.net_credit_usd); const current = spot ? payoffAt(spot, preview) : 0;
  $('payoffStats').innerHTML = `<div class="payoff-stat"><span>现价对应到期盈亏</span><strong class="${current >= 0 ? 'profit' : 'loss'}">${money(current)}</strong></div><div class="payoff-stat"><span>最大收益</span><strong class="profit">${Number.isFinite(maxProfit) ? money(maxProfit) : '--'}</strong></div><div class="payoff-stat"><span>最大亏损</span><strong class="loss">${unbounded ? '无上限' : optionalMoney(preview.max_loss_usd)}</strong></div><div class="payoff-stat"><span>盈亏平衡</span><strong>${breaks.length ? breaks.map((point) => Math.round(point).toLocaleString()).join(' / ') : '--'}</strong></div>`;
}
function renderLegs(legs) { $('legs').className = 'legs'; $('legs').innerHTML = (legs || []).map((leg) => `<div class="leg"><span class="leg-mark ${leg.side.toLowerCase()}">${leg.side === 'Sell' ? 'S' : 'B'} ${leg.option_type[0]}</span><div><div class="leg-title">${leg.side === 'Sell' ? '卖出' : '买入'} ${leg.option_type} · Δ ${Number(leg.delta).toFixed(3)}</div><div class="leg-symbol">${esc(leg.symbol)}</div></div><div class="leg-price"><strong>${money(leg.mark_price)}</strong><small>目标 ${Number(leg.target_delta).toFixed(2)}</small><small>预估费 ${money(leg.estimated_fee_usd)} / 封顶 ${money(leg.fee_cap_usd)}</small></div></div>`).join(''); }
function renderPositions(items) { $('positions').className = items.length ? 'positions' : 'positions empty'; $('positions').innerHTML = items.length ? items.map((p) => `<div class="position-row"><strong>${esc(p.symbol)}</strong><span>${esc(p.side)} · ${p.size}</span><span class="${p.unrealised_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${money(p.unrealised_pnl)}</span></div>`).join('') : '暂无持仓'; }
function renderLogs(items) { $('logs').innerHTML = (items || []).slice(0, 20).map((log) => `<div class="log-line"><span class="log-time">${utcTime(log.timestamp)}</span><span class="log-level ${log.level}">${log.level}</span><span>${esc(log.message)}</span></div>`).join(''); }
function renderPortfolioMargin(health) {
  let target = $('pmDetails');
  if (!target) { target = document.createElement('div'); target.id = 'pmDetails'; target.className = 'pm-details'; $('healthContent').appendChild(target); }
  if (health.margin_mode !== 'PORTFOLIO_MARGIN') { target.style.display = 'none'; return; }
  target.style.display = 'grid';
  if (!health.portfolio_margin_available) { target.innerHTML = `<div class="pm-message">真实 PM 明细不可用：${esc(health.portfolio_margin_message || '等待 Bybit 返回')}</div>`; return; }
  const incrementIm = health.pm_incremental_initial_margin_usd; const incrementMm = health.pm_incremental_maintenance_margin_usd;
  const increment = (value) => value === null || value === undefined ? '--' : `${value >= 0 ? '+' : ''}${money(value)}`;
  target.innerHTML = `<div><span>真实账户 IM</span><strong>${optionalMoney(health.pm_account_initial_margin_usd)}</strong></div><div><span>真实账户 MM</span><strong>${optionalMoney(health.pm_account_maintenance_margin_usd)}</strong></div><div><span>BTC 风险单元 IM</span><strong>${optionalMoney(health.pm_asset_initial_margin_usd)}</strong></div><div><span>BTC 风险单元 MM</span><strong>${optionalMoney(health.pm_asset_maintenance_margin_usd)}</strong></div><div><span>本次开仓 IM 增量</span><strong class="${Number(incrementIm) > 0 ? 'loss' : 'profit'}">${increment(incrementIm)}</strong></div><div><span>本次开仓 MM 增量</span><strong class="${Number(incrementMm) > 0 ? 'loss' : 'profit'}">${increment(incrementMm)}</strong></div><div><span>Contingency</span><strong>${optionalMoney(health.pm_contingency_usd)}</strong></div><div><span>最大压力场景</span><strong>${health.pm_max_loss_price_move === null || health.pm_max_loss_price_move === undefined ? '--' : `${(Number(health.pm_max_loss_price_move) * 100).toFixed(1)}%`} / IV ${health.pm_max_loss_iv_shock === null || health.pm_max_loss_iv_shock === undefined ? '--' : `${(Number(health.pm_max_loss_iv_shock) * 100).toFixed(1)}%`}</strong></div>`;
}
const isClosingExecution = (item) => item.reduce_only === true || String(item.order_link_id || '').startsWith('ic-close-');
function uniqueExecutions(items) {
  const seen = new Set();
  return (items || []).filter((item) => {
    if (!item.exec_id) return true;
    if (seen.has(item.exec_id)) return false;
    seen.add(item.exec_id);
    return true;
  });
}
function matchClosingExecutions(items) {
  const lots = new Map();
  const results = new Map();
  const time = (item) => new Date(item.exec_time).getTime();
  const openingGroup = (item) => item.execution_group || (String(item.order_link_id || '').startsWith('ic-') ? item.order_link_id.split('-')[1] : null);
  const ordered = uniqueExecutions(items).slice().sort((a, b) => time(a) - time(b) || Number(isClosingExecution(b)) - Number(isClosingExecution(a)) || String(a.exec_id || '').localeCompare(String(b.exec_id || '')));
  for (const item of ordered) {
    const closing = isClosingExecution(item);
    const qty = Number(item.exec_qty); const price = Number(item.exec_price); const fee = Number(item.exec_fee);
    if (![qty, price, fee, time(item)].every(Number.isFinite) || qty <= 0 || price < 0 || !['Buy', 'Sell'].includes(item.side)) {
      if (closing) results.set(item, null);
      continue;
    }
    const side = closing ? (item.side === 'Buy' ? 'Sell' : 'Buy') : item.side;
    const key = JSON.stringify([item.symbol, side, item.fee_currency || '']);
    const candidates = lots.get(key) || [];
    if (!closing) {
      candidates.push({item, remaining: qty});
      lots.set(key, candidates);
      continue;
    }
    let remaining = qty;
    let pnl = -fee;
    for (const lot of candidates) {
      if (remaining <= 1e-9) break;
      if (lot.remaining <= 1e-9 || (item.opening_group && openingGroup(lot.item) !== item.opening_group)) continue;
      const matchedQty = Math.min(remaining, lot.remaining);
      pnl += (item.side === 'Sell' ? 1 : -1) * (price - Number(lot.item.exec_price)) * matchedQty;
      pnl -= Number(lot.item.exec_fee) * matchedQty / Number(lot.item.exec_qty);
      lot.remaining -= matchedQty;
      remaining -= matchedQty;
    }
    results.set(item, remaining <= 1e-9 ? pnl : null);
  }
  return results;
}
function renderExecutions(items) {
  const target = $('executions');
  if (!items || !items.length) { target.className = 'executions empty'; target.textContent = '暂无成交记录'; return; }
  items = uniqueExecutions(items);
  const isClose = isClosingExecution;
  const realizedByExecution = matchClosingExecutions(items);
  const timeBucket = (item) => { const time = new Date(item.exec_time || 0).getTime(); return Number.isFinite(time) ? Math.floor(time / 15000) : 0; };
  const groups = new Map();
  for (const item of items) {
    const link = String(item.order_link_id || '');
    const parts = link.split('-');
    let key = link;
    let label = '其他成交';
    const close = isClose(item);
    if (!close && item.execution_group) { key = `open-${item.execution_group}`; label = '开仓组合'; }
    else if (parts[0] === 'ic' && parts[1] === 'close' && parts[2]) { key = parts.slice(0, 3).join('-'); label = '平仓组合'; }
    else if (close) { key = `legacy-close-${timeBucket(item)}`; label = '平仓组合'; }
    else if (parts[0] === 'ic' && parts[1]) { key = parts.slice(0, 2).join('-'); label = '开仓组合'; }
    key = JSON.stringify([key, item.fee_currency || '']);
    if (!groups.has(key)) groups.set(key, {label, items: [], fee: 0, cashflow: 0, chainDiff: 0, hasChainDiff: false, currency: item.fee_currency || ''});
    const group = groups.get(key);
    group.items.push(item);
    group.fee += Number(item.exec_fee || 0);
    group.cashflow += (item.side === 'Sell' ? 1 : -1) * Number(item.exec_price || 0) * Number(item.exec_qty || 0);
    if (item.chain_price_diff !== null && item.chain_price_diff !== undefined) { group.chainDiff += Number(item.chain_price_diff || 0); group.hasChainDiff = true; }
    if (!group.currency) group.currency = item.fee_currency || '';
  }
  target.className = 'executions';
  target.innerHTML = [...groups.values()].map((group) => {
    const legCount = new Set(group.items.map((item) => item.symbol)).size;
    let realized = 0;
    let matched = true;
    if (group.label === '平仓组合') {
      for (const close of group.items) {
        const pnl = realizedByExecution.get(close);
        if (pnl === null || pnl === undefined) matched = false;
        else realized += pnl;
      }
    }
    const resultLabel = group.label === '平仓组合' ? '组合平仓收益' : '组合成交净额';
    const resultValue = group.label === '平仓组合' ? (matched ? realized : null) : group.cashflow - group.fee;
    const resultText = resultValue === null ? `${resultLabel} 待匹配开仓成交` : `${resultLabel} ${resultValue >= 0 ? '+' : ''}${resultValue.toFixed(6)} ${esc(group.currency)}`;
    const chainText = group.label === '开仓组合' && group.hasChainDiff ? ` · 相对创建时链价差 ${group.chainDiff >= 0 ? '+' : ''}${group.chainDiff.toFixed(6)} ${esc(group.currency)}` : '';
    return `<div class="execution-group"><div class="execution-group-head"><strong>${group.label} · ${legCount} 腿</strong><span>${resultText}${chainText} · 手续费 -${group.fee.toFixed(6)} ${esc(group.currency)}</span></div>${group.items.map((item) => `<div class="execution-row"><strong>${esc(item.symbol)}</strong><span>${esc(item.side)} ${Number(item.exec_qty).toFixed(4)} · ${money(item.exec_price)}${item.chain_price_at_create !== null && item.chain_price_at_create !== undefined ? ` · 链基准 ${Number(item.chain_price_at_create).toFixed(4)}` : ''}</span><span class="exec-fee">-${Number(item.exec_fee).toFixed(6)} ${esc(item.fee_currency)}</span></div>`).join('')}</div>`;
  }).join('');
}
function tradeResultMessage(payload, action) {
  const results = payload.results || [];
  if (!results.length) return `${action}未返回订单结果，请刷新持仓核对`;
  const incomplete = results.filter((item) => !['filled', 'simulated'].includes(item.status));
  if (incomplete.length) {
    return `${action}尚未全部确认成交，请核对持仓：\n${incomplete.map((item) => `${item.symbol}：${item.status}${item.message ? `（${item.message}）` : ''}`).join('\n')}`;
  }
  return (payload.orders_submitted ?? payload.live) ? `${payload.environment === "testnet" ? "测试网" : ""}${action}订单已全部成交` : `模拟${action}已记录`;
}
function updateTradeControls() {
  const live = Boolean(window.__liveEnabled);
  const confirmed = $('confirm').checked;
  const legs = selectedStrategyMode() === 'short_strangle' ? '双腿' : '四腿';
  const openingDisabled = Boolean(window.__tradingBlocked || window.__openingBlocked || window.__strategyUnavailable) || !hasCurrentStrategyPreview() || (live && !confirmed);
  if (!$('openTrade').dataset.busy) $('openTrade').textContent = window.__marketReadOnly ? '仅供查看 · 禁止开仓' : `${live ? '确认并' : '模拟'}开仓${legs}`;
  if (!$('closeTrade').dataset.busy) $('closeTrade').textContent = `${live ? '确认并' : '模拟'}平仓已跟踪持仓`;
  if (!$('openTrade').dataset.busy) $('openTrade').disabled = openingDisabled;
  if (!$('closeTrade').dataset.busy) $('closeTrade').disabled = Boolean(window.__tradingBlocked) || (live && !confirmed);
  if (!$('rfqCreate').dataset.busy) $('rfqCreate').disabled = openingDisabled;
}
function clearStrategyDisplay(message) {
  $('marketNotice').hidden = true;
  if ($('chainPanel')) $('chainPanel').dataset.availability = 'unavailable';
  $('legs').className = 'legs strategy-empty';
  window.__latestPreview = null;
  window.__previewSelection = null;
  window.__latestChain = [];
  for (const id of ['creditValue', 'lossValue', 'marginValue', 'maintenanceValue', 'costValue', 'rrValue']) {
    if ($(id)) $(id).textContent = '--';
  }
  $('marginSub').textContent = '等待可用策略';
  $('feeSub').textContent = '等待可用策略';
  renderChain([], null);
  $('chain').innerHTML = `<tr><td colspan="19" class="empty-cell">${esc(message)}</td></tr>`;
  $('chainCount').textContent = message;
  $('legs').textContent = message;
  $('expiry').textContent = '周日到期';
  renderPayoff(null);
}
async function loadMarket() {
  const read = window.ProjectHub.begin('market');
  if (!read) return;
  const requestedQty = Number($('quantity')?.value);
  const requestedMode = selectedStrategyMode();
  const current = () => read.current() && requestedMode === selectedStrategyMode() && requestedQty === Number($('quantity').value);
  try {
    if (!Number.isFinite(requestedQty) || requestedQty <= 0) throw new Error('请输入有效的每腿数量。');
    const marketUrl = `/api/dashboard/market?quantity=${encodeURIComponent(requestedQty)}&strategy_mode=${encodeURIComponent(requestedMode)}`;
    const payload = await getJson(marketUrl);
    if (!current()) return;
    const {config, preview, chain} = payload;
    window.__strategyConfig = config;
    if (!strategySelectionInitialized) {
      strategySelectionInitialized = true;
      if (config.strategy_mode === 'short_strangle' && requestedMode !== config.strategy_mode) {
        $('strategyMode').value = config.strategy_mode;
        invalidateStrategySelection('正在读取默认开仓结构');
        void loadMarket();
        return;
      }
    }
    renderStrategySelection();
    if (preview && previewStrategyMode(preview) !== requestedMode) throw new Error('返回的预览与所选开仓结构不一致，请重新刷新。');
    window.__positionMarket = {price: Number(chain.btc_price || preview?.btc_price || 0), timestamp: Date.parse(chain.updated_at || preview?.market_timestamp || ''), staleSeconds: Number(config.quote_stale_seconds || 30)};
    renderPositionPayoff();
    window.__liveEnabled = config.trading_enabled ?? config.live_enabled;
    window.__openingBlocked = Boolean(config.opening_blocked_reason);
    const enabled = window.__liveEnabled;
    $('environment').dataset.mode = config.environment || (enabled ? 'live' : 'dry-run');
    if ($('modeBox')) $('modeBox').dataset.mode = config.environment || (enabled ? 'live' : 'dry-run');
    const modeLabel = config.environment === "testnet" ? "TESTNET" : (enabled ? "LIVE" : "MAINNET DATA / DRY");
    window.__tradingBlocked = Boolean(config.trading_blocked_reason);
    window.__marketReadOnly = payload.read_only === true || payload.status === 'waiting_for_listing';
    window.__strategyUnavailable = !preview || window.__marketReadOnly;
    $('marketNotice').hidden = !window.__marketReadOnly;
    $('marketNotice').textContent = payload.message || '备用盘口仅供查看，禁止开仓和询价。';
    $('chainSource').textContent = chain.source.toUpperCase();
    $('refreshRate').textContent = config.market_refresh_seconds;
    $('nextOpen').textContent = config.open_time;
    $('modeTitle').textContent = enabled ? (config.environment === 'testnet' ? '测试网模式已启用' : '实盘模式已启用') : '模拟模式';
    $('modeText').textContent = enabled ? (config.environment === 'testnet' ? '确认后将向 Bybit 测试网发送订单。' : '确认后将向 Bybit 主网发送 BBO 限价订单。') : '不会向交易所发送订单。';
    updateTradeControls();
    if (payload.status === 'waiting_for_listing') {
      clearStrategyDisplay('等待周日到期合约上线');
      if (chain.expiry && chain.items?.length) {
        $('chainPanel').dataset.availability = 'ready';
        window.__latestChain = chain.items;
        renderChain(chain.items, {expiry: chain.expiry, btc_price: chain.btc_price, legs: []});
        $('expiry').textContent = `仅查看 · ${new Date(chain.expiry).toLocaleDateString('zh-CN', {month: '2-digit', day: '2-digit', timeZone: 'UTC'})}`;
        $('legSummary').textContent = 'VIEW ONLY';
        $('marketNotice').textContent = payload.message;
        $('marketNotice').hidden = false;
        $('modeTitle').textContent = '备用盘口 · 仅供查看';
        $('modeText').textContent = '周日合约上线后自动恢复策略。备用合约不可开仓或询价，已有持仓仍可按原规则平仓。';
      }
      $('btcPrice').textContent = chain.btc_price ? money(chain.btc_price) : '--';
      $('environment').textContent = modeLabel;
      $('statusValue').dataset.state = config.trading_blocked_reason ? 'error' : 'waiting';
      $('statusValue').textContent = config.trading_blocked_reason ? '交易已阻止' : (chain.expiry && chain.items?.length ? '备用盘口 · 只读' : '等待合约上线');
      $('statusSub').textContent = config.trading_blocked_reason || payload.message;
      $('updateText').textContent = '行情已连接 · 自动检查合约';
      $('updateDot').style.background = '#d6bc86';
      return;
    }
    if ($('chainPanel')) $('chainPanel').dataset.availability = 'ready';
    window.__latestPreview = preview;
    window.__previewSelection = {mode: requestedMode, quantity: requestedQty};
    window.__latestChain = chain.items || []; $('btcPrice').textContent = preview.btc_price ? money(preview.btc_price) : '--';
    $('environment').textContent = chain.source === 'bybit' ? (modeLabel) : 'UNAVAILABLE';
    const unbounded = unboundedPreview(preview);
    const portfolioEstimate = !unbounded && (preview.margin_basis === 'portfolio_loss_estimate' || (!preview.margin_basis && preview.margin_mode === 'PORTFOLIO_MARGIN'));
    $('creditValue').textContent = optionalMoney(preview.net_credit_usd); $('lossValue').textContent = unbounded ? '无上限' : optionalMoney(preview.max_loss_usd); $('marginValue').textContent = optionalMoney(preview.estimated_margin_usd); $('marginSub').textContent = portfolioEstimate ? 'PM 压力测试估算' : unbounded ? '常规保证金估算（含缓冲）' : 'Bybit Order IM'; $('maintenanceValue').textContent = optionalMoney(preview.estimated_maintenance_margin_usd); $('costValue').textContent = optionalMoney(preview.estimated_trading_cost_usd); $('feeSub').textContent = `Taker ${(Number(preview.estimated_fee_rate) * 100).toFixed(3)}% · 单腿上限 ${(Number(preview.fee_cap_pct) * 100).toFixed(0)}%`; if ($('rrValue')) $('rrValue').textContent = preview.risk_reward == null ? '--' : `${preview.risk_reward}x`;
    updateTradeControls();
    const quoteTime = preview.market_timestamp ? new Date(preview.market_timestamp) : new Date(); const age = Math.max(0, Math.round((Date.now() - quoteTime.getTime()) / 1000));
    $('statusValue').dataset.state = age > config.quote_stale_seconds ? 'waiting' : 'ready';
    $('statusValue').textContent = age > config.quote_stale_seconds ? '行情过期' : '策略就绪'; $('statusSub').textContent = `${chain.source === 'bybit' ? (config.market_testnet ? 'Bybit 测试网行情' : 'Bybit 主网行情') : '行情不可用'} · ${age}s 前`; $('expiry').textContent = `到期 ${new Date(preview.expiry).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit',timeZone:'UTC'})}`; renderChain(chain.items, preview); renderLegs(preview.legs); renderPayoff(preview); $('updateText').textContent = `行情 ${age}s · 每 ${config.market_refresh_seconds}s 更新`; $('updateDot').style.background = age > config.quote_stale_seconds ? '#df9f99' : '#bedcc6';
    if (config.trading_blocked_reason || config.opening_blocked_reason) { $('statusValue').dataset.state = 'error'; $('statusValue').textContent = '交易已阻止'; $('statusSub').textContent = config.trading_blocked_reason || 'RFQ 状态待确认，后台正在对账'; }
  } catch (error) { if (!current()) return; window.__positionMarket = null; renderPositionPayoff(); window.__strategyUnavailable = true; clearStrategyDisplay('策略暂不可用'); updateTradeControls(); $('statusValue').dataset.state = 'error'; $('statusValue').textContent = '行情异常'; $('statusSub').textContent = error.message; $('updateText').textContent = '等待重试'; $('updateDot').style.background = '#df9f99'; }
  finally { read.finish(); }
}
let ordersSnapshot = null;
let ordersReceivedAt = null;
let ordersReadFailed = false;

function orderNumber(value, price = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '--';
  return value.toLocaleString('en-US', {minimumFractionDigits: price ? 2 : 0, maximumFractionDigits: 8});
}
function orderTime(value) {
  if (typeof value !== 'string' || !value || !Number.isFinite(Date.parse(value))) return '--';
  return new Date(value).toISOString().slice(5, 19).replace('T', ' ');
}
function orderDisplayState(item) {
  if (item.terminal === true) {
    if (item.exchange_status === 'Rejected' || item.status === 'rejected') return {label: '已拒绝', tone: 'error'};
    if (['Cancelled', 'Canceled', 'PartiallyFilledCanceled', 'Deactivated'].includes(item.exchange_status)
        || item.status === 'timeout_cancelled') return {label: '已撤单', tone: 'ended'};
    if (item.status === 'not_submitted') return {label: '未提交', tone: 'ended'};
    const full = Number.isFinite(item.qty) && item.qty > 0 && Number.isFinite(item.filled_qty)
      && Math.abs(item.filled_qty - item.qty) <= 1e-9 && Number.isFinite(item.remaining_qty) && item.remaining_qty <= 1e-9;
    if (item.status === 'filled' && full) return {label: '已全部成交', tone: 'filled'};
    if (item.filled_qty > 0) return {label: '部分成交 · 已结束', tone: 'warning'};
    return {label: item.status === 'error' ? '执行失败' : '已结束', tone: item.status === 'error' ? 'error' : 'ended'};
  }
  const phases = {submitting: '正在提交', amending: '正在改价', cancelling: '正在撤单',
    reconciling: '正在核对', working: '挂单中'};
  const label = typeof phases[item.phase] === 'string' ? phases[item.phase] : null;
  return {label: label || '状态未知 · 待核对', tone: label ? 'working' : 'warning'};
}
function orderRow(item) {
  const state = orderDisplayState(item);
  const operation = item.operation === 'open' ? '开仓' : item.operation === 'close' ? '平仓' : '--';
  const executionType = ['BBO', 'IOC'].includes(item.execution_type) ? item.execution_type : '--';
  const side = item.side === 'Buy' ? '买入' : item.side === 'Sell' ? '卖出' : '--';
  const sideClass = item.side === 'Buy' ? 'buy' : item.side === 'Sell' ? 'sell' : 'unknown';
  const confirmation = item.stale === true && item.terminal !== true
    ? `<span class="order-stale">${orderTime(item.last_confirmed_at) === '--' ? '尚无交易所确认 · 待核对' : '交易所确认已过期 · 待核对'}</span>` : '';
  return `<tr role="row" data-order-link="${esc(item.order_link_id)}">
    <td role="cell" data-label="合约 / 订单标识" class="order-contract"><strong>${esc(item.symbol || '--')}</strong><small>关联 ${esc(item.order_link_id || '--')}</small><small>交易所 ${esc(item.order_id || '--')}</small></td>
    <td role="cell" data-label="执行"><div class="order-operation">${operation} <span>${executionType}</span></div><span class="order-side ${sideClass}">${side}</span></td>
    <td role="cell" data-label="价格 · USD"><div class="order-values"><span><small>申请</small><b>${orderNumber(item.requested_price, true)}</b></span><span><small>已确认</small><b>${orderNumber(item.confirmed_price, true)}</b></span></div></td>
    <td role="cell" data-label="数量 · BTC"><div class="order-values"><span><small>委托</small><b>${orderNumber(item.qty)}</b></span><span><small>累计成交</small><b>${orderNumber(item.filled_qty)}</b></span><span><small>未成交</small><b>${orderNumber(item.remaining_qty)}</b></span></div></td>
    <td role="cell" data-label="状态阶段"><span class="order-phase" data-tone="${state.tone}">${state.label}</span><small class="order-exchange-status">${item.exchange_status ? `交易所 ${esc(item.exchange_status)}` : `本地 ${esc(item.status || '--')}`}</small>${confirmation}</td>
    <td role="cell" data-label="时间 · UTC"><div class="order-values order-times"><span><small>更新</small><b>${orderTime(item.updated_at)}</b></span><span><small>确认</small><b>${orderTime(item.last_confirmed_at)}</b></span><span><small>创建</small><b>${orderTime(item.created_at)}</b></span></div></td>
  </tr>`;
}
function renderOrdersFreshness() {
  const offline = window.navigator.onLine === false;
  const expired = ordersReceivedAt !== null && Date.now() - ordersReceivedAt > 45000;
  const stale = ordersReadFailed || expired;
  const hasSnapshot = ordersSnapshot !== null;
  $('orders').dataset.state = offline ? 'offline' : stale ? 'stale' : hasSnapshot ? 'ready' : 'loading';
  const label = offline ? '离线 · 同步暂停' : stale ? '数据已过期' : !hasSnapshot ? '等待同步'
    : ordersSnapshot.execution_active ? '执行中 · 持续同步' : '每秒同步';
  if ($('ordersSyncState').textContent !== label) $('ordersSyncState').textContent = label;
  let notice = '';
  if (offline) notice = hasSnapshot ? '网络已断开，保留最近委托记录；联网后自动补读。' : '网络已断开；联网后自动读取委托记录。';
  else if (ordersReadFailed) notice = hasSnapshot ? '挂单更新失败，以下为最近一次记录，数据已过期；正在自动重试。' : '挂单暂不可用，正在自动重试。';
  else if (expired) notice = '挂单快照已过期，以下为最近一次记录；等待同步恢复。';
  // Only connection-state transitions are announced; prices and timestamps are not live regions.
  if ($('ordersNotice').textContent !== notice) $('ordersNotice').textContent = notice;
  $('ordersNotice').hidden = !notice;
  if (!hasSnapshot) $('ordersEmpty').textContent = stale || offline ? '尚未取得委托记录' : '等待挂单同步…';
  else if (!ordersSnapshot.items.length) {
    const busy = $('openTrade').dataset.busy || $('closeTrade').dataset.busy;
    $('ordersEmpty').textContent = busy && !stale && !offline ? '正在等待首笔委托，执行期间持续同步…' : '暂无本系统跟踪的单腿委托';
  }
}
function renderOrders(payload) {
  if (!payload || !Array.isArray(payload.items) || payload.items.some(item => !item || typeof item !== 'object' || Array.isArray(item))) {
    throw new Error('Invalid order snapshot');
  }
  const rows = payload.items.map(orderRow).join('');
  const active = payload.items.filter(item => item.terminal !== true).length;
  const terminal = payload.items.length - active;
  ordersSnapshot = payload;
  ordersReceivedAt = Date.now();
  ordersReadFailed = false;
  $('ordersSummary').textContent = `未结束 ${active} 笔 · 最近结束 ${terminal} 笔`;
  $('ordersSnapshotTime').textContent = `服务器快照 ${orderTime(payload.generated_at)} UTC`;
  $('ordersEmpty').hidden = payload.items.length > 0;
  $('ordersTableWrap').hidden = payload.items.length === 0;
  // A clock-only snapshot must not replace rows or disturb the user's reading position.
  if ($('ordersBody').innerHTML !== rows) $('ordersBody').innerHTML = rows;
  renderOrdersFreshness();
}
async function loadOrders() {
  const read = window.ProjectHub.begin('orders');
  if (!read) return;
  try {
    const payload = await getJson('/api/dashboard/orders');
    if (read.current()) renderOrders(payload);
  } catch (_) {
    if (!read.current()) return;
    ordersReadFailed = true;
    renderOrdersFreshness();
  } finally { read.finish(); }
}

async function loadAccount() {
  const read = window.ProjectHub.begin('account');
  if (!read) return;
  try {
    const payload = await getJson('/api/dashboard/account');
    if (!read.current()) return;
    const {positions, logs, health, executions} = payload;
    window.__positionSnapshot = positions.items || [];
    window.__positionError = positions.available === false;
    renderPositionPayoff();
    renderPositions(positions.items || []); renderLogs(logs.items || []); renderExecutions(executions.items || []);
    window.__latestExecutions = executions.items || [];
    $('healthMode').textContent = health.available ? marginLabel(health.margin_mode) : '未连接账户'; $('healthUnavailable').textContent = health.message || '连接账户后，将显示余额、保证金和风险指标。'; $('healthUnavailable').style.display = health.available ? 'none' : 'block'; $('healthContent').style.display = health.available ? 'grid' : 'none'; if (health.available) { const im = Number(health.initial_margin_rate || 0) * 100; const mm = Number(health.maintenance_margin_rate || 0) * 100; $('imRate').textContent = `${im.toFixed(2)}%`; $('mmRate').textContent = `${mm.toFixed(2)}%`; $('imBar').style.width = `${Math.min(100, im)}%`; $('mmBar').style.width = `${Math.min(100, mm)}%`; $('availableBalance').textContent = money(health.available_balance_usd); $('marginBalance').textContent = money(health.margin_balance_usd); $('totalEquity').textContent = money(health.total_equity_usd); $('accountMode').textContent = marginLabel(health.margin_mode); renderPortfolioMargin(health); }
  } catch (error) { if (!read.current()) return; window.__positionError = true; renderPositionPayoff(); $('healthMode').textContent = '账户连接异常'; $('healthUnavailable').textContent = error.message; $('healthUnavailable').style.display = 'block'; $('healthContent').style.display = 'none'; }
  finally { read.finish(); }
}
async function load() { await Promise.allSettled([loadMarket(), loadAccount()]); }
async function openTrade() {
  const button = $('openTrade');
  if (button.dataset.busy) return;
  try {
    const selection = openingSelection();
    button.dataset.busy = '1'; button.disabled = true; button.textContent = '执行中…';
    const result = await getJson('/api/trading/open', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(selection)});
    showTradeResult(result, `${strategyName(selection.strategy_mode)}开仓`);
    await load();
  } catch (error) { showNotice(error.message, '操作未完成', 'error'); }
  finally { delete button.dataset.busy; updateTradeControls(); }
}
function tick() { $('clock').textContent = `${new Date().toLocaleTimeString('en-GB',{hour12:false,timeZone:'UTC'})} UTC`; renderOrdersFreshness(); }
setInterval(renderPositionPayoff, 10000);
$('refresh').addEventListener('click', async () => { try { await getJson('/api/market/refresh',{method:'POST'}); await loadMarket(); } catch (error) { $('statusSub').textContent = error.message; } }); $('reloadPositions').addEventListener('click', loadAccount); $('openTrade').addEventListener('click', openTrade); $('confirm').addEventListener('change', updateTradeControls); tick(); setInterval(tick,1000);
let payoffResizeFrame; window.addEventListener('resize', () => { cancelAnimationFrame(payoffResizeFrame); payoffResizeFrame = requestAnimationFrame(() => { if (window.__latestPreview) renderPayoff(window.__latestPreview); renderPositionPayoff(); }); });
window.__desiredQty = window.localStorage.getItem('ic-quantity') || '';
const quantityField = $('quantity');
const quantityPreset = $('quantityPreset');
const syncQuantityPreset = () => { const value = String(quantityField.value); const option = [...quantityPreset.options].find((item) => item.value === value); quantityPreset.value = option ? value : 'custom'; };
if (window.__desiredQty) quantityField.value = window.__desiredQty;
syncQuantityPreset();
quantityField.addEventListener('input', () => {
  window.__desiredQty = quantityField.value;
  window.localStorage.setItem('ic-quantity', quantityField.value);
  syncQuantityPreset();
  invalidateStrategySelection('数量已变更，等待重新预览');
});
const refreshEstimate = () => { const value = Number(quantityField.value); if (value > 0) { window.__desiredQty = String(value); window.localStorage.setItem('ic-quantity', String(value)); invalidateStrategySelection('正在重算当前数量'); void loadMarket(); } };
quantityPreset.addEventListener('change', () => { if (quantityPreset.value !== 'custom') { quantityField.value = quantityPreset.value; refreshEstimate(); } });
quantityField.addEventListener('change', refreshEstimate);
function changeStrategyMode() {
  strategySelectionInitialized = true;
  invalidateStrategySelection('正在读取所选开仓结构');
  void loadMarket();
}
$('strategyMode').addEventListener('change', changeStrategyMode);
renderStrategySelection();
async function closeTrade() { const button = $('closeTrade'); button.dataset.busy = '1'; button.disabled = true; button.textContent = '执行中…'; try { const result = await getJson('/api/trading/close', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked})}); showTradeResult(result, '平仓'); await load(); } catch (error) { showNotice(error.message, '操作未完成', 'error'); } finally { delete button.dataset.busy; updateTradeControls(); } }
$('closeTrade').addEventListener('click', closeTrade);
function quoteNet(quote, side, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg.side])); return (quote[side === 'Buy' ? 'quoteBuyList' : 'quoteSellList'] || []).reduce((sum, item) => { const requestedSide = requested.get(item.symbol) || 'Buy'; const takerSide = side === 'Sell' ? requestedSide : (requestedSide === 'Buy' ? 'Sell' : 'Buy'); return sum + (takerSide === 'Sell' ? 1 : -1) * Number(item.price || 0) * Number(item.qty || 0); }, 0); }
function quoteLegCount(quote) { return new Set((quote.quoteSellList || []).map((item) => item.symbol)).size; }
function quoteLegComparison(quote, side, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg])); const chain = new Map((window.__latestChain || []).map((item) => [item.symbol, item])); return (quote[side === 'Buy' ? 'quoteBuyList' : 'quoteSellList'] || []).map((item) => { const leg = requested.get(item.symbol) || {}; const takerSide = side === 'Sell' ? (leg.side || 'Buy') : ((leg.side || 'Buy') === 'Buy' ? 'Sell' : 'Buy'); const market = chain.get(item.symbol) || {}; const reference = Number(takerSide === 'Sell' ? market.bid : market.ask) || Number(market.mark_price) || 0; const quoted = Number(item.price || 0); const qty = Number(item.qty || leg.qty || 0); const diff = (takerSide === 'Sell' ? 1 : -1) * (quoted - reference) * qty; return `<div class="rfq-leg-row"><strong>${esc(item.symbol)}</strong><span>${takerSide} ${quoted.toFixed(4)} × ${qty}</span><span>链 ${reference.toFixed(4)}</span><b class="${diff >= 0 ? 'rfq-diff-positive' : 'rfq-diff-negative'}">差额 ${diff >= 0 ? '+' : ''}${diff.toFixed(4)}</b></div>`; }).join(''); }
const rfqMeta = $('rfqId')?.parentElement; if (rfqMeta && !$('rfqType')) { const type = document.createElement('span'); type.innerHTML = '类型 <b id="rfqType">--</b>'; rfqMeta.appendChild(type); }
function quoteChainNet(quote, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg])); const chain = new Map((window.__latestChain || []).map((item) => [item.symbol, item])); return (quote.quoteSellList || []).reduce((sum, item) => { const leg = requested.get(item.symbol) || {}; const market = chain.get(item.symbol) || {}; const price = Number((leg.side || 'Buy') === 'Sell' ? market.bid : market.ask) || Number(market.mark_price) || 0; return sum + ((leg.side || 'Buy') === 'Sell' ? 1 : -1) * price * Number(item.qty || leg.qty || 0); }, 0); }
function quoteNetDiff(quote, state) { return quoteNet(quote, 'Sell', state) - quoteChainNet(quote, state); }
function rfqFeeEstimate(quote) { const index = Number(window.__latestPreview?.btc_price || 0); const effectiveRate = Math.max(0.0003 * 0.5, 0.0003); return (quote.quoteSellList || []).reduce((sum, item) => { const price = Number(item.price || 0); const qty = Number(item.qty || 0); return sum + Math.min(effectiveRate * index, 0.07 * price) * qty; }, 0); }
function quoteSpread(quote, state) { const requested = new Map((state.legs || []).map((leg) => [leg.symbol, leg])); const chain = new Map((window.__latestChain || []).map((item) => [item.symbol, item])); return (quote.quoteSellList || []).reduce((sum, item) => { const leg = requested.get(item.symbol) || {}; const market = chain.get(item.symbol) || {}; const reference = Number((leg.side || 'Buy') === 'Sell' ? market.bid : market.ask) || Number(market.mark_price) || 0; return sum + Math.abs(Number(item.price || 0) - reference); }, 0); }
function rfqStrategyLabel(state) {
  if (state.strategy_mode === 'short_strangle') return '双腿卖出 · 无保护腿，亏损无上限';
  if (state.strategy_mode === 'iron_condor') return '四腿铁鹰';
  return `${(state.legs || []).length} 腿组合`;
}
function renderRfq(state) {
  if (!state || !state.rfq_id || ['Canceled', 'Expired', 'Filled', 'Failed'].includes(state.status)) { $('rfqStatus').textContent = state?.status || '未创建'; $('rfqId').textContent = '--'; $('rfqType').textContent = '--'; $('rfqExpires').textContent = '--'; $('rfqQuoteCount').textContent = '0'; $('rfqQuotes').className = 'rfq-quotes empty'; $('rfqQuotes').textContent = '暂无活动 RFQ'; return; }
  $('rfqStatus').textContent = state.status || '--'; $('rfqId').textContent = state.rfq_id; $('rfqType').textContent = rfqStrategyLabel(state); $('rfqExpires').textContent = state.expires_at ? new Date(Number(state.expires_at)).toLocaleTimeString('zh-CN') : '--'; const quotes = (state.quotes || []).slice().sort((a, b) => { const aCount = quoteLegCount(a); const bCount = quoteLegCount(b); const total = (state.legs || []).length; if ((aCount >= total) !== (bCount >= total)) return aCount >= total ? -1 : 1; if ((aCount > 0) !== (bCount > 0)) return aCount > 0 ? -1 : 1; return quoteNet(b, 'Sell', state) - quoteNet(a, 'Sell', state); }); $('rfqQuoteCount').textContent = String(quotes.length);
  $('rfqQuotes').className = quotes.length ? 'rfq-quotes' : 'rfq-quotes empty'; $('rfqQuotes').innerHTML = quotes.length ? quotes.map((quote) => { const legCount = quoteLegCount(quote); const complete = legCount >= (state.legs || []).length; const executable = complete && state.status === 'Active' && !state.selected_quote_id; const disabled = executable ? '' : ' disabled title="报价不完整或询价已经提交/结束"'; const netDiff = quoteNetDiff(quote, state); return `<div class="rfq-quote"><div><strong>${esc(quote.deskCode || '做市商')} · ${legCount}/${(state.legs || []).length} 腿</strong><span>${esc(quote.status || '--')} · 到期 ${quote.expiresAt ? new Date(Number(quote.expiresAt)).toLocaleTimeString('zh-CN') : '--'}</span></div><div class="rfq-quote-values"><span>Sell 净额 ${quoteNet(quote, 'Sell', state).toFixed(4)}</span><span class="${netDiff >= 0 ? 'rfq-diff-positive' : 'rfq-diff-negative'}">链净额差 ${netDiff >= 0 ? '+' : ''}${netDiff.toFixed(4)}</span><span class="rfq-fee">预估手续费 ${rfqFeeEstimate(quote).toFixed(6)} USDT <small>预估费率0.03% · 单腿上限7%</small></span><button class="button ghost rfq-execute" data-rfq="${esc(quote.rfqId || state.rfq_id)}" data-quote="${esc(quote.quoteId || '')}" data-side="Sell"${disabled}>执行 Sell</button></div><div class="rfq-compare-title">Sell 报价方向 · 本次询价 ${(state.legs || []).length} 腿成交方向（Sell 用 Bid1，Buy 用 Ask1）</div><div class="rfq-leg-compare">${quoteLegComparison(quote, 'Sell', state) || '<span>无 Sell 方向报价</span>'}</div></div>`; }).join('') : '等待做市商报价';
  document.querySelectorAll('.rfq-execute').forEach((button) => button.addEventListener('click', executeRfq));
}
async function loadRfq() { const read = window.ProjectHub.begin('rfq'); if (!read) return; try { const payload = await getJson('/api/rfq/status'); if (read.current()) renderRfq(payload); } catch (error) { if (read.current()) $('rfqStatus').textContent = error.message; } finally { read.finish(); } }
async function createRfq() {
  const button = $('rfqCreate');
  if (button.dataset.busy) return;
  try {
    const selection = openingSelection();
    button.dataset.busy = '1'; button.disabled = true;
    await getJson('/api/rfq/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...selection, counterparties:[]})});
    await loadRfq();
  } catch (error) { showNotice(error.message, '操作未完成', 'error'); }
  finally { delete button.dataset.busy; updateTradeControls(); }
}
async function executeRfq(event) { const button = event.currentTarget; try { await getJson('/api/rfq/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked, rfq_id:button.dataset.rfq, quote_id:button.dataset.quote, quote_side:button.dataset.side})}); await loadRfq(); } catch (error) { showNotice(error.message, '操作未完成', 'error'); } }
async function cancelRfq() { try { const state = await getJson('/api/rfq/status?refresh=false'); if (!state.rfq_id) throw new Error('没有活动 RFQ'); await getJson('/api/rfq/cancel', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm_live:$('confirm').checked, rfq_id:state.rfq_id})}); await loadRfq(); } catch (error) { showNotice(error.message, '操作未完成', 'error'); } }
$('rfqCreate').addEventListener('click', createRfq); $('rfqCancel').addEventListener('click', cancelRfq);
$('reloadPerformance').addEventListener('click', loadPerformance);
$('performanceCurrency').addEventListener('change', () => { if (window.__performance) renderPerformance(window.__performance); });
window.addEventListener('resize', () => { if (window.__performance) renderPerformance(window.__performance); });
window.ProjectHub.register('market', 10000, loadMarket);
window.ProjectHub.register('account', 15000, loadAccount);
window.ProjectHub.register('orders', 1000, loadOrders);
window.ProjectHub.register('rfq', 3000, loadRfq);
window.ProjectHub.register('performance', 30000, loadPerformance);
window.ProjectHub.start();
