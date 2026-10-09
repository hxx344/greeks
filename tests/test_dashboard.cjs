const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function dashboard({embedded = false, request} = {}) {
  const elements = new Map();
  const pending = [];
  const requests = [], events = new Map(), messages = [];
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
    window: {location: {hostname: embedded ? 'p-0123456789abcdef01234567.hub.localhost' : 'localhost', protocol: 'http:', port: '8000'}, parent, navigator: {onLine: true}, localStorage: {getItem() { return null; }, setItem() {}}, addEventListener(name, callback) { events.set(name, callback); }},
    setTimeout() { return 1; }, clearTimeout() {},
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
  return {context, element, pending, requests, events, messages, message};
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
  await new Promise(setImmediate);
  element('healthUnavailable').style.display = 'none';
  element('healthContent').style.display = 'grid';
  await context.loadAccount();
  assert.equal(element('healthUnavailable').style.display, 'block');
  assert.equal(element('healthContent').style.display, 'none');
  assert.equal(element('healthMode').textContent, '账户连接异常');
  assert.match(element('positionPayoffContent').innerHTML, /持仓更新失败/);
});

test('real dashboard initialization waits for proxy activity across all four streams', async () => {
  const {context, requests, message} = dashboard({embedded: true});
  assert.equal(requests.length, 0);
  message({type: 'ready', role: 'host'});
  assert.equal(requests.length, 0);
  message({type: 'activity', active: true, backgroundUpdates: true});
  assert.deepEqual(requests.map(item => item.url.split('?')[0]).sort(),
    ['/api/dashboard/account', '/api/dashboard/market', '/api/dashboard/performance', '/api/rfq/status'].sort());
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
  for (const [action, endpoint] of [['openTrade', '/api/trading/open'], ['createRfq', '/api/rfq/create']]) {
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
  assert.equal(writes[0].url, '/api/trading/close');
  assert.deepEqual(writes[0].body, {confirm_live:true});
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
