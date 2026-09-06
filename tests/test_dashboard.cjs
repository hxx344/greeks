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

function execution(id, second, side, qty, price, fee, close = false, extra = {}) {
  return {exec_id: id, exec_time: `2026-09-04T12:00:${String(second).padStart(2, '0')}Z`,
    symbol: 'BTC-OPTION', side, exec_qty: qty, exec_price: price, exec_fee: fee,
    fee_currency: 'USDT', reduce_only: close, order_link_id: `ic-${close ? 'close-' : ''}${id}-0`, ...extra};
}

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
