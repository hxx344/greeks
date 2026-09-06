const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/position-payoff.js`, 'utf8'), ctx);
const position = (strike, type, side, size = .1, avg_price = 100, date = '13SEP26', suffix = '-USDT') => ({symbol: `BTC-${date}-${strike}-${type}${suffix}`, side, size, avg_price, unrealised_pnl: 2, source: 'bybit'});
const condor = () => ctx.positionPayoffGroups([
  position(90000, 'P', 'Buy'), position(95000, 'P', 'Sell', .1, 600),
  position(105000, 'C', 'Sell', .1, 600), position(110000, 'C', 'Buy'),
]).groups[0];

test('actual entry prices and sizes produce exact condor payoff and break-even points', () => {
  const group = condor();
  const model = ctx.analyzePositionPayoff(group, 100000);
  assert.equal(model.current, 100);
  assert.equal(model.max, 100);
  assert.equal(model.min, -400);
  assert.deepEqual(Array.from(model.breaks), [94000, 106000]);
  assert.equal(group.floating, 8);
  assert.equal(model.condor, true);
  assert.equal(model.stage, '最大盈利区');
  for (const [spot, stage] of [[94500,'盈利缓冲区'],[94000,'盈亏平衡'],[94050,'盈亏平衡附近'],[93000,'亏损扩大区'],[88000,'最大亏损区'],[112000,'最大亏损区']]) {
    assert.equal(ctx.analyzePositionPayoff(group, spot).stage, stage);
  }
});

test('partial legs and unequal sizes are calculated without assuming a complete iron condor', () => {
  const group = condor();
  group.legs[0].quantity = .05;
  const model = ctx.analyzePositionPayoff(group, 85000);
  assert.equal(model.condor, false);
  assert.equal(model.current, -645);
  group.legs = [group.legs[2]];
  const naked = ctx.analyzePositionPayoff(group, 100000);
  assert.equal(naked.min, -Infinity);
  assert.equal(naked.max, 60);
  assert.deepEqual(Array.from(naked.breaks), [105600]);
});

test('long calls have unbounded upside and puts have finite loss over nonnegative BTC prices', () => {
  const group = ctx.positionPayoffGroups([position(100000,'C','Buy',.2,500)]).groups[0];
  const model = ctx.analyzePositionPayoff(group, 102000);
  assert.equal(model.max, Infinity);
  assert.equal(model.min, -100);
  assert.equal(model.current, 300);
  const put = ctx.positionPayoffGroups([position(100000,'P','Sell',.2,500)]).groups[0];
  assert.equal(ctx.analyzePositionPayoff(put, 100000).min, -19900);
});

test('expiry, currency and simulated positions stay in separate groups; invalid rows are explicit', () => {
  const rows = [position(95000,'P','Buy'), position(95000,'P','Buy',.1,100,'20SEP26'),
    position(95000,'P','Buy',.1,100,'13SEP26',''), {...position(95000,'P','Buy'),source:'demo'},
    {...position(95000,'P','Buy'),avg_price:null}, position(95000,'P','Buy',.1,100,'31SEP26'),
    {...position(95000,'P','Buy'),symbol:'ETH-13SEP26-4000-C-USDT'}, position(95000,'P','Buy',0)];
  const result = ctx.positionPayoffGroups(rows);
  assert.equal(result.groups.length, 4);
  assert.equal(result.excluded, 3);
  assert.equal(result.groups[0].expiry, Date.UTC(2026,8,13,8));
});

test('missing spot does not fabricate a stage and far spot remains in chart domain', () => {
  const group = condor();
  for (const spot of [null, undefined, NaN, 0]) {
    const model = ctx.analyzePositionPayoff(group, spot);
    assert.equal(model.current, null);
    assert.equal(model.stage, '等待现价');
  }
  const model = ctx.analyzePositionPayoff(group, 200000);
  assert.ok(model.high > 200000);
  assert.ok(model.points.some(point => point.price === 200000));
});

test('expiry phase switches at exactly 08:00 UTC', () => {
  const expiry = Date.UTC(2026,8,13,8);
  assert.match(ctx.positionExpiryLabel(expiry, expiry - 3600000), /临近到期.*1小时/);
  assert.match(ctx.positionExpiryLabel(expiry, expiry - 2 * 86400000), /持仓中.*2天/);
  assert.match(ctx.positionExpiryLabel(expiry, expiry), /已到期/);
});
