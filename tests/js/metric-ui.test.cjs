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
