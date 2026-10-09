// Only the workbench's isolated proxy iframe may negotiate page activity.
(() => {
  'use strict';
  const location = window.location;
  const embedded = window.parent !== window && /^p-[a-f0-9]{24}\.hub\.localhost$/.test(location.hostname)
    && ['http:', 'https:'].includes(location.protocol);
  const origin = embedded ? `${location.protocol}//hub.localhost${location.port ? `:${location.port}` : ''}` : null;
  const tasks = new Map();
  let ready = false, active = !embedded, background = !embedded, started = false;
  const online = () => window.navigator.onLine !== false;
  const foreground = () => active && document.visibilityState !== 'hidden';
  const allowed = () => online() && (!embedded || ready) && (foreground() || background);
  const mode = () => !allowed() ? 'paused' : foreground() ? 'foreground' : 'background';
  let previousMode = mode();
  const post = (message) => { if (embedded) window.parent.postMessage({channel: 'project-hub', version: 1, ...message}, origin); };
  const clear = task => { if (task.timer != null) clearTimeout(task.timer); task.timer = null; };
  function schedule(task) {
    clear(task);
    if (started && allowed() && !task.running) {
      task.timer = setTimeout(() => { task.timer = null; task.load(); }, foreground() ? task.period : Math.max(30000, task.period));
    }
  }
  function synchronize(force = false) {
    const next = mode();
    const changed = next !== previousMode;
    const catchup = next !== 'paused' && (force || previousMode === 'paused' || (next === 'foreground' && changed));
    previousMode = next;
    if (!started || (!changed && !force)) return;
    for (const task of tasks.values()) {
      clear(task);
      // Invalidate old reads; never abort or retry a trading request.
      task.generation++;
      if (task.running) { task.pending = catchup; continue; }
      if (catchup) task.load(); else schedule(task);
    }
  }
  const bridge = {
    register(name, period, load) { tasks.set(name, {period, load, generation: 0, running: false, pending: false, timer: null}); },
    start() {
      started = true;
      if (allowed()) for (const task of tasks.values()) task.load();
    },
    begin(name) {
      const task = tasks.get(name);
      if (!task || !allowed()) return null;
      if (task.running) { task.pending = true; task.generation++; return null; }
      clear(task);
      task.running = true;
      const generation = ++task.generation;
      return {
        current: () => allowed() && generation === task.generation,
        finish() {
          task.running = false;
          const pending = task.pending;
          task.pending = false;
          if (pending && allowed()) task.load(); else schedule(task);
        },
      };
    },
    changed() {
      if (ready) post({type: 'changed', scope: 'summary'});
      synchronize(true);
    },
  };
  window.ProjectHub = bridge;
  if (embedded) {
    window.addEventListener('message', event => {
      if (event.source !== window.parent || event.origin !== origin) return;
      const data = event.data;
      if (!data || typeof data !== 'object' || Array.isArray(data) || data.channel !== 'project-hub' || data.version !== 1) return;
      if (data.type === 'ready' && data.role === 'host') {
        ready = true;
        post({type: 'ready', role: 'module', capabilities: ['activity', 'changed']});
      } else if (ready && data.type === 'activity' && typeof data.active === 'boolean'
                 && (data.backgroundUpdates === undefined || typeof data.backgroundUpdates === 'boolean')) {
        active = data.active;
        background = data.backgroundUpdates === true;
        synchronize();
      }
    });
    post({type: 'ready', role: 'module'});
  }
  document.addEventListener('visibilitychange', () => synchronize());
  window.addEventListener('online', () => synchronize(true));
  window.addEventListener('offline', () => synchronize());
  window.addEventListener('focus', () => { if (foreground()) synchronize(true); });
  window.addEventListener('pageshow', event => { if (event.persisted) synchronize(true); });
})();
