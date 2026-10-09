const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(`${__dirname}/../app/static/hub.js`, 'utf8');
const hostOrigin = 'http://hub.localhost:3100';
function page({embedded = true, hostname = 'p-0123456789abcdef01234567.hub.localhost'} = {}) {
  const events = new Map(), timers = new Map(), messages = [];
  let nextTimer = 0;
  const parent = {postMessage: (message, origin) => messages.push({message, origin})};
  const win = {location: {hostname: embedded ? hostname : 'localhost', protocol: 'http:', port: '3100'},
    navigator: {onLine: true}, addEventListener: (name, callback) => events.set(name, callback), parent};
  if (!embedded) win.parent = win;
  const doc = {visibilityState: 'visible', addEventListener: (name, callback) => events.set(name, callback)};
  const context = vm.createContext({window: win, document: doc,
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); }});
  vm.runInContext(source, context);
  const emit = (name, event = {}) => events.get(name)?.(event);
  const message = (data, extras = {}) => emit('message', {data: {channel: 'project-hub', version: 1, ...data}, source: parent, origin: hostOrigin, ...extras});
  const ready = () => message({type: 'ready', role: 'host'});
  const activity = (active, backgroundUpdates) => message({type: 'activity', active, ...(backgroundUpdates === undefined ? {} : {backgroundUpdates})});
  function fire() { const first = timers.entries().next().value; assert.ok(first); timers.delete(first[0]); first[1].callback(); }
  return {bridge: win.ProjectHub, win, doc, events, timers, messages, emit, message, ready, activity, fire};
}

function reader(page, name = 'market', period = 10000) {
  const reads = [];
  const run = () => { const token = page.bridge.begin(name); if (token) reads.push(token); };
  page.bridge.register(name, period, run);
  return {reads, run};
}

test('proxy iframe stays inactive until exact parent/origin handshake and valid activity', () => {
  const p = page(), r = reader(p);
  p.bridge.start();
  assert.equal(r.reads.length, 0);
  assert.equal(p.messages.length, 1);
  assert.equal(p.messages[0].origin, hostOrigin);
  assert.equal(p.messages[0].message.capabilities, undefined);
  p.activity(true, true);
  p.message({type: 'ready', role: 'host'}, {origin: 'http://hub.localhost:3000'});
  p.message({type: 'ready', role: 'host'}, {source: {}});
  p.activity(true, true);
  assert.equal(r.reads.length, 0);
  p.ready();
  assert.equal(r.reads.length, 0);
  assert.deepEqual([...p.messages.at(-1).message.capabilities], ['activity', 'changed']);
  p.message({type: 'activity', active: true, backgroundUpdates: 'true'});
  p.message({type: 'activity', active: 'true'});
  p.message({type: 'activity', active: true, version: 2});
  assert.equal(r.reads.length, 0);
  p.activity(true, true);
  assert.equal(r.reads.length, 1);
});

test('lookalike hosts and independent windows never establish the bridge', () => {
  for (const hostname of ['other.hub.localhost', 'p-0123456789abcdef01234567.hub.localhost.evil', 'p-short.hub.localhost']) {
    const p = page({hostname}), r = reader(p);
    p.bridge.start();
    assert.equal(p.messages.length, 0);
    assert.equal(r.reads.length, 1);
    assert.equal(p.events.has('message'), false);
  }
  const p = page({embedded: false});
  p.bridge.changed();
  assert.equal(p.messages.length, 0);
});

test('all four streams keep foreground periods and background intervals of at least 30 seconds', () => {
  const p = page();
  const streams = [reader(p, 'market', 10000), reader(p, 'account', 15000), reader(p, 'rfq', 3000), reader(p, 'performance', 30000)];
  p.bridge.start(); p.ready(); p.activity(true, true);
  for (const r of streams) r.reads[0].finish();
  assert.deepEqual([...p.timers.values()].map(item => item.delay).sort((a,b) => a-b), [3000, 10000, 15000, 30000]);
  p.activity(false, true);
  assert.equal(p.timers.size, 4);
  assert.ok([...p.timers.values()].every(item => item.delay >= 30000));
  p.fire();
  assert.equal(streams.reduce((sum, r) => sum + r.reads.length, 0), 5);
});

test('legacy host pauses hidden iframe; direct pages continue slowly in background', () => {
  const p = page(), r = reader(p);
  p.bridge.start(); p.ready(); p.activity(true);
  r.reads[0].finish();
  p.activity(false);
  assert.equal(p.timers.size, 0);
  r.run(); assert.equal(r.reads.length, 1);
  p.activity(true);
  assert.equal(r.reads.length, 2);
  const direct = page({embedded: false}), d = reader(direct);
  direct.bridge.start(); d.reads[0].finish();
  direct.doc.visibilityState = 'hidden'; direct.emit('visibilitychange');
  assert.equal([...direct.timers.values()][0].delay, 30000);
});

test('offline pauses reads, recovery coalesces and invalidates late responses without overlap', () => {
  const p = page(), r = reader(p);
  p.bridge.start(); p.ready(); p.activity(true, true);
  const old = r.reads[0];
  assert.equal(old.current(), true);
  p.win.navigator.onLine = false; p.emit('offline');
  assert.equal(old.current(), false);
  r.run(); assert.equal(r.reads.length, 1);
  p.win.navigator.onLine = true; p.emit('online');
  p.emit('focus'); p.emit('focus');
  assert.equal(r.reads.length, 1);
  old.finish();
  assert.equal(r.reads.length, 2);
  assert.equal(r.reads[1].current(), true);
  assert.equal(p.timers.size, 0);
  r.reads[1].finish(); assert.equal(p.timers.size, 1);
});

test('changed notifications require handshake and only invalidate reads', () => {
  const p = page(), r = reader(p);
  p.bridge.start(); p.bridge.changed();
  assert.equal(p.messages.filter(item => item.message.type === 'changed').length, 0);
  p.ready(); p.activity(true, true);
  p.bridge.changed();
  assert.equal(p.messages.at(-1).message.scope, 'summary');
  assert.equal(r.reads.length, 1);
  assert.equal(r.reads[0].current(), false);
  r.reads[0].finish(); assert.equal(r.reads.length, 2);
});

test('foreground recovery invalidates delayed background data and queues exactly one fresh read', () => {
  const p = page(), r = reader(p);
  p.bridge.start(); p.ready(); p.activity(false, true);
  const background = r.reads[0];
  p.activity(true, true); p.activity(true, true);
  assert.equal(background.current(), false);
  assert.equal(r.reads.length, 1);
  background.finish();
  assert.equal(r.reads.length, 2);
  assert.equal(r.reads[1].current(), true);
});
