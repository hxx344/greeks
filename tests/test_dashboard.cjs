const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function dashboard({embedded = false, request, storage = new Map()} = {}) {
  const elements = new Map();
  const pending = [];
  const requests = [], events = new Map(), messages = [];
  const timers = new Map();
  let nextTimer = 0;
  const parent = {postMessage(message, origin) { messages.push({message, origin}); }};
  const drawing = new Proxy({}, {get(target, name) { return target[name] ?? (() => {}); }});
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'quantity' ? '0.01' : id === 'strategyMode' ? 'iron_condor' : '', dataset: {}, style: {}, attributes: {}, listeners: new Map(),
      options: [], addEventListener(name, callback) { this.listeners.set(name, callback); }, closest() { return null; },
      dispatch(name) { return this.listeners.get(name)?.({currentTarget: this}); },
      setAttribute(name, value) { this.attributes[name] = value; },
      getBoundingClientRect() { return {width: 640, height: 260}; }, getContext() { return drawing; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {visibilityState: 'visible', addEventListener(name, callback) { events.set(name, callback); }, getElementById: element, querySelector() { return null; }, querySelectorAll() { return []; }},
    window: {location: {hostname: embedded ? 'p-0123456789abcdef01234567.hub.localhost' : 'localhost', protocol: 'http:', port: '8000'}, parent, navigator: {onLine: true}, crypto: {randomUUID: () => '22222222-2222-4222-8222-222222222222'}, localStorage: {getItem(key) { return storage.get(key) ?? null; }, setItem(key, value) { storage.set(key, value); }}, addEventListener(name, callback) { events.set(name, callback); }},
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() {},
    fetch(url, options) {
      requests.push({url, options});
      if (request) return request(url, options);
      if (!url.includes('/dashboard/market')) return Promise.reject(new Error('offline'));
      return new Promise((resolve) => pending.push({url, resolve}));
    },
  });
  vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/hub.js`, 'utf8'), context);
  vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/position-payoff.js`, 'utf8'), context);
  vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/performance.js`, 'utf8'), context);
  vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/app.js`, 'utf8'), context);
  const message = data => events.get('message')?.({source: parent, origin: 'http://hub.localhost:8000', data: {channel: 'project-hub', version: 1, ...data}});
  const fireTimer = (id) => { const timer = timers.get(id); assert.ok(timer); timers.delete(id); timer.callback(); };
  return {context, element, pending, requests, events, messages, message, timers, fireTimer};
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

test('polling preserves the shared pending state across all trading controls', () => {
  const {context, element} = dashboard();
  context.setTradeLock('openTrade');
  context.updateTradeControls();
  assert.equal(element('openTrade').textContent, '准备方案…');
  for (const id of ['openTrade', 'closeTrade', 'rfqCreate']) assert.equal(element(id).disabled, true);
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
  await new Promise(setImmediate);
  element('healthUnavailable').style.display = 'none';
  element('healthContent').style.display = 'grid';
  await context.loadAccount();
  assert.equal(element('healthUnavailable').style.display, 'block');
  assert.equal(element('healthContent').style.display, 'none');
  assert.equal(element('healthMode').textContent, '账户连接异常');
  assert.match(element('positionPayoffContent').innerHTML, /持仓更新失败/);
});

test('real dashboard initialization waits for proxy activity across all five streams', async () => {
  const {context, requests, message} = dashboard({embedded: true});
  assert.equal(requests.length, 0);
  message({type: 'ready', role: 'host'});
  assert.equal(requests.length, 0);
  message({type: 'activity', active: true, backgroundUpdates: true});
  assert.deepEqual(requests.map(item => item.url.split('?')[0]).sort(),
    ['/api/dashboard/account', '/api/dashboard/market', '/api/dashboard/orders', '/api/dashboard/performance', '/api/rfq/status'].sort());
  context.window.navigator.onLine = false;
});

test('successful POST notifies summary once without aborting or replaying across activity transitions', async () => {
  let resolveWrite;
  const {context, requests, messages, message, events} = dashboard({embedded: true,
    request(url, options) {
      if (options?.method === 'POST') return new Promise(resolve => { resolveWrite = resolve; });
      return Promise.reject(new Error('offline'));
    }});
  message({type: 'ready', role: 'host'});
  message({type: 'activity', active: true, backgroundUpdates: true});
  const write = context.getJson('/api/trading/open', {method:'POST', body:'{"confirm_live":true}'});
  message({type: 'activity', active: false, backgroundUpdates: true});
  context.window.navigator.onLine = false; events.get('offline')();
  context.window.navigator.onLine = true; events.get('online')();
  message({type: 'activity', active: true, backgroundUpdates: true});
  assert.equal(requests.filter(item => item.options?.method === 'POST').length, 1);
  assert.equal(messages.filter(item => item.message.type === 'changed').length, 0);
  resolveWrite({ok:true, text:async () => '{"results":[]}'});
  await write;
  assert.equal(messages.filter(item => item.message.type === 'changed').length, 1);
  assert.equal(requests.filter(item => item.options?.method === 'POST').length, 1);
  assert.equal(requests.find(item => item.options?.method === 'POST').options.signal, undefined);
});

test('failed POST never reports a successful summary change', async () => {
  const {context, messages, message} = dashboard({embedded: true,
    request() { return Promise.resolve({ok:false, text:async () => '{"detail":"rejected"}'}); }});
  message({type: 'ready', role: 'host'});
  await assert.rejects(context.getJson('/api/trading/open', {method:'POST'}), /rejected/);
  assert.equal(messages.filter(item => item.message.type === 'changed').length, 0);
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

test('performance view keeps currencies separate and explains pending and open groups', () => {
  const {context, element} = dashboard();
  const payload = {network:'testnet',credentials_available:true,groups:[
    {id:'closed-a',currency:'USDT',status:'closed',eligible:true,closure_kind:'expiry',opened_at:'2026-09-01T00:00:00Z',closed_at:'2026-09-02T00:00:00Z',leg_count:4,net_pnl:12,open_fee:1,close_fee:1,delivery_fee:0,issues:[],fills:[]},
    {id:'pending-b',currency:'USDT',status:'pending',opened_at:null,leg_count:4,net_pnl:null,issues:['成交记录待补齐'],fills:[]},
    {id:'open-c',currency:'USDC',status:'open',opened_at:null,leg_count:4,net_pnl:0,floating_pnl:20,issues:[],fills:[]}],
    series:[{currency:'USDT',total_pnl:12,closed_count:1,wins:1,max_drawdown:0,points:[{group_id:'closed-a',time:'2026-09-02T00:00:00Z',pnl:12,cumulative:12}]}]};
  context.renderPerformance(payload);
  assert.match(element('performanceMode').textContent, /测试网/);
  assert.equal(element('performanceTotal').textContent, '$12');
  assert.match(element('performanceGroups').innerHTML, /pending-b/);
  assert.doesNotMatch(element('performanceGroups').innerHTML, /open-c/);
  assert.match(element('performanceGroups').innerHTML, /不纳入综合统计/);
  assert.doesNotMatch(element('performanceChart').innerHTML, /NaN|Infinity/);
  element('performanceCurrency').value = 'USDC';
  context.renderPerformance(payload);
  assert.equal(element('performanceTotal').textContent, '--');
  assert.match(element('performanceGroups').innerHTML, /open-c/);
  assert.doesNotMatch(element('performanceGroups').innerHTML, /closed-a/);
  assert.match(element('performanceChart').innerHTML, /暂无已核实/);
});

test('sampled charts use real time, bounded smooth paths, and break at missing samples', () => {
  const {context} = dashboard();
  const chart = context.performanceChart({currency:'USDT',sample_seconds:60,points:[
    {time:'2026-09-01T00:00:00Z',cumulative:1,pnl:1},
    {time:'2026-09-01T00:01:00Z',cumulative:4,pnl:4},
    {time:'2026-09-01T00:02:00Z',cumulative:2,pnl:2},
    {time:'2026-09-01T02:00:00Z',cumulative:8,pnl:8,terminal:true}]},310);
  assert.match(chart, / C/);
  assert.doesNotMatch(chart, / H| V|NaN|Infinity/);
  assert.match(chart, /1 段采样缺口/);
  assert.match(chart, /3 个真实采样点/);
  const old = context.performanceChart({currency:'USDT',points:[{time:'2026-09-01T02:00:00Z',cumulative:8,pnl:8,terminal:true}]});
  assert.match(old, /历史无持仓采样/);
  assert.doesNotMatch(old, / C/);
});

test('testnet mode requires separate plan confirmation and reports exchange fills as testnet', async () => {
  const {context, element, pending} = dashboard();
  const payload = marketPayload(false);
  Object.assign(payload.config, {environment: 'testnet', live_enabled: false, trading_enabled: true, market_testnet: true});
  pending[0].resolve({ok: true, text: async () => JSON.stringify(payload)});
  await new Promise(setImmediate);
  assert.equal(element('modeTitle').textContent, '测试网模式已启用');
  assert.equal(element('environment').textContent, 'TESTNET');
  assert.match(element('statusSub').textContent, /测试网行情/);
  assert.equal(element('openTrade').disabled, false, 'preparing a plan never submits an order');
  assert.equal(element('planSubmit').disabled, true);
  assert.equal(element('rfqCreate').disabled, true);
  element('confirm').checked = true;
  context.updateTradeControls();
  assert.equal(element('rfqCreate').disabled, false);
  assert.equal(element('planSubmit').disabled, true, 'opening preview consent is not plan consent');
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

function strategyMarket(mode = 'iron_condor') {
  const payload = marketPayload(false);
  const leg = (type, side, strike, mark) => ({symbol: `BTC-13SEP99-${strike}-${type[0]}-USDT`, option_type: type,
    side, strike, mark_price: mark, qty: .01, delta: type === 'Call' ? .15 : -.15, target_delta: .15,
    estimated_fee_usd: .1, fee_cap_usd: .2});
  const shorts = [leg('Put', 'Sell', 95000, 600), leg('Call', 'Sell', 105000, 600)];
  Object.assign(payload.config, {strategy_mode: 'iron_condor', max_margin_usd: 2500, auto_open: false});
  Object.assign(payload.preview, {strategy_mode: mode, unbounded_loss: mode === 'short_strangle',
    margin_mode: 'PORTFOLIO_MARGIN', margin_basis: mode === 'short_strangle' ? 'regular_order_im' : 'portfolio_loss_estimate',
    legs: mode === 'short_strangle' ? shorts : [leg('Put', 'Buy', 90000, 100), ...shorts, leg('Call', 'Buy', 110000, 100)],
    net_credit_usd: mode === 'short_strangle' ? 12 : 10, max_loss_usd: mode === 'short_strangle' ? null : 40,
    risk_reward: mode === 'short_strangle' ? null : .25, estimated_margin_usd: 550, estimated_initial_margin_usd: 500,
    estimated_maintenance_margin_usd: 250, estimated_trading_cost_usd: .4, estimated_fee_rate: .0003, fee_cap_pct: .07});
  return payload;
}
async function deliverMarket(page, payload) {
  page.pending.at(-1).resolve({ok: true, text: async () => JSON.stringify(payload)});
  await new Promise(setImmediate);
}
async function readyStrategy(page, mode) {
  if (mode === 'short_strangle') {
    page.element('strategyMode').value = mode;
    page.element('strategyMode').dispatch('change');
    await deliverMarket(page, strategyMarket());
  }
  await deliverMarket(page, strategyMarket(mode));
}

function unavailableStrategyMarket(mode, environment = 'live') {
  const payload = strategyMarket(mode), expiry = payload.preview.expiry;
  Object.assign(payload.config, {environment, trading_enabled:true});
  payload.chain = {source:'bybit', btc_price:102000, updated_at:new Date().toISOString(), expiry,
    items:payload.preview.legs.map(leg => ({...leg, expiry, bid:leg.mark_price, ask:leg.mark_price + 10}))};
  return {...payload, status:'strategy_unavailable', read_only:true, reason_code:'short_strike_order',
    message:'当前报价下，卖出 Put 的行权价必须低于卖出 Call，暂不能组成所选策略。', preview:null};
}

test('strategy unavailability preserves real quotes and position spot, isolates opening controls, and recovers automatically', async (t) => {
  for (const [mode, environment] of [['iron_condor', 'live'], ['short_strangle', 'testnet']]) {
    await t.test(`${mode}/${environment}`, async () => {
      const page = dashboard(), {context, element} = page;
      await readyStrategy(page, mode);
      const unavailable = unavailableStrategyMarket(mode, environment);
      context.window.__positionError = false;
      context.window.__positionSnapshot = [{symbol:'BTC-13Sep99-100000-C-USDT', side:'Buy', size:.1, avg_price:100, unrealised_pnl:3}];
      const ready = {...strategyMarket(mode), config:unavailable.config, chain:unavailable.chain};
      const previous = context.loadMarket(); await deliverMarket(page, ready); await previous;
      assert.match(element('chain').innerHTML, /selected-mark/);
      assert.notEqual(element('creditValue').textContent, '--');
      element('confirm').checked = true;

      const waiting = context.loadMarket(); await deliverMarket(page, unavailable); await waiting;
      assert.equal(element('statusValue').textContent, '策略暂不可用');
      assert.equal(element('statusValue').dataset.state, 'waiting');
      assert.equal(element('statusSub').textContent, unavailable.message);
      assert.equal(element('marketNotice').textContent, unavailable.message);
      assert.equal(element('marketNotice').hidden, false);
      assert.equal(element('updateText').textContent, '行情更新正常 · 策略等待');
      assert.equal(element('environment').textContent, environment === 'testnet' ? 'TESTNET' : 'LIVE');
      assert.equal(element('modeBox').dataset.mode, environment);
      assert.equal(element('modeTitle').textContent, environment === 'testnet' ? '测试网模式已启用' : '实盘模式已启用');
      assert.doesNotMatch(element('modeText').textContent, /备用|仅供查看|只读/);
      assert.match(element('btcPrice').textContent, /102,000/);
      assert.equal(element('chainPanel').dataset.availability, 'ready');
      assert.match(element('chain').innerHTML, /600.00/);
      assert.doesNotMatch(element('chain').innerHTML, /selected-mark|class="(?:atm )?selected"/);
      assert.equal(element('legSummary').textContent, '暂无可用策略');
      assert.equal(context.window.__latestChain.length, unavailable.chain.items.length);
      assert.equal(context.window.__latestPreview, null);
      assert.equal(context.window.__previewSelection, null);
      for (const id of ['creditValue', 'lossValue', 'marginValue', 'maintenanceValue', 'costValue', 'rrValue']) assert.equal(element(id).textContent, '--');
      assert.equal(element('legs').textContent, unavailable.message);
      assert.equal(element('payoffStats').textContent, '等待可用策略');
      assert.equal(context.window.__positionMarket.price, 102000);
      assert.equal(context.window.__positionMarket.timestamp, Date.parse(unavailable.chain.updated_at));
      assert.match(element('positionPayoffContent').innerHTML, /pp-spot-dot/);
      assert.equal(element('openTrade').disabled, true);
      assert.equal(element('rfqCreate').disabled, true);
      assert.equal(element('closeTrade').disabled, false);
      await context.openTrade(); await context.createRfq();
      assert.equal(page.requests.filter(item => item.options?.method === 'POST').length, 0);

      const recovering = context.loadMarket(); await deliverMarket(page, ready); await recovering;
      assert.equal(element('statusValue').textContent, '策略就绪');
      assert.equal(element('marketNotice').hidden, true);
      assert.equal(context.window.__latestPreview.strategy_mode, mode);
      assert.match(element('chain').innerHTML, /selected-mark/);
      assert.equal(element('openTrade').disabled, false);
      assert.equal(element('rfqCreate').disabled, false);
      assert.equal(page.requests.filter(item => item.options?.method === 'POST').length, 0);

      const failed = context.loadMarket();
      page.pending.at(-1).resolve({ok:false, status:503, text:async () => JSON.stringify({detail:'行情已过期'})});
      await failed;
      assert.equal(element('statusValue').textContent, '行情异常');
      assert.equal(element('statusSub').textContent, '行情已过期');
      assert.equal(element('updateText').textContent, '等待重试');
      assert.equal(context.window.__positionMarket, null);
      assert.doesNotMatch(element('positionPayoffContent').innerHTML, /pp-spot-dot/);
      assert.equal(element('chainPanel').dataset.availability, 'unavailable');
      assert.equal(element('openTrade').disabled, true);
    });
  }
});

test('strategy unavailable responses that expire in transit retain the chain without claiming fresh quotes', async () => {
  const page = dashboard(), {context, element} = page;
  await new Promise(setImmediate);
  const payload = unavailableStrategyMarket('iron_condor');
  const sampledAt = Date.parse(payload.chain.updated_at), arrivedAt = sampledAt + 31000;
  vm.runInContext(`Date.now = () => ${arrivedAt}`, context);
  context.window.__positionError = false;
  context.window.__positionSnapshot = [{symbol:'BTC-13Sep99-100000-C-USDT', side:'Buy', size:.1, avg_price:100, unrealised_pnl:3}];
  await deliverMarket(page, payload);
  assert.equal(element('statusValue').textContent, '行情已过期');
  assert.equal(element('updateText').textContent, '行情已过期 · 等待更新');
  assert.match(element('marketNotice').textContent, /保留最近收到的真实盘口/);
  assert.match(element('marketNotice').textContent, /卖出 Put/);
  assert.equal(element('chainPanel').dataset.availability, 'stale');
  assert.match(element('chain').innerHTML, /600.00/);
  assert.match(element('btcPrice').textContent, /102,000/);
  assert.equal(context.window.__latestChain.length, payload.chain.items.length);
  assert.equal(context.window.__positionMarket.timestamp, sampledAt);
  assert.doesNotMatch(element('positionPayoffContent').innerHTML, /pp-spot-dot/);
  assert.match(element('positionPayoffContent').innerHTML, /pp-line/);
  assert.equal(element('openTrade').disabled, true);
  assert.equal(element('rfqCreate').disabled, true);
  assert.equal(element('closeTrade').disabled, false);
  const refresh = context.loadMarket();
  await deliverMarket(page, {...payload, chain:{...payload.chain, updated_at:new Date(arrivedAt).toISOString()}});
  await refresh;
  assert.equal(element('statusValue').textContent, '策略暂不可用');
  assert.equal(element('updateText').textContent, '行情更新正常 · 策略等待');
  assert.equal(element('chainPanel').dataset.availability, 'ready');
  assert.match(element('positionPayoffContent').innerHTML, /pp-spot-dot/);
  assert.equal(element('openTrade').disabled, true);
});

test('strategy unavailable responses with invalid or future timestamps cannot mark prices as fresh', async (t) => {
  for (const [name, timestamp] of [['invalid', 'not-a-date'], ['missing', undefined], ['future', new Date(Date.now() + 60000).toISOString()]]) {
    await t.test(name, async () => {
      const page = dashboard(), {context, element} = page;
      await new Promise(setImmediate);
      const payload = unavailableStrategyMarket('iron_condor');
      payload.chain.updated_at = timestamp;
      context.window.__positionError = false;
      context.window.__positionSnapshot = [{symbol:'BTC-13Sep99-100000-C-USDT', side:'Buy', size:.1, avg_price:100, unrealised_pnl:3}];
      await deliverMarket(page, payload);
      assert.equal(element('statusValue').textContent, '行情时间待核对');
      assert.equal(element('updateText').textContent, '行情时间待核对 · 等待更新');
      assert.equal(element('chainPanel').dataset.availability, 'time_unknown');
      assert.match(element('chain').innerHTML, /600.00/);
      assert.match(element('btcPrice').textContent, /102,000/);
      assert.equal(context.window.__positionMarket, null);
      assert.doesNotMatch(element('positionPayoffContent').innerHTML, /pp-spot-dot/);
      assert.match(element('positionPayoffContent').innerHTML, /pp-line/);
      assert.equal(element('openTrade').disabled, true);
      assert.equal(element('rfqCreate').disabled, true);
      assert.equal(element('closeTrade').disabled, false);
    });
  }
});

test('strategy unavailability preserves execution stop and existing close blockers', async (t) => {
  for (const blocked of [false, true]) await t.test(blocked ? 'state error' : 'active execution', async () => {
    const page = dashboard(), {context, element} = page;
    const payload = unavailableStrategyMarket('iron_condor');
    if (blocked) payload.config.trading_blocked_reason = 'State file unreadable';
    context.renderOrders({items:[], groups:[executionGroup()], execution_active:true});
    await deliverMarket(page, payload);
    assert.equal(element('closeTrade').disabled, true);
    assert.match(element('activeExecutions').innerHTML, /data-stop="execution-one" >停止执行/);
    assert.equal(element('statusValue').textContent, blocked ? '交易已阻止' : '策略暂不可用');
    assert.equal(element('statusSub').textContent, blocked ? 'State file unreadable' : payload.message);
    context.renderOrders({items:[], groups:[], execution_active:false});
    assert.equal(element('closeTrade').disabled, blocked);
  });
});

test('an unavailable response from the previous strategy selection cannot replace the current selection', async () => {
  const page = dashboard(), {context, element} = page;
  element('strategyMode').value = 'short_strangle';
  element('strategyMode').dispatch('change');
  await deliverMarket(page, unavailableStrategyMarket('iron_condor'));
  assert.equal(element('strategyMode').value, 'short_strangle');
  assert.notEqual(element('statusValue').textContent, '策略暂不可用');
  assert.equal(context.window.__latestPreview, null);
  assert.equal(element('openTrade').disabled, true);
  await deliverMarket(page, strategyMarket('short_strangle'));
  assert.equal(element('statusValue').textContent, '策略就绪');
  assert.equal(context.window.__latestPreview.strategy_mode, 'short_strangle');
});

test('mode changes clear confirmation and discard the old preview before showing an unbounded strategy', async () => {
  const page = dashboard(), {context, element, pending} = page;
  element('confirm').checked = true;
  element('strategyMode').value = 'short_strangle';
  element('strategyMode').dispatch('change');
  assert.equal(element('confirm').checked, false);
  assert.equal(element('openTrade').disabled, true);
  assert.equal(element('rfqCreate').disabled, true);
  assert.equal(context.window.__latestPreview, null);
  await context.openTrade(); await context.createRfq();
  assert.equal(page.requests.filter(item => item.options?.method === 'POST').length, 0);
  await deliverMarket(page, strategyMarket());
  assert.equal(context.window.__latestPreview, null);
  assert.equal(pending.length, 2);
  assert.match(pending[1].url, /quantity=0.01&strategy_mode=short_strangle/);
  const payload = strategyMarket('short_strangle');
  payload.config.max_margin_usd = 100; // Preview remains available even above the separate budget.
  await deliverMarket(page, payload);
  assert.equal(element('lossValue').textContent, '无上限');
  assert.equal(element('marginSub').textContent, '常规保证金估算（含缓冲）');
  assert.equal(element('marginValue').textContent, '$550');
  assert.equal(element('rrValue').textContent, '--');
  assert.equal(element('legSummary').textContent, 'BUY 0 · SELL 2');
  assert.match(element('strategyRisk').textContent, /保证金预算 \$100/);
  assert.match(element('confirmText').textContent, /亏损无上限/);
  assert.equal(element('payoffRisk').hidden, false);
  assert.match(element('payoffRisk').textContent, /亏损无上限.*有限价格范围/);
  assert.match(element('payoffChart').attributes['aria-label'], /亏损无上限/);
  assert.match(element('payoffStats').innerHTML, /最大亏损<\/span><strong class="loss">无上限/);
  assert.doesNotMatch(element('payoffStats').innerHTML, /NaN|Infinity|最大风险/);
  assert.equal(element('quantity').value, '0.01');
  element('confirm').checked = true; context.updateTradeControls();
  assert.equal(element('openTrade').disabled, false);
});

test('a four-leg or legacy response cannot authorize a selected two-leg order', async () => {
  const page = dashboard();
  page.element('strategyMode').value = 'short_strangle'; page.element('strategyMode').dispatch('change');
  await deliverMarket(page, strategyMarket());
  const wrong = strategyMarket(); delete wrong.preview.strategy_mode;
  await deliverMarket(page, wrong);
  page.element('confirm').checked = true; page.context.updateTradeControls();
  assert.equal(page.context.window.__latestPreview, null);
  assert.equal(page.element('openTrade').disabled, true);
  assert.match(page.element('statusSub').textContent, /预览与所选开仓结构不一致/);
  await page.context.openTrade();
  assert.equal(page.requests.filter(item => item.options?.method === 'POST').length, 0);
});

test('configured default initializes the selector while manual changes leave automatic strategy unchanged', async () => {
  const page = dashboard();
  const first = strategyMarket();
  Object.assign(first.config, {strategy_mode: 'short_strangle', auto_open: true});
  await deliverMarket(page, first);
  assert.equal(page.element('strategyMode').value, 'short_strangle');
  assert.equal(page.context.window.__latestPreview, null);
  assert.match(page.pending.at(-1).url, /strategy_mode=short_strangle/);
  const second = strategyMarket('short_strangle'); second.config = first.config;
  await deliverMarket(page, second);
  page.element('strategyMode').value = 'iron_condor'; page.element('strategyMode').dispatch('change');
  await deliverMarket(page, first);
  assert.equal(page.element('strategyMode').value, 'iron_condor');
  assert.equal(page.context.window.__latestPreview.strategy_mode, 'iron_condor');
  assert.equal(page.element('automaticMode').textContent, '自动任务：双腿卖出 · 已启用');
  assert.equal(page.element('payoffRisk').hidden, true);
});

test('manual opening and new RFQ capture mode once and never replay on a later mode change', async () => {
  for (const [action, endpoint] of [['openTrade', '/api/trading/plans'], ['createRfq', '/api/rfq/create']]) {
    const page = dashboard(); await readyStrategy(page, 'short_strangle');
    page.element('confirm').checked = true;
    const writes = [];
    page.context.fetch = (url, options) => options?.method === 'POST'
      ? new Promise(resolve => writes.push({url, options, resolve})) : Promise.reject(new Error('offline'));
    const operation = page.context[action]();
    await page.context[action]();
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url, endpoint);
    assert.equal(writes[0].options.signal, undefined);
    assert.equal(JSON.parse(writes[0].options.body).strategy_mode, 'short_strangle');
    assert.equal(JSON.parse(writes[0].options.body).quantity, .01);
    page.element('strategyMode').value = 'iron_condor'; page.element('strategyMode').dispatch('change');
    assert.equal(page.element('confirm').checked, false);
    writes[0].resolve({ok: true, text: async () => '{"results":[{"status":"simulated"}]}'});
    await operation;
    assert.equal(writes.length, 1);
    assert.equal(JSON.parse(writes[0].options.body).strategy_mode, 'short_strangle');
  }
});

test('existing RFQ keeps its own two-leg directions and risk after changing the opening selector', () => {
  const {context, element} = dashboard();
  element('strategyMode').value = 'iron_condor';
  const legs = strategyMarket('short_strangle').preview.legs;
  const state = {rfq_id:'saved-two-leg', status:'Active', strategy_mode:'short_strangle', strategy_type:'custom', legs,
    quotes:[{quoteId:'quote-one', quoteSellList:legs.map(leg => ({symbol:leg.symbol, qty:leg.qty, price:600}))}]};
  context.renderRfq(state);
  assert.match(element('rfqType').textContent, /双腿卖出.*亏损无上限/);
  assert.match(element('rfqQuotes').innerHTML, /2\/2 腿/);
  assert.match(element('rfqQuotes').innerHTML, /本次询价 2 腿成交方向/);
  assert.equal((element('rfqQuotes').innerHTML.match(/<span>Sell 600/g) || []).length, 2);
  assert.doesNotMatch(element('rfqQuotes').innerHTML, /四腿|折扣/);
  assert.match(element('rfqQuotes').innerHTML, /预估费率0.03% · 单腿上限7%/);
  element('strategyMode').value = 'short_strangle';
  state.strategy_mode = 'iron_condor'; state.legs = strategyMarket().preview.legs;
  context.renderRfq(state);
  assert.equal(element('rfqType').textContent, '四腿铁鹰');
  assert.match(element('rfqQuotes').innerHTML, /本次询价 4 腿成交方向/);
});

test('closing stays bound to tracked positions and does not send the opening selector', async () => {
  const page = dashboard(); await readyStrategy(page, 'short_strangle');
  page.element('confirm').checked = true; page.context.updateTradeControls();
  assert.match(page.element('closeTrade').textContent, /平仓已跟踪持仓/);
  assert.doesNotMatch(page.element('closeTrade').textContent, /双腿|四腿/);
  const writes = [];
  page.context.fetch = (url, options) => {
    if (options?.method !== 'POST') return Promise.reject(new Error('offline'));
    writes.push({url, body:JSON.parse(options.body)});
    return Promise.resolve({ok:true, text:async () => '{"results":[{"status":"simulated"}]}'});
  };
  await page.context.closeTrade();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url, '/api/trading/plans');
  assert.deepEqual(writes[0].body, {operation:'close'});
  assert.equal(page.element('planSubmit').disabled, true);
});

test('legacy four-leg preview keeps finite loss and PM estimate labels', async () => {
  const page = dashboard(), payload = strategyMarket();
  delete payload.preview.strategy_mode; delete payload.preview.margin_basis; delete payload.preview.unbounded_loss;
  await deliverMarket(page, payload);
  assert.equal(page.element('lossValue').textContent, '$40');
  assert.equal(page.element('marginSub').textContent, 'PM 压力测试估算');
  assert.equal(page.element('payoffRisk').hidden, true);
  assert.match(page.element('payoffStats').innerHTML, /最大亏损<\/span><strong class="loss">\$40/);
});

test('two-leg tracked positions retain unbounded-loss annotation inside a narrow SVG chart', () => {
  const {context, element} = dashboard();
  context.window.__positionSnapshot = [
    {symbol:'BTC-13Sep99-95000-P-USDT', side:'Sell', size:.01, avg_price:600, unrealised_pnl:2},
    {symbol:'BTC-13Sep99-105000-C-USDT', side:'Sell', size:.01, avg_price:600, unrealised_pnl:2},
  ];
  element('positionPayoffContent').clientWidth = 300;
  context.renderPositionPayoff();
  assert.match(element('positionPayoffContent').innerHTML, /<text class="pp-risk-note"[^>]*>亏损无上限 · 图示价格范围有限<\/text>/);
  assert.match(element('positionPayoffContent').innerHTML, /最大到期亏损<\/span><strong class="">无上限/);
  assert.doesNotMatch(element('positionPayoffContent').innerHTML, /NaN|Infinity/);
});

function orderSnapshot(item = {}, snapshot = {}) {
  return {generated_at: '2026-10-10T04:00:05Z', execution_active: true, active_count: 1, terminal_count: 0,
    items: [{order_link_id: 'ic-open-0', order_id: 'exchange-0', symbol: 'BTC-11OCT26-100000-C',
      operation: 'open', side: 'Sell', execution_type: 'BBO', qty: .01, filled_qty: 0, remaining_qty: .01,
      requested_price: 100, confirmed_price: 100, terminal: false, status: 'pending', phase: 'working',
      exchange_status: 'New', created_at: '2026-10-10T04:00:00Z', updated_at: '2026-10-10T04:00:04Z',
      last_confirmed_at: '2026-10-10T04:00:03Z', stale: false, ...item}], ...snapshot};
}
const jsonResponse = payload => ({ok: true, text: async () => JSON.stringify(payload)});

function tradePlan(operation = 'open', overrides = {}) {
  return {plan_id: 'plan-one', operation, environment: 'testnet', strategy_mode: 'iron_condor',
    created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 30000).toISOString(),
    legs: strategyMarket().preview.legs.map(leg => ({...leg, reference_price: leg.mark_price, expiry: '2099-09-13T08:00:00Z'})),
    estimated_gross_usd: 10, estimated_fee_usd: .4, estimated_net_usd: 9.6, estimated_margin_usd: 55,
    min_net_income_usd: 9.6, max_net_cost_usd: 10.4, ...overrides};
}
function executionGroup(overrides = {}) {
  return {execution_id: 'execution-one', request_id: '22222222-2222-4222-8222-222222222222', plan_id: 'plan-one',
    operation: 'open', strategy_mode: 'iron_condor', execution_status: 'running', can_stop: true,
    created_at: '2026-10-10T04:00:00Z', elapsed_seconds: 25, completed_legs: 1, total_legs: 4, progress_ratio: .35,
    amounts_complete: true, gross_amount: 8, fee_amount: .2, net_amount: 7.8, currency: 'USDT',
    legs: [{symbol: 'BTC-13SEP99-105000-C-USDT', side: 'Sell', target_qty: .01, filled_qty: .004,
      remaining_qty: .006, progress_ratio: .4, complete: false, unknown: false, orders: []}], ...overrides};
}

test('order polling updates fills every second while task submission is pending and every trading action stays locked', async () => {
  let resolveWrite, orderReads = 0;
  const page = dashboard({request(url, options) {
    if (url === '/api/trading/plans') return Promise.resolve(jsonResponse(tradePlan()));
    if (url === '/api/trading/tasks' && options?.method === 'POST') return new Promise(resolve => { resolveWrite = resolve; });
    if (url.startsWith('/api/dashboard/market')) return Promise.resolve(jsonResponse(strategyMarket()));
    if (url === '/api/dashboard/orders') {
      orderReads++;
      return Promise.resolve(jsonResponse(orderReads === 1 ? {items: [], groups: [], execution_active:false} : orderSnapshot({
        phase: 'amending', requested_price: 105, confirmed_price: 100, filled_qty: .002, remaining_qty: .008,
      })));
    }
    return Promise.reject(new Error('offline'));
  }});
  await new Promise(setImmediate);
  await page.context.openTrade();
  page.element('planConfirm').checked = true;
  const write = page.context.submitTradePlan();
  await page.context.openTrade(); await page.context.closeTrade(); await page.context.createRfq();
  assert.equal(page.requests.filter(item => item.url === '/api/trading/tasks').length, 1);
  const readsBeforeTick = orderReads;
  const timer = [...page.timers].find(([, item]) => item.delay === 1000);
  assert.ok(timer, 'orders have an independent one-second timer');
  page.fireTimer(timer[0]);
  await new Promise(setImmediate);
  assert.equal(orderReads, readsBeforeTick + 1);
  for (const id of ['openTrade', 'closeTrade', 'rfqCreate']) assert.equal(page.element(id).disabled, true);
  assert.match(page.element('ordersBody').innerHTML, /正在改价/);
  assert.match(page.element('ordersBody').innerHTML, /申请<\/small><b>105\.00/);
  assert.match(page.element('ordersBody').innerHTML, /已确认<\/small><b>100\.00/);
  assert.match(page.element('ordersBody').innerHTML, /累计成交<\/small><b>0\.002/);
  assert.match(page.element('ordersBody').innerHTML, /未成交<\/small><b>0\.008/);
  assert.equal(page.element('ordersSyncState').textContent, '执行中 · 持续同步');
  resolveWrite(jsonResponse({execution_id: 'execution-one', execution_status:'accepted'}));
  await write;
  assert.equal(page.requests.filter(item => item.url === '/api/trading/tasks').length, 1);
});

test('order failures and offline transitions preserve the last snapshot and recover with a fresh read', async () => {
  let payload = orderSnapshot(), fail = false;
  const {context, element, events} = dashboard({request(url) {
    if (url !== '/api/dashboard/orders') return Promise.reject(new Error('offline'));
    return fail ? Promise.reject(new Error('unavailable')) : Promise.resolve(jsonResponse(payload));
  }});
  await new Promise(setImmediate);
  const previousRows = element('ordersBody').innerHTML, previousTime = element('ordersSnapshotTime').textContent;
  fail = true;
  await context.loadOrders();
  assert.equal(element('ordersBody').innerHTML, previousRows);
  assert.equal(element('ordersSnapshotTime').textContent, previousTime);
  assert.equal(element('orders').dataset.state, 'stale');
  assert.match(element('ordersNotice').textContent, /最近一次记录.*已过期/);
  context.window.navigator.onLine = false; events.get('offline')(); context.renderOrdersFreshness();
  assert.equal(element('orders').dataset.state, 'offline');
  assert.match(element('ordersNotice').textContent, /保留最近委托记录/);
  assert.equal(element('ordersBody').innerHTML, previousRows);
  fail = false;
  payload = orderSnapshot({terminal: true, status: 'timeout_cancelled', phase: 'terminal', exchange_status: 'Cancelled',
    filled_qty: .004, remaining_qty: .006}, {generated_at: '2026-10-10T04:00:20Z', execution_active: false});
  context.window.navigator.onLine = true; events.get('online')();
  await new Promise(setImmediate);
  assert.equal(element('orders').dataset.state, 'ready');
  assert.equal(element('ordersNotice').hidden, true);
  assert.match(element('ordersBody').innerHTML, /已撤单/);
  assert.match(element('ordersBody').innerHTML, /累计成交<\/small><b>0\.004/);
  assert.doesNotMatch(element('ordersBody').innerHTML, /已全部成交/);
  assert.equal(element('ordersSummary').textContent, '未结束 0 笔 · 最近结束 1 笔');
  assert.notEqual(element('ordersSnapshotTime').textContent, previousTime);
});

test('order states preserve unknown values and distinguish rejected, cancelled, partial, and full execution', () => {
  const {context, element} = dashboard({embedded: true});
  const base = orderSnapshot().items[0];
  for (const [overrides, expected] of [
    [{terminal: true, status: 'filled', filled_qty: .01, remaining_qty: 0}, '已全部成交'],
    [{terminal: true, status: 'filled', filled_qty: .004, remaining_qty: .006}, '部分成交 · 已结束'],
    [{terminal: true, exchange_status: 'Rejected', filled_qty: .004}, '已拒绝'],
    [{terminal: true, status: 'filled', exchange_status: 'Cancelled', filled_qty: .01, remaining_qty: 0}, '已撤单'],
    [{terminal: false, phase: '__proto__'}, '状态未知 · 待核对'],
  ]) assert.equal(context.orderDisplayState({...base, ...overrides}).label, expected);
  context.renderOrders(orderSnapshot({requested_price: null, confirmed_price: null, qty: null, filled_qty: null,
    remaining_qty: null, created_at: null, updated_at: null, last_confirmed_at: null, stale: true,
    phase: 'unknown', symbol: '<script>attack()</script>', order_link_id: 'bad" onmouseover="attack()',
    order_id: '<img src=x onerror=attack()>', exchange_status: '<svg onload=attack()>'}));
  const html = element('ordersBody').innerHTML;
  assert.match(html, /申请<\/small><b>--/);
  assert.match(html, /已确认<\/small><b>--/);
  assert.match(html, /累计成交<\/small><b>--/);
  assert.match(html, /确认<\/small><b>--/);
  assert.match(html, /尚无交易所确认 · 待核对/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /bad&quot; onmouseover=&quot;attack\(\)/);
  assert.doesNotMatch(html, /<script|<img|<svg|NaN|Infinity|>0\.00</);
});

test('order reads coalesce, discard offline responses, and throttle in the background', async () => {
  const reads = [];
  const page = dashboard({embedded: true, request(url) {
    if (url === '/api/dashboard/orders') return new Promise(resolve => reads.push(resolve));
    return Promise.reject(new Error('offline'));
  }});
  page.message({type: 'ready', role: 'host'});
  page.message({type: 'activity', active: true, backgroundUpdates: true});
  assert.equal(reads.length, 1);
  await page.context.loadOrders(); await page.context.loadOrders();
  assert.equal(reads.length, 1);
  reads[0](jsonResponse(orderSnapshot({symbol: 'superseded'})));
  await new Promise(setImmediate);
  assert.equal(reads.length, 2);
  assert.doesNotMatch(page.element('ordersBody').innerHTML || '', /superseded/);
  page.context.window.navigator.onLine = false; page.events.get('offline')();
  reads[1](jsonResponse(orderSnapshot({symbol: 'late-offline'})));
  await new Promise(setImmediate);
  assert.equal(reads.length, 2);
  assert.equal(page.timers.size, 0);
  assert.doesNotMatch(page.element('ordersBody').innerHTML || '', /late-offline/);
  page.context.window.navigator.onLine = true; page.events.get('online')();
  assert.equal(reads.length, 3);
  reads[2](jsonResponse(orderSnapshot({symbol: 'fresh-online'})));
  await new Promise(setImmediate);
  assert.match(page.element('ordersBody').innerHTML, /fresh-online/);
  page.message({type: 'activity', active: false, backgroundUpdates: true});
  assert.equal(reads.length, 3);
  assert.equal(page.timers.size, 5);
  assert.ok([...page.timers.values()].every(timer => timer.delay >= 30000));
  for (const id of [...page.timers.keys()]) page.fireTimer(id);
  assert.equal(reads.length, 4);
  page.message({type: 'activity', active: false, backgroundUpdates: false});
  reads[3](jsonResponse(orderSnapshot({symbol: 'late-paused'})));
  await new Promise(setImmediate);
  assert.equal(page.timers.size, 0);
  assert.match(page.element('ordersBody').innerHTML, /fresh-online/);
});

test('order clock updates leave unchanged rows in place and invalid payloads retain the previous snapshot', async () => {
  let payload = orderSnapshot();
  const {context, element} = dashboard({request(url) {
    return url === '/api/dashboard/orders' ? Promise.resolve(jsonResponse(payload)) : Promise.reject(new Error('offline'));
  }});
  await new Promise(setImmediate);
  let rows = element('ordersBody').innerHTML, replacements = 0;
  Object.defineProperty(element('ordersBody'), 'innerHTML', {
    get() { return rows; }, set(value) { rows = value; replacements++; },
  });
  payload = {...payload, generated_at: '2026-10-10T04:00:06Z'};
  await context.loadOrders();
  assert.equal(replacements, 0);
  assert.equal(element('ordersSnapshotTime').textContent, '服务器快照 10-10 04:00:06 UTC');
  payload = {items: [null]};
  await context.loadOrders();
  assert.equal(replacements, 0);
  assert.equal(element('orders').dataset.state, 'stale');
  assert.equal(element('ordersSnapshotTime').textContent, '服务器快照 10-10 04:00:06 UTC');
});

async function planPage(plan = tradePlan(), handler = () => Promise.reject(new Error('offline'))) {
  const page = dashboard({request(url, options) {
    if (url.startsWith('/api/dashboard/market')) return Promise.resolve(jsonResponse(strategyMarket()));
    if (url === '/api/trading/plans') return Promise.resolve(jsonResponse(plan));
    return handler(url, options);
  }});
  await new Promise(setImmediate);
  return page;
}

test('fixed open plan requires its own confirmation, binds contract and net price, and never submits twice', async () => {
  let finish;
  const page = await planPage(tradePlan(), (url, options) => url === '/api/trading/tasks'
    ? new Promise(resolve => { finish = resolve; }) : Promise.reject(new Error('offline')));
  page.element('confirm').checked = true;
  await page.context.openTrade();
  assert.equal(page.element('tradePlanDialog').open, true);
  assert.equal(page.element('planSubmit').disabled, true);
  assert.equal(page.element('planConfirm').checked, false);
  assert.match(page.element('planLegs').innerHTML, /BTC-13SEP99-105000-C-USDT/);
  assert.match(page.element('planLegs').innerHTML, /数量 0\.01 BTC/);
  assert.equal(page.element('planNetLimit').value, '9.6');
  await page.context.submitTradePlan();
  assert.equal(page.requests.filter(item => item.url === '/api/trading/tasks').length, 0);
  page.element('planNetLimit').value = '8.5'; page.element('planNetLimit').dispatch('input');
  page.element('planConfirm').checked = true;
  const submission = page.context.submitTradePlan();
  await page.context.submitTradePlan(); await page.context.closeTrade(); await page.context.createRfq();
  const tasks = page.requests.filter(item => item.url === '/api/trading/tasks');
  assert.equal(tasks.length, 1);
  assert.deepEqual(JSON.parse(tasks[0].options.body), {plan_id: 'plan-one', request_id: '22222222-2222-4222-8222-222222222222', confirm_live: true, min_net_income_usd: 8.5});
  assert.equal(tasks[0].options.signal, undefined);
  finish(jsonResponse({execution_id:'execution-one', execution_status:'accepted'}));
  await submission;
  for (const id of ['openTrade', 'closeTrade', 'rfqCreate']) assert.equal(page.element(id).disabled, true);
});

test('close confirmation uses tracked remainder, accepts negative net cost, and ignores opening quantity and checkbox', async () => {
  const plan = tradePlan('close', {legs: [{symbol:'BTC-13SEP99-105000-C-USDT', side:'Buy', qty:.004,
    expiry:'2099-09-13T08:00:00Z', strike:105000, option_type:'Call', reference_price:100}], max_net_cost_usd:-2});
  const page = await planPage(plan, url => url === '/api/trading/tasks'
    ? Promise.resolve(jsonResponse({execution_id:'execution-one', execution_status:'accepted'})) : Promise.reject(new Error('offline')));
  page.element('quantity').value = '100'; page.element('confirm').checked = false;
  await page.context.closeTrade();
  assert.deepEqual(JSON.parse(page.requests.find(item => item.url === '/api/trading/plans').options.body), {operation:'close'});
  assert.match(page.element('planLegs').innerHTML, /数量 0\.004 BTC/);
  assert.equal(page.element('planNetLimit').value, '-2');
  assert.equal(page.element('planSubmit').disabled, true);
  page.element('planConfirm').checked = true;
  await page.context.submitTradePlan();
  const body = JSON.parse(page.requests.find(item => item.url === '/api/trading/tasks').options.body);
  assert.equal(body.confirm_live, true); assert.equal(body.max_net_cost_usd, -2);
  assert.equal(body.quantity, undefined); assert.equal(body.strategy_mode, undefined);
});

test('expired plans, nonfinite limits, and changing selection cannot reuse confirmation', async () => {
  const page = await planPage();
  await page.context.openTrade();
  page.element('planConfirm').checked = true;
  page.element('planNetLimit').value = 'Infinity'; page.context.updatePlanControls();
  assert.equal(page.element('planSubmit').disabled, true);
  page.element('planNetLimit').value = '9'; page.element('planNetLimit').dispatch('input');
  assert.equal(page.element('planConfirm').checked, false);
  page.element('quantity').value = '.02'; page.element('quantity').dispatch('input');
  assert.equal(page.element('tradePlanDialog').open, false);
  await page.context.submitTradePlan();
  assert.equal(page.requests.filter(item => item.url === '/api/trading/tasks').length, 0);
  const expired = await planPage(tradePlan('open', {expires_at:'2000-01-01T00:00:00Z'}));
  await expired.context.openTrade(); expired.element('planConfirm').checked = true;
  await expired.context.submitTradePlan();
  assert.equal(expired.element('planConfirm').checked, false);
  assert.match(expired.element('planExpiry').textContent, /已过期/);
  assert.equal(expired.requests.filter(item => item.url === '/api/trading/tasks').length, 0);
});

test('lost submission response stays locked across empty recovery reads and recovers one task without another write', async () => {
  let recovered = null;
  const page = await planPage(tradePlan(), url => {
    if (url === '/api/trading/tasks') return Promise.reject(new Error('connection lost'));
    if (url.startsWith('/api/trading/tasks?')) return Promise.resolve(jsonResponse({items: recovered ? [recovered] : []}));
    return Promise.reject(new Error('offline'));
  });
  await page.context.openTrade(); page.element('planConfirm').checked = true;
  await page.context.submitTradePlan(); await new Promise(setImmediate);
  await page.context.openTrade(); await page.context.closeTrade(); await page.context.createRfq();
  assert.equal(page.requests.filter(item => item.url === '/api/trading/plans').length, 1);
  assert.equal(page.requests.filter(item => item.url === '/api/trading/tasks').length, 1);
  assert.equal(page.element('closeTrade').disabled, true);
  assert.match(page.element('executionPending').textContent, /提交结果待核对/);
  recovered = executionGroup();
  await page.context.recoverPendingExecution();
  assert.equal(page.element('closeTrade').disabled, true, 'accepted task remains exclusive');
  const terminal = executionGroup({execution_status:'stopped', can_stop:false});
  page.context.renderOrders({items:[], groups:[terminal], execution_active:false});
  assert.equal(page.element('closeTrade').disabled, false);
  assert.equal(page.requests.filter(item => item.url === '/api/trading/tasks').length, 1);
});

test('restored request hints recover after reload and do not turn an empty server response into permission to retry', async () => {
  const storage = new Map([['ic-pending-execution', JSON.stringify({request_id:'request-restored', plan_id:'old-plan'})]]);
  const page = dashboard({storage, request(url) {
    if (url === '/api/dashboard/orders') return Promise.resolve(jsonResponse({items:[], groups:[], execution_active:false}));
    if (url.startsWith('/api/trading/tasks?')) return Promise.resolve(jsonResponse({items:[]}));
    return Promise.reject(new Error('offline'));
  }});
  await new Promise(setImmediate);
  assert.equal(page.element('closeTrade').disabled, true);
  assert.ok(page.requests.some(item => item.url === '/api/trading/tasks?request_id=request-restored&plan_id=old-plan'));
  page.context.renderOrders({items:[], execution_active:true, groups:[executionGroup({request_id:'request-restored'})]});
  assert.equal(JSON.parse(storage.get('ic-pending-execution')), null);
  assert.match(page.element('activeExecutions').innerHTML, /停止执行/);
});

test('server closure proof unlocks unaccepted expired and missing plans, including old recovery hints', async (t) => {
  for (const hint of [{request_id:'request-restored', plan_id:'missing-plan'},
    {request_id:'request-restored', plan_id:'expired-plan', expires_at:'2000-01-01T00:00:00Z'}]) {
    await t.test(hint.plan_id, async () => {
      const storage = new Map([['ic-pending-execution', JSON.stringify(hint)]]);
      const page = dashboard({embedded:true, storage, request(url) {
        assert.equal(url, `/api/trading/tasks?request_id=${hint.request_id}&plan_id=${hint.plan_id}`);
        return Promise.resolve(jsonResponse({items:[], request_id:hint.request_id, plan_id:hint.plan_id,
          server_time:'2026-10-10T04:00:00Z', admission_active:false, admission_closed:true}));
      }});
      page.context.renderOrders({items:[], groups:[], execution_active:false});
      assert.equal(page.element('closeTrade').disabled, true);
      await page.context.recoverPendingExecution();
      assert.equal(JSON.parse(storage.get('ic-pending-execution')), null);
      assert.equal(page.element('closeTrade').disabled, false);
      assert.equal(page.element('executionPending').hidden, true);
      assert.equal(page.element('heroExecution').textContent, '当前无活动执行');
      assert.match(page.element('noticeText').textContent, /原方案未被接纳且已失效/);
      assert.equal(page.requests.filter(item => item.options?.method === 'POST').length, 0);
    });
  }
});

test('pending recovery requires matching identities and explicit complete server closure proof', async (t) => {
  const hint = {request_id:'request-restored', plan_id:'old-plan'};
  const proof = {items:[], ...hint, admission_active:false, admission_closed:true};
  const cases = [
    ['admission in progress', {admission_active:true}],
    ['plan remains open', {admission_closed:false}],
    ['missing closure proof', {admission_closed:undefined}],
    ['wrong request', {request_id:'another-request'}],
    ['wrong plan', {plan_id:'another-plan'}],
    ['nonempty result', {items:[{request_id:'another-request'}]}],
    ['missing result', {items:undefined}],
    ['nonboolean admission status', {admission_active:'false'}],
  ];
  for (const [name, overrides] of cases) await t.test(name, async () => {
    const storage = new Map([['ic-pending-execution', JSON.stringify(hint)]]);
    const page = dashboard({embedded:true, storage, request() { return Promise.resolve(jsonResponse({...proof, ...overrides})); }});
    page.context.renderOrders({items:[], groups:[], execution_active:false});
    await page.context.recoverPendingExecution();
    assert.deepEqual(JSON.parse(storage.get('ic-pending-execution')), hint);
    assert.equal(page.element('closeTrade').disabled, true);
    assert.equal(page.requests.filter(item => item.options?.method === 'POST').length, 0);
  });
});

test('a late closure proof cannot clear a newer pending request or plan', async (t) => {
  for (const replacement of [{request_id:'new-request', plan_id:'old-plan'}, {request_id:'request-restored', plan_id:'new-plan'}]) {
    await t.test(`${replacement.request_id}/${replacement.plan_id}`, async () => {
      let finish;
      const hint = {request_id:'request-restored', plan_id:'old-plan'};
      const storage = new Map([['ic-pending-execution', JSON.stringify(hint)]]);
      const page = dashboard({embedded:true, storage, request() { return new Promise(resolve => { finish = resolve; }); }});
      const recovery = page.context.recoverPendingExecution();
      page.context.persistPending(replacement);
      page.context.updateTradeControls();
      finish(jsonResponse({items:[], ...hint, admission_active:false, admission_closed:true}));
      await recovery;
      assert.deepEqual(JSON.parse(storage.get('ic-pending-execution')), replacement);
      assert.equal(page.element('closeTrade').disabled, true);
    });
  }
});

test('combination progress keeps real currency, signed net, missing amounts, historical details, and simulation distinct', () => {
  const {context, element} = dashboard({embedded:true});
  const active = executionGroup({net_amount:-1.2, currency:'USDC'});
  const terminal = executionGroup({execution_id:'completed-one', execution_status:'completed', can_stop:false,
    completed_legs:4, progress_ratio:1, amounts_complete:false, currency:null, simulated:true});
  context.renderOrders({items:[], execution_active:true, groups:[active, terminal]});
  assert.equal(element('ordersSummary').textContent, '执行中 1 组 · 最近结束 1 组');
  assert.match(element('activeExecutions').innerHTML, /完成 1 \/ 4 腿/);
  assert.match(element('activeExecutions').innerHTML, /-1\.2 USDC/);
  assert.match(element('activeExecutions').innerHTML, /目标 <b>0\.01/);
  assert.match(element('activeExecutions').innerHTML, /剩余 <b>0\.006/);
  assert.doesNotMatch(element('activeExecutions').innerHTML, /completed-one/);
  assert.match(element('executionHistoryBody').innerHTML, /模拟 · 无真实成交/);
  assert.doesNotMatch(element('executionHistoryBody').innerHTML, /金额与费用待核对/);
  const unknown = executionGroup({amounts_complete:false, gross_amount:null, fee_amount:null, net_amount:null,
    has_unknown:true, status_message:'<script>test</script>'});
  context.renderOrders({items:[], groups:[unknown], execution_active:true});
  assert.match(element('activeExecutions').innerHTML, /金额与费用待核对/);
  assert.match(element('activeExecutions').innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(element('activeExecutions').innerHTML, /\+0 USD|<script>/);
});

test('stop remains available during execution and requests only one stop without reversing partial fills', async () => {
  let finishStop;
  const page = dashboard({embedded:true, request(url) {
    if (url.endsWith('/stop')) return new Promise(resolve => { finishStop = resolve; });
    return Promise.reject(new Error('offline'));
  }});
  page.context.renderOrders({items:[], groups:[executionGroup()], execution_active:true});
  assert.equal(page.element('closeTrade').disabled, true);
  const stopping = page.context.stopExecution('execution-one');
  await page.context.stopExecution('execution-one');
  assert.equal(page.requests.filter(item => item.options?.method === 'POST').length, 1);
  assert.equal(page.requests[0].url, '/api/trading/tasks/execution-one/stop');
  assert.equal(page.requests[0].options.body, undefined);
  assert.match(page.element('activeExecutions').innerHTML, /停止请求待核对/);
  finishStop(jsonResponse({execution_status:'stopping'})); await stopping;
  page.context.renderOrders({items:[], groups:[executionGroup({execution_status:'partial', can_stop:false})], execution_active:false});
  assert.match(page.element('executionHistoryBody').innerHTML, /部分成交 · 已结束/);
  assert.match(page.element('executionHistoryBody').innerHTML, /已成 <b>0\.004/);
  assert.equal(page.element('closeTrade').disabled, false);
});

test('ambiguous stop failures wait for a fresh running snapshot before an idempotent retry', async (t) => {
  for (const failure of ['network', '503']) await t.test(failure, async () => {
    let attempts = 0, finish;
    const page = dashboard({embedded:true, request(url) {
      assert.equal(url, '/api/trading/tasks/execution-one/stop');
      if (++attempts === 1) return failure === 'network' ? Promise.reject(new Error('connection lost'))
        : Promise.resolve({ok:false, status:503, text:async () => JSON.stringify({detail:'unavailable'})});
      return new Promise(resolve => { finish = resolve; });
    }});
    const running = () => ({items:[], groups:[executionGroup()], execution_active:true});
    page.context.renderOrders(running());
    await page.context.stopExecution('execution-one');
    assert.match(page.element('activeExecutions').innerHTML, /data-stop="execution-one" disabled>停止请求待核对/);
    await page.context.stopExecution('execution-one');
    assert.equal(attempts, 1, 'failed write alone does not authorize a retry');
    page.context.renderOrders(running());
    assert.match(page.element('activeExecutions').innerHTML, /data-stop="execution-one" >重试停止/);
    assert.match(page.element('activeExecutions').innerHTML, /已成 <b>0\.004/);
    const retry = page.context.stopExecution('execution-one');
    page.context.renderOrders(running());
    await page.context.stopExecution('execution-one');
    assert.equal(attempts, 2, 'the retry also blocks concurrent clicks');
    finish(jsonResponse(executionGroup({execution_status:'stopping', can_stop:false})));
    await retry;
    page.context.renderOrders(running());
    assert.match(page.element('activeExecutions').innerHTML, /data-stop="execution-one" disabled>已请求停止/);
    await page.context.stopExecution('execution-one');
    assert.equal(attempts, 2, 'an acknowledged stop stays locked while awaiting the terminal snapshot');
    assert.equal(page.element('closeTrade').disabled, true);
    page.context.renderOrders({items:[], groups:[executionGroup({execution_status:'partial', can_stop:false})], execution_active:false});
    assert.match(page.element('executionHistoryBody').innerHTML, /部分成交 · 已结束/);
    assert.match(page.element('executionHistoryBody').innerHTML, /已成 <b>0\.004/);
  });
});

test('a stop failure followed by a server stop marker never offers another stop', async () => {
  const page = dashboard({embedded:true, request() { return Promise.reject(new Error('connection lost')); }});
  page.context.renderOrders({items:[], groups:[executionGroup()], execution_active:true});
  await page.context.stopExecution('execution-one');
  page.context.renderOrders({items:[], groups:[executionGroup({stop_requested_at:'2026-10-10T04:00:01Z'})], execution_active:true});
  assert.match(page.element('activeExecutions').innerHTML, /data-stop="execution-one" disabled>已请求停止/);
  await page.context.stopExecution('execution-one');
  assert.equal(page.requests.length, 1);
});

test('RFQ uses independent quote consent and preserves all button locks through polling', async () => {
  let finish;
  const page = await planPage(tradePlan(), url => url === '/api/rfq/execute'
    ? new Promise(resolve => { finish = resolve; }) : Promise.reject(new Error('offline')));
  const legs = strategyMarket('short_strangle').preview.legs;
  const state = {rfq_id:'rfq-one', status:'Active', strategy_mode:'short_strangle', legs,
    quotes:[{quoteId:'quote-one', quoteSellList:legs.map(leg => ({symbol:leg.symbol, qty:.01, price:600}))}]};
  page.context.renderRfq(state); page.element('confirm').checked = true;
  const event = {currentTarget:{dataset:{rfq:'rfq-one', quote:'quote-one', side:'Sell'}}};
  await page.context.executeRfq(event);
  assert.equal(page.element('rfqConfirmSubmit').disabled, true);
  assert.match(page.element('rfqConfirmSummary').innerHTML, /亏损无上限/);
  await page.context.submitRfqConfirmation();
  assert.equal(page.requests.filter(item => item.url === '/api/rfq/execute').length, 0);
  page.element('rfqConfirm').checked = true;
  const submitting = page.context.submitRfqConfirmation();
  page.context.renderRfq(state);
  assert.match(page.element('rfqQuotes').innerHTML, /data-executable="true" disabled/);
  for (const id of ['openTrade','closeTrade','rfqCreate','rfqCancel']) assert.equal(page.element(id).disabled, true);
  await page.context.executeRfq(event); await page.context.openTrade(); await page.context.closeTrade();
  assert.equal(page.requests.filter(item => item.url === '/api/rfq/execute').length, 1);
  finish(jsonResponse({status:'Filled'})); await submitting;
});
