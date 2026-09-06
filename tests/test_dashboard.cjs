const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function dashboard() {
  const elements = new Map();
  const pending = [];
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'quantity' ? '0.01' : '', dataset: {}, style: {},
      options: [], addEventListener() {}, closest() { return null; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {getElementById: element, querySelector() { return null; }},
    window: {localStorage: {getItem() { return null; }}, addEventListener() {}},
    setInterval() {},
    fetch(url) {
      if (!url.includes('/dashboard/market')) return Promise.reject(new Error('offline'));
      return new Promise((resolve) => pending.push({url, resolve}));
    },
  });
  vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/position-payoff.js`, 'utf8'), context);
  vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/app.js`, 'utf8'), context);
  return {context, element, pending};
}

test('partial and failed orders are not reported as successful', () => {
  const {context} = dashboard();
  const message = context.tradeResultMessage({live: true, results: [
    {symbol: 'A', status: 'filled'}, {symbol: 'B', status: 'partial'},
    {symbol: 'C', status: 'error', message: 'timeout'},
  ]}, '开仓');
  assert.match(message, /尚未全部确认成交/);
  assert.match(message, /B：partial/);
  assert.match(message, /C：error（timeout）/);
  assert.doesNotMatch(message, /订单已全部成交/);
  assert.match(context.tradeResultMessage({live: true, results: [{status: 'filled'}]}, '平仓'), /订单已全部成交/);
});

test('polling preserves busy trade button text', () => {
  const {context, element} = dashboard();
  const button = element('openTrade');
  button.dataset.busy = '1';
  button.disabled = true;
  button.textContent = '执行中…';
  context.updateTradeControls();
  assert.equal(button.textContent, '执行中…');
  assert.equal(button.disabled, true);
});

test('state failure disables both trading buttons even with confirmation', () => {
  const {context, element} = dashboard();
  context.window.__liveEnabled = true;
  context.window.__tradingBlocked = true;
  element('confirm').checked = true;
  context.updateTradeControls();
  assert.equal(element('openTrade').disabled, true);
  assert.equal(element('closeTrade').disabled, true);
});

test('quantity changes during a request coalesce into a fresh request', async () => {
  const {context, element, pending} = dashboard();
  assert.equal(pending.length, 1);
  assert.match(pending[0].url, /quantity=0.01/);
  element('quantity').value = '0.02';
  await context.loadMarket();
  element('quantity').value = '0.03';
  await context.loadMarket();
  assert.equal(pending.length, 1);
  pending[0].resolve({ok: false, text: async () => JSON.stringify({detail: 'offline'})});
  await new Promise(setImmediate);
  assert.equal(pending.length, 2);
  assert.match(pending[1].url, /quantity=0.03/);
});

test('in-page trade feedback preserves incomplete fills and error detail', () => {
  const {context, element} = dashboard();
  context.showTradeResult({live: true, results: [
    {symbol: 'A', status: 'filled'}, {symbol: 'B', status: 'unknown', message: '等待交易所确认'},
  ]}, '开仓');
  assert.equal(element('notice').hidden, false);
  assert.equal(element('notice').dataset.tone, 'error');
  assert.match(element('noticeText').textContent, /尚未全部确认成交/);
  assert.match(element('noticeText').textContent, /B：unknown（等待交易所确认）/);
});

test('account connection failure hides stale health metrics', async () => {
  const {context, element} = dashboard();
  element('healthUnavailable').style.display = 'none';
  element('healthContent').style.display = 'grid';
  await context.loadAccount();
  assert.equal(element('healthUnavailable').style.display, 'block');
  assert.equal(element('healthContent').style.display, 'none');
  assert.equal(element('healthMode').textContent, '账户连接异常');
  assert.match(element('positionPayoffContent').innerHTML, /持仓更新失败/);
});

test('position chart preserves payoff but removes stale spot, and clears closed positions', () => {
  const {context, element} = dashboard();
  context.window.__positionSnapshot = [{symbol:'BTC-13Sep99-100000-C-USDT',side:'Buy',size:.1,avg_price:100,unrealised_pnl:3}];
  context.window.__positionMarket = {price:102000,timestamp:Date.now(),staleSeconds:30};
  context.renderPositionPayoff();
  assert.match(element('positionPayoffContent').innerHTML, /pp-spot-dot/);
  assert.match(element('positionPayoffContent').innerHTML, /到期盈利区/);
  assert.doesNotMatch(element('positionPayoffContent').innerHTML, /NaN|Infinity/);
  context.window.__positionMarket.timestamp -= 60000;
  context.renderPositionPayoff();
  assert.doesNotMatch(element('positionPayoffContent').innerHTML, /pp-spot-dot/);
  assert.match(element('positionPayoffContent').innerHTML, /等待现价/);
  assert.match(element('positionPayoffContent').innerHTML, /pp-line/);
  context.window.__positionError = true;
  context.renderPositionPayoff();
  assert.doesNotMatch(element('positionPayoffContent').innerHTML, /pp-line/);
  context.window.__positionError = false;
  context.window.__positionSnapshot = [];
  context.renderPositionPayoff();
  assert.match(element('positionPayoffContent').innerHTML, /暂无可计算/);
  assert.doesNotMatch(element('positionPayoffContent').innerHTML, /pp-line/);
});

test('testnet mode requires confirmation and reports exchange fills as testnet', async () => {
  const {context, element, pending} = dashboard();
  const payload = marketPayload(false);
  Object.assign(payload.config, {environment: 'testnet', live_enabled: false, trading_enabled: true, market_testnet: true});
  pending[0].resolve({ok: true, text: async () => JSON.stringify(payload)});
  await new Promise(setImmediate);
  assert.equal(element('modeTitle').textContent, '测试网模式已启用');
  assert.equal(element('environment').textContent, 'TESTNET');
  assert.match(element('statusSub').textContent, /测试网行情/);
  assert.equal(element('openTrade').disabled, true);
  element('confirm').checked = true;
  context.updateTradeControls();
  assert.equal(element('openTrade').disabled, false);
  assert.match(context.tradeResultMessage({live: false, orders_submitted: true, environment: 'testnet', results: [{status: 'filled'}]}, '开仓'), /测试网开仓订单已全部成交/);
});

test('unresolved RFQ disables ordinary opening and new inquiries', async () => {
  const {context, element, pending} = dashboard();
  const payload = marketPayload(false);
  payload.config.opening_blocked_reason = 'Unresolved RFQ';
  element('confirm').checked = true;
  pending[0].resolve({ok: true, text: async () => JSON.stringify(payload)});
  await new Promise(setImmediate);
  assert.equal(element('openTrade').disabled, true);
  assert.equal(element('rfqCreate').disabled, true);
});

function execution(id, second, side, qty, price, fee, close = false, extra = {}) {
  return {exec_id: id, exec_time: `2026-09-04T12:00:${String(second).padStart(2, '0')}Z`,
    symbol: 'BTC-OPTION', side, exec_qty: qty, exec_price: price, exec_fee: fee,
    fee_currency: 'USDT', reduce_only: close, order_link_id: `ic-${close ? 'close-' : ''}${id}-0`, ...extra};
}

function marketPayload(waiting) {
  return {
    status: waiting ? 'waiting_for_listing' : 'ready',
    message: '周日到期合约尚未上线，系统将自动检查；上线后生成四腿策略。',
    config: {live_enabled: true, market_refresh_seconds: 10, quote_stale_seconds: 30, max_risk_usd: 2500, open_time: 'Friday 21:00 UTC'},
    chain: {source: 'bybit', btc_price: 100000, items: []},
    preview: waiting ? null : {expiry: '2026-09-13T08:00:00Z', market_timestamp: new Date().toISOString(), legs: [], btc_price: 100000, net_credit_usd: 42},
  };
}

test('listing wait clears stale strategy and permits closing, then recovers automatically', async () => {
  const {context, element, pending} = dashboard();
  const deliver = async (payload) => {
    pending.at(-1).resolve({ok: true, text: async () => JSON.stringify(payload)});
    await new Promise(setImmediate);
  };
  element('confirm').checked = true;
  await deliver(marketPayload(false));
  assert.equal(element('statusValue').textContent, '策略就绪');
  assert.equal(element('creditValue').textContent, '$42');
  const waiting = context.loadMarket();
  await deliver(marketPayload(true));
  await waiting;
  assert.equal(element('statusValue').textContent, '等待合约上线');
  assert.doesNotMatch(element('updateText').textContent, /异常|重试/);
  assert.match(element('btcPrice').textContent, /100,000/);
  assert.equal(element('creditValue').textContent, '--');
  assert.equal(context.window.__latestPreview, null);
  assert.match(element('legs').textContent, /等待/);
  assert.equal(element('payoffStats').textContent, '等待可用策略');
  assert.equal(element('openTrade').disabled, true);
  assert.equal(element('rfqCreate').disabled, true);
  assert.equal(element('closeTrade').disabled, false);
  const ready = context.loadMarket();
  await deliver(marketPayload(false));
  await ready;
  assert.equal(element('statusValue').textContent, '策略就绪');
  assert.equal(element('openTrade').disabled, false);
  assert.equal(element('rfqCreate').disabled, false);
});

test('actual market errors remain distinct from listing wait', async () => {
  const {element, pending} = dashboard();
  pending[0].resolve({ok: false, text: async () => JSON.stringify({detail: '行情已过期'})});
  await new Promise(setImmediate);
  assert.equal(element('statusValue').textContent, '行情异常');
  assert.equal(element('statusSub').textContent, '行情已过期');
  assert.equal(element('updateText').textContent, '等待重试');
  assert.equal(element('openTrade').disabled, true);
});

test('observation chain is visible without enabling orders and automatically returns to Sunday strategy', async () => {
  const {context, element, pending} = dashboard();
  const payload = marketPayload(true);
  payload.read_only = true;
  payload.message = '周日未上线，后天到期盘口仅供查看';
  payload.chain.expiry = '2026-09-09T08:00:00+00:00';
  payload.chain.items = [{symbol:'BTC-9SEP26-100000-C',expiry:'2026-09-09T08:00:00Z',option_type:'Call',strike:100000,delta:.5,bid:500,ask:510,mark_price:505}];
  element('confirm').checked = true;
  pending[0].resolve({ok:true,text:async()=>JSON.stringify(payload)});
  await new Promise(setImmediate);
  assert.equal(element('chainPanel').dataset.availability, 'ready');
  assert.match(element('chain').innerHTML, /500.00/);
  assert.equal(element('marketNotice').hidden, false);
  assert.match(element('statusValue').textContent, /只读/);
  assert.match(element('expiry').textContent, /09\/09/);
  assert.equal(context.window.__latestPreview, null);
  assert.equal(element('creditValue').textContent, '--');
  assert.equal(element('openTrade').disabled, true);
  assert.equal(element('rfqCreate').disabled, true);
  assert.equal(element('closeTrade').disabled, false);
  context.updateTradeControls();
  assert.equal(element('openTrade').disabled, true);
  const ready = context.loadMarket();
  pending.at(-1).resolve({ok:true,text:async()=>JSON.stringify(marketPayload(false))});
  await ready;
  assert.equal(element('marketNotice').hidden, true);
  assert.equal(element('openTrade').disabled, false);
  assert.equal(element('rfqCreate').disabled, false);
  assert.equal(element('statusValue').textContent, '策略就绪');
});

test('listing wait keeps state failure visible and closing blocked', async () => {
  const {element, pending} = dashboard();
  const payload = marketPayload(true);
  payload.config.trading_blocked_reason = 'State file unreadable';
  element('confirm').checked = true;
  pending[0].resolve({ok: true, text: async () => JSON.stringify(payload)});
  await new Promise(setImmediate);
  assert.equal(element('statusValue').textContent, '交易已阻止');
  assert.equal(element('statusSub').textContent, 'State file unreadable');
  assert.equal(element('closeTrade').disabled, true);
});

test('FIFO consumes partial fills and apportions fees exactly once', () => {
  const {context, element} = dashboard();
  const open1 = execution('o1', 1, 'Buy', 2, 100, 2);
  const open2 = execution('o2', 2, 'Buy', 1, 110, 1);
  const close1 = execution('c1', 3, 'Sell', 1.5, 120, 1.5, true);
  const close2 = execution('c2', 4, 'Sell', 1.5, 130, 1.5, true);
  const items = [close2, close1, open2, open1];
  const matched = context.matchClosingExecutions(items);
  assert.equal(matched.get(close1), 27);
  assert.equal(matched.get(close2), 32);
  assert.equal(open1.exec_qty, 2);
  context.renderExecutions(items);
  assert.match(element('executions').innerHTML, /组合平仓收益 \+27\.000000/);
  assert.match(element('executions').innerHTML, /组合平仓收益 \+32\.000000/);
});

test('missing history is not matched to future openings or reused fills', () => {
  const {context} = dashboard();
  const open = execution('o', 1, 'Buy', 1, 100, 1);
  const close1 = execution('c1', 2, 'Sell', 2, 120, 1, true);
  const close2 = execution('c2', 3, 'Sell', 0.5, 120, 1, true);
  const future = execution('future', 4, 'Buy', 5, 100, 1);
  const matched = context.matchClosingExecutions([future, close2, close1, open]);
  assert.equal(matched.get(close1), null);
  assert.equal(matched.get(close2), null);
});

test('known opening group and currency prevent matching unrelated lots', () => {
  const {context} = dashboard();
  const unrelated = execution('o1', 1, 'Buy', 1, 10, 0, false, {execution_group: 'A'});
  const correct = execution('o2', 2, 'Buy', 1, 100, 1, false, {execution_group: 'B'});
  const close = execution('c1', 3, 'Sell', 1, 120, 1, true, {opening_group: 'B'});
  const wrongCurrency = execution('c2', 4, 'Sell', 1, 120, 1, true, {fee_currency: 'USDC'});
  const matched = context.matchClosingExecutions([unrelated, correct, close, wrongCurrency]);
  assert.equal(matched.get(close), 18);
  assert.equal(matched.get(wrongCurrency), null);
});

test('duplicate execution IDs do not duplicate quantities or PnL', () => {
  const {context, element} = dashboard();
  const open = execution('o', 1, 'Buy', 1, 100, 1);
  const close = execution('c', 2, 'Sell', 1, 120, 1, true);
  context.renderExecutions([close, {...close}, open, {...open}]);
  assert.equal((element('executions').innerHTML.match(/组合平仓收益 \+18\.000000/g) || []).length, 1);
  const excess = execution('excess', 3, 'Sell', 1, 120, 1, true);
  assert.equal(context.matchClosingExecutions([open, {...open}, close, excess]).get(excess), null);
});

test('short closes account for direction and maker rebates', () => {
  const {context} = dashboard();
  const open = execution('o', 1, 'Sell', 2, 100, -2);
  const close = execution('c', 2, 'Buy', 2, 90, 1, true);
  assert.equal(context.matchClosingExecutions([close, open]).get(close), 21);
});
