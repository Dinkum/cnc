const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = (name) => fs.readFileSync(path.join(__dirname, '../../app/static/js', name), 'utf8');
function uiContext() {
  const scripts = [];
  const document = {
    scripts,
    createElement() {
      return { dataset: {}, handlers: {}, getAttribute() { return this.src; }, addEventListener(event, callback) { this.handlers[event] = callback; }, remove() { scripts.splice(scripts.indexOf(this), 1); } };
    },
    head: { appendChild(script) { scripts.push(script); } },
  };
  const context = vm.createContext({ window: {}, document, console });
  vm.runInContext(source('cnc-ui.js'), context);
  return { ui: context.window.CNCUI, scripts, context };
}
function arrow(name, file) {
  const text = source(file);
  const start = text.indexOf(`  const ${name} =`);
  const end = text.indexOf('\n  };', start) + '\n  };'.length;
  return text.slice(start, end);
}

test('failed assets retry while concurrent loads share a request', async () => {
  const { ui, scripts } = uiContext();
  const first = ui.loadDeferredScript('/fixture.js');
  assert.equal(first, ui.loadDeferredScript('/fixture.js'));
  const rejected = assert.rejects(first, /failed to load/);
  scripts[0].handlers.error();
  await rejected;
  assert.equal(scripts.length, 0);
  const second = ui.loadDeferredScript('/fixture.js');
  assert.equal(scripts.length, 1);
  scripts[0].handlers.load();
  await second;
  await ui.loadDeferredScript('/fixture.js');
  assert.equal(scripts.length, 1);
});

test('metric readers preserve absence, zero, and partial network totals', () => {
  const { ui, context } = uiContext();
  Object.assign(context, { metricNumber: ui.metricNumber, normalizeHostMetricKey: (key) => key, HOST_METRIC_DATA_KEYS: { cpu: 'cpu_percent', memory: 'memory_percent', disk: 'disk_percent', network: 'network_total_bps' } });
  vm.runInContext(arrow('hostMetricValue', 'dashboard.js') + arrow('outputMetricValue', 'output-detail.js') + ';this.readers=[hostMetricValue,outputMetricValue];', context);
  for (const value of [null, undefined, '', ' ', false, NaN, Infinity]) assert.equal(ui.metricNumber(value), null);
  const fields = ['cpu_percent', 'cpu_percent_of_host', 'memory_percent', 'disk_percent', 'disk_usage_bytes', 'network_total_bps', 'network_rx_bps', 'network_tx_bps'];
  for (const reader of context.readers) for (const metric of ['cpu', 'memory', 'disk', 'network']) {
    for (const value of [null, 0, 42]) {
      assert.equal(reader(Object.fromEntries(fields.map((key) => [key, value])), metric), value);
    }
  }
  assert.equal(context.readers[1]({ network_total_bps: null, network_rx_bps: 1200, network_tx_bps: 800 }, 'network'), 2000);
});

test('metric CSV labels rates and preserves nulls, quotes, and numbers', () => {
  const { ui } = uiContext();
  const csv = ui.metricCsv({ metric: { key: 'network', unit_kind: 'rate' }, series: [{ timestamp: '2026-01-01T00:00:00Z', avg: 0, min: -1, max: null, note: 'a,"b"' }, { timestamp: '=1+1', avg: 100 }] }, 'host');
  assert.match(csv, /"bytes_per_second"/);
  assert.match(csv, /"0","-1","","a,""b"""/);
  assert.match(csv, /"'=1\+1"/);
  assert.equal(csv.trim().split('\r\n').length, 3);
});


test('output polling distinguishes missing observations from runtime failures', () => {
  const context = vm.createContext({});
  vm.runInContext(arrow('outputRuntimeBadge', 'dashboard.js') + ';this.badge=outputRuntimeBadge;', context);
  const badge = (...args) => JSON.parse(JSON.stringify(context.badge(...args)));
  for (const kind of ['app', 'shield']) {
    for (const service of [undefined, {}, { data: {} }, { data: { ActiveState: 'unknown' } }]) {
      assert.deepEqual(badge(true, kind, service), { value: 'unknown', tone: 'queued' });
    }
    assert.equal(badge(true, kind, { data: { ActiveState: 'active' }, ok: true }).value, 'healthy');
    for (const state of ['failed', 'inactive']) {
      assert.equal(badge(true, kind, { data: { ActiveState: state }, ok: true }).value, 'unhealthy');
    }
  }
  for (const diagnosis of ['backend_observation_deferred', 'backend_observation_unavailable']) {
    assert.equal(badge(true, 'app', { data: { ActiveState: 'active' }, diagnostics: { diagnosis } }).value, 'unknown');
  }
  assert.equal(badge(true, 'app', { data: { ActiveState: 'active' }, diagnostics: { diagnosis: 'app_unmonitored' } }).value, 'unmonitored');
  assert.equal(badge(true, 'app', { data: { ActiveState: 'active' }, diagnostics: { diagnosis: 'app_failed' } }).value, 'unhealthy');
  assert.equal(badge(false, 'app', undefined).value, 'not enabled');
  assert.equal(badge(true, 'static', undefined).value, 'healthy');
});

test('outputs list recovers from a cold deletion snapshot without a false unhealthy count', () => {
  const pill = () => ({ textContent: '', className: '', classList: { toggle() {} } });
  const rows = Array.from({ length: 8 }, (_, i) => {
    const cells = new Map();
    return {
      dataset: { outputName: `app-${i}`, outputKind: 'app', outputEnabled: '1' },
      querySelector: (selector) => {
        if (!cells.has(selector)) cells.set(selector, pill());
        return cells.get(selector);
      },
    };
  });
  const unhealthy = pill(), enabled = pill();
  const context = vm.createContext({
    document: {
      querySelectorAll: () => rows,
      querySelector: (selector) => selector.includes('unhealthy') ? unhealthy : enabled,
    },
    averageOutputLoad: () => null,
    outputLoadMeter: () => ({ label: '', tone: 'inactive', percent: 0, compact: true }),
    setMeterFill() {},
  });
  vm.runInContext(['backendNameFromService', 'outputRuntimeBadge', 'renderOutputsFromStatus']
    .map(name => arrow(name, 'dashboard.js')).join('\n') + ';this.render=renderOutputsFromStatus;', context);
  context.render({ services: [] });
  assert.equal(unhealthy.textContent, '0 unhealthy');
  assert.equal(enabled.textContent, '8/8 enabled');
  for (const row of rows) assert.equal(row.querySelector('[data-output-runtime-pill]').textContent, 'unknown');
  const services = rows.map(row => ({ backend: row.dataset.outputName, ok: true, data: { ActiveState: 'active' } }));
  context.render({ services });
  assert.equal(unhealthy.textContent, '0 unhealthy');
  for (const row of rows) assert.equal(row.querySelector('[data-output-runtime-pill]').textContent, 'healthy');
  services[0].data.ActiveState = 'failed';
  services[0].ok = false;
  context.render({ services });
  assert.equal(unhealthy.textContent, '1 unhealthy');
  assert.equal(rows[0].querySelector('[data-output-runtime-pill]').textContent, 'unhealthy');
});

test('late history responses cannot replace the current metric or cached export', async () => {
  const pending = [], cached = [], rendered = [];
  const context = vm.createContext({
    AbortController, URLSearchParams,
    metricSelect: { value: 'cpu' }, timeframeSelect: { value: 'day' },
    metricsChart: { parentElement: { clientWidth: 900 } }, metricsEmpty: { hidden: true },
    lastMetricPayload: null, chartInstance: null, metricsRequestId: 0, metricsAbortController: null,
    syncOutputMetricExport() {}, setMetricsLoading() {},
    ensureMetricChartAssets: async () => {}, waitForMetricsLoadingPaint: async () => {},
    writeCachedMetricPayload: payload => cached.push(payload.metric.key),
    renderMetricsChart: payload => rendered.push(payload.metric.key),
    showMetricHistoryError() { throw new Error('unexpected request failure'); },
    fetch: (_url, options) => new Promise(resolve => pending.push({ resolve, signal: options.signal })),
    backendId: 1,
  });
  context.outputMetricKey = () => context.metricSelect.value;
  context.outputTimeframeKey = () => context.timeframeSelect.value;
  vm.runInContext(arrow('loadMetricHistory', 'output-detail.js') + ';this.load=loadMetricHistory;', context);
  const older = context.load();
  await new Promise(setImmediate);
  context.metricSelect.value = 'memory';
  const newer = context.load();
  await new Promise(setImmediate);
  assert.equal(pending.length, 2);
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve({ ok: true, json: async () => ({ metric: { key: 'memory' } }) });
  await newer;
  // Model a transport that completes despite cancellation, after the newer request.
  pending[0].resolve({ ok: true, json: async () => ({ metric: { key: 'cpu' } }) });
  await older;
  assert.deepEqual(cached, ['memory']);
  assert.deepEqual(rendered, ['memory']);
});
