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
const ranges = model => Array.from(model.maxRanges, range => Array.from(range));
const analyzeLegs = (legs, spot = null) => ctx.analyzePositionPayoff({legs}, spot);
const leg = (strike, type, quantity, entry = 0) => ({strike, type, quantity, entry});
const num = (value, digits = 2) => Number(value || 0).toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const money = value => `$${Number(value || 0).toLocaleString('en-US', {maximumFractionDigits: 2})}`;
ctx.num = num;
ctx.money = money;

function renderRows(rows, options = {}) {
  const elements = {positionPayoffContent: {clientWidth: options.width || 300}, positionPayoffCount: {}};
  const window = {__positionSnapshot: rows, __positionMarket: {price: 82600, timestamp: Date.now(), staleSeconds: 30}, ...options};
  const renderCtx = vm.createContext({window, $: id => elements[id], num, money, esc: String});
  vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/position-payoff.js`, 'utf8'), renderCtx);
  renderCtx.renderPositionPayoff();
  return elements.positionPayoffContent.innerHTML;
}

test('actual entry prices and sizes produce exact condor payoff and break-even points', () => {
  const group = condor();
  const model = ctx.analyzePositionPayoff(group, 100000);
  assert.equal(model.current, 100);
  assert.equal(model.max, 100);
  assert.deepEqual(ranges(model), [[95000, 105000]]);
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
  assert.deepEqual(ranges(model), []);
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

test('short strangle max range uses strikes, independently of break-even prices and quantity imbalance', () => {
  const legs = [leg(82500, 'P', -1, 267.5), leg(82750, 'C', -1, 267.5)];
  const model = analyzeLegs(legs, 82600);
  assert.equal(model.max, 535);
  assert.deepEqual(Array.from(model.breaks), [81965, 83285]);
  assert.deepEqual(ranges(model), [[82500, 82750]]);
  assert.equal(model.condor, false);
  for (const spot of [82500, 82600, 82750]) assert.equal(analyzeLegs(legs, spot).stage, '最大盈利区');
  assert.equal(analyzeLegs(legs, 82365).stage, '到期盈利区');
  legs[1].quantity = -.6;
  assert.deepEqual(ranges(analyzeLegs(legs)), [[82500, 82750]]);
});

test('residual single legs have exact left, right and boundary-point maxima over nonnegative prices', () => {
  assert.deepEqual(ranges(analyzeLegs([leg(100, 'C', -1, 5)])), [[0, 100]]);
  assert.deepEqual(ranges(analyzeLegs([leg(100, 'P', -1, 5)])), [[100, Infinity]]);
  const put = analyzeLegs([leg(100, 'P', 1, 5)]);
  assert.equal(put.max, 95);
  assert.deepEqual(ranges(put), [[0, 0]]);
  assert.equal(put.low, 0);
  assert.ok(put.points.some(point => point.price === 0 && point.pnl === 95));
  assert.deepEqual(ranges(analyzeLegs([leg(100, 'C', -1, 5), leg(100, 'P', -1, 5)])), [[100, 100]]);
  assert.deepEqual(ranges(analyzeLegs([leg(100, 'C', 1), leg(200, 'C', -2)])), [[200, 200]]);
});

test('flat positions retain the entire domain, including zero and negative highest payoff', () => {
  for (const [longEntry, shortEntry, maximum] of [[5, 10, 5], [10, 10, 0], [20, 10, -10]]) {
    const model = analyzeLegs([leg(100, 'C', 1, longEntry), leg(100, 'C', -1, shortEntry)], 150);
    assert.equal(model.max, maximum);
    assert.deepEqual(ranges(model), [[0, Infinity]]);
    if (maximum <= 0) assert.doesNotMatch(model.stage, /最大盈利/);
  }
});

test('all disconnected global maximum plateaus and isolated points are retained', () => {
  const plateaus = [[80, 1], [90, -1], [100, -1], [110, 2], [120, -1], [130, -1], [140, 1]].map(([strike, quantity]) => leg(strike, 'C', quantity));
  const model = analyzeLegs(plateaus, 115);
  assert.equal(model.max, 10);
  assert.deepEqual(ranges(model), [[90, 100], [120, 130]]);
  for (const spot of [90, 95, 100, 120, 125, 130]) assert.equal(analyzeLegs(plateaus, spot).stage, '最大盈利区');
  assert.equal(model.stage, '到期盈利区');
  const peaks = [[80, 1], [90, -2], [100, 2], [110, -2], [120, 1]].map(([strike, quantity]) => leg(strike, 'C', quantity));
  assert.deepEqual(ranges(analyzeLegs(peaks)), [[90, 90], [110, 110]]);
  const unequalPeaks = peaks.map(item => ({...item}));
  unequalPeaks[2].quantity = 1.9;
  assert.deepEqual(ranges(analyzeLegs(unequalPeaks)), [[90, 90]]);
});

test('quantity-relative tolerances distinguish tiny true slopes from floating point cancellation', () => {
  for (const quantity of [1e-10, 1e-18]) {
    const long = analyzeLegs([leg(100, 'C', quantity, 5)]);
    assert.equal(long.max, Infinity);
    assert.deepEqual(ranges(long), []);
    const short = analyzeLegs([leg(100, 'C', -quantity, 5)]);
    assert.equal(short.min, -Infinity);
    assert.deepEqual(ranges(short), [[0, 100]]);
    assert.deepEqual(ranges(analyzeLegs([leg(100, 'P', -quantity, 5)])), [[100, Infinity]]);
  }
  const tilted = [leg(82500, 'P', -1, 267.5), leg(82750, 'C', -1, 267.5), leg(1, 'C', 1e-12)];
  assert.deepEqual(ranges(analyzeLegs(tilted)), [[82750, 82750]]);
  const lowerPlateau = [leg(100000, 'P', -1, 267.5), leg(100010, 'C', -1, 267.5), leg(100005, 'C', 1e-12)];
  assert.deepEqual(ranges(analyzeLegs(lowerPlateau)), [[100010, 100010]]);
  const residual = analyzeLegs([leg(100, 'C', 1), leg(100, 'C', -1 + 1e-10)]);
  assert.equal(residual.max, Infinity);
  const cancelled = analyzeLegs([leg(100, 'C', .1), leg(100, 'C', .2), leg(100, 'C', -.3)]);
  assert.deepEqual(ranges(cancelled), [[0, Infinity]]);
});

test('SVG exposes concrete ranges and separates maximum, spot and risk labels at 300px and desktop width', () => {
  const model = analyzeLegs([leg(82500, 'P', -1, 267.5), leg(82750, 'C', -1, 267.5)], 82365);
  for (const width of [300, 920]) {
    const svg = ctx.positionPayoffSvg(model, 82365, 0, width);
    assert.match(svg, /aria-label="[^"]*最大收益对应 BTC 到期价格（USD）：82,500–82,750/);
    assert.match(svg, /class="pp-max-label"[^>]*>[^<]*82,500–82,750/);
    assert.match(svg, /class="pp-max-band"/);
    assert.match(svg, /class="pp-max-line"/);
    const ys = ['pp-max-label', 'pp-spot-label', 'pp-risk-note'].map(name => Number(new RegExp(`class="${name}"[^>]* y="([\\d.]+)"`).exec(svg)[1]));
    assert.ok(ys[1] - ys[0] >= 18);
    assert.ok(ys[2] - ys[1] >= 18);
    assert.doesNotMatch(svg, /(?:NaN|undefined)/);
  }
});

test('SVG labels preserve unbounded and single-point domains instead of showing viewport bounds', () => {
  const shortPut = analyzeLegs([leg(100, 'P', -1, 5)], 110);
  const putSvg = ctx.positionPayoffSvg(shortPut, 110, 0, 300);
  assert.match(putSvg, /class="pp-max-label"[^>]*>[^<]*≥100/);
  assert.match(putSvg, /class="pp-max-line"[^>]*d="[^"]* L[^ ]+ L/);
  const longPut = analyzeLegs([leg(100, 'P', 1, 5)]);
  const pointSvg = ctx.positionPayoffSvg(longPut, null, 0, 300);
  assert.match(pointSvg, /class="pp-max-label"[^>]*>[^<]*最大收益价格/);
  assert.match(pointSvg, /0（单点）/);
  assert.match(pointSvg, /class="pp-max-point"/);
  const longCall = analyzeLegs([leg(100, 'C', 1, 5)]);
  const unlimitedSvg = ctx.positionPayoffSvg(longCall, null, 0, 300);
  assert.match(unlimitedSvg, /收益无上限 · 无有限最大收益区间/);
  assert.doesNotMatch(unlimitedSvg, /class="pp-max-(?:band|line|point)"/);
});

test('rendered summary is direct, retains negative highest PNL and does not invent a maximum for empty or failed states', () => {
  const date = '13SEP30';
  const strangle = renderRows([position(82500, 'P', 'Sell', 1, 267.5, date), position(82750, 'C', 'Sell', 1, 267.5, date)]);
  assert.match(strangle, /class="pp-max-summary"[^>]*><span>最大收益对应 BTC 到期价格 · USD<\/span><strong>82,500–82,750<\/strong>/);
  assert.match(strangle, /最大到期收益<\/span><strong class="">\$535<\/strong>/);
  for (const entry of [10, 20]) {
    const flat = renderRows([position(100, 'C', 'Buy', 1, entry, date), position(100, 'C', 'Sell', 1, 10, date)]);
    assert.match(flat, /最高盈亏对应 BTC 到期价格 · USD/);
    assert.match(flat, /全部非负价格（0–∞）/);
    assert.match(flat, new RegExp(`最高到期盈亏<\\/span><strong class="">\\$${10 - entry}<\\/strong>`));
    assert.doesNotMatch(flat, /最大收益对应|最大盈利区/);
  }
  assert.match(renderRows([]), /暂无可计算/);
  assert.doesNotMatch(renderRows([]), /pp-max-summary|<svg/);
  assert.match(renderRows(undefined), /等待持仓同步/);
  assert.doesNotMatch(renderRows([position(100, 'C', 'Buy')], {__positionError: true}), /pp-max-summary|<svg/);
});

test('multiple maximum intervals remain complete in summary and aria text when chart labels are compact', () => {
  const date = '13SEP30';
  const calls = [[80, 1], [90, -1], [100, -1], [110, 2], [120, -1], [130, -1], [140, 1]];
  const html = renderRows(calls.map(([strike, quantity]) => position(strike, 'C', quantity > 0 ? 'Buy' : 'Sell', Math.abs(quantity), 0, date)));
  assert.match(html, /<strong>90–100；120–130<\/strong>/);
  assert.match(html, /aria-label="[^"]*90–100；120–130/);
  const model = analyzeLegs([leg(100, 'C', -1, 5)], 100);
  model.maxRanges = Array.from({length: 10}, (_, i) => [80000 + i * 200, 80100 + i * 200]);
  const svg = ctx.positionPayoffSvg(model, 100, 0, 300);
  assert.match(svg, /共 10 段/);
  assert.match(svg, /完整到期价格见上方/);
  assert.match(svg, /aria-label="[^"]*80,000–80,100[^"]*81,800–81,900/);
});
