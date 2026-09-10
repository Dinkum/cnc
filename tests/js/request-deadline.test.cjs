const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = name => fs.readFileSync(path.join(__dirname, '../../app/static/js', name), 'utf8');
const segment = (text, start, end) => text.slice(text.indexOf(start), text.indexOf(end, text.indexOf(start)));

async function simulate(kind, fault) {
  let now = 0, nextId = 0, calls = 0, active = 0, maxActive = 0;
  const successes = [], timers = new Map();
  const delay = (callback, ms) => {
    const id = ++nextId;
    timers.set(id, { at: now + ms, callback });
    return id;
  };
  const fetch = (_url, { signal }) => {
    calls += 1;
    active += 1;
    maxActive = Math.max(maxActive, active);
    const payload = { pending: false, host_metrics: {} };
    if (calls === 1 && fault) {
      const pending = () => new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          active -= 1;
          // Browser body readers can lose the original abort reason.
          reject(fault === 'body' ? new DOMException('Body aborted', 'AbortError') : signal.reason);
        }, { once: true });
      });
      return fault === 'body' ? Promise.resolve({ ok: true, json: pending }) : pending();
    }
    return new Promise(resolve => delay(() => {
      active -= 1;
      resolve({ ok: true, json: async () => payload });
    }, 50));
  };
  const context = vm.createContext({
    window: { setTimeout: delay, clearTimeout: id => timers.delete(id) },
    fetch, AbortController, DOMException, console: { error() {} },
    document: { hidden: false, querySelector: () => ({ dataset: { tab: 'home' } }) },
    STATUS_POLL_TABS: new Set(['home', 'outputs', 'settings']), SERVER_LOAD_POLL_INTERVAL_MS: 15000,
    serverLoadPollTimer: null, serverLoadPollInFlight: false, hostMutationUiBusy: 0,
    updateFlowActive: () => false, readJsonResponse: response => response.json(),
    renderHostMetricsFromStatus: () => successes.push(now), renderOutputsFromStatus() {},
    renderHomeFromStatus() {}, renderUpdateVersion() {}, renderUpdate() {},
    outputHasRuntimeSignals: () => true, runtimeRefreshInFlight: null,
    runtimeRefreshAbortController: null, runtimeRefreshFailures: 0, runtimeRefreshTimer: null,
    backendId: 1, renderPendingRuntimeState() {}, renderRuntimeSignals: () => successes.push(now),
  });
  vm.runInContext(source('cnc-ui.js') + '\nconst { withRequestDeadline } = window.CNCUI;', context);
  if (kind === 'dashboard') {
    vm.runInContext(segment(source('dashboard.js'), '  const scheduleServerLoadPoll =', '  const refreshVisibleTabStatus =') + '\npollServerLoadStatus();', context);
  } else {
    vm.runInContext(segment(source('output-detail.js'), '  const runtimeRefreshDelay =', '  afterInitialLoad(() => {') + '\nrefreshRuntimeSignals().then(p => scheduleRuntimeRefresh(runtimeRefreshDelay(p))).catch(() => scheduleRuntimeRefresh());', context);
  }
  const flush = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };
  await flush();
  while (timers.size) {
    const [id, task] = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
    if (task.at > 120000) break;
    now = task.at;
    timers.delete(id);
    task.callback();
    await flush();
  }
  return { successes, active, maxActive };
}

for (const kind of ['dashboard', 'runtime']) {
  for (const fault of [false, 'headers', 'body']) {
    test(`${kind} polling recovers from ${fault || 'no'} stall without overlapping`, async () => {
      const result = await simulate(kind, fault);
      assert.equal(result.maxActive, 1);
      assert.equal(result.active, 0);
      assert.equal(result.successes.length, fault ? (kind === 'dashboard' ? 5 : 4) : 8);
      assert.equal(result.successes[0], fault ? (kind === 'dashboard' ? 45050 : 60050) : 50);
    });
  }
}

test('deadline clears on completion and preserves deliberate cancellation', async () => {
  const timers = new Map();
  let id = 0;
  const context = vm.createContext({
    window: { setTimeout: callback => { timers.set(++id, callback); return id; }, clearTimeout: key => timers.delete(key) },
    AbortController, DOMException,
  });
  vm.runInContext(source('cnc-ui.js'), context);
  const { withRequestDeadline } = context.window.CNCUI;
  assert.equal(await withRequestDeadline(async () => 42), 42);
  assert.equal(timers.size, 0);
  const controller = new AbortController();
  const request = withRequestDeadline(signal => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  }), { controller });
  controller.abort();
  await assert.rejects(request, { name: 'AbortError' });
  assert.equal(timers.size, 0);
});
