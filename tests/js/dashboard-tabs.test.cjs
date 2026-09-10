const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../app/static/js/dashboard.js'), 'utf8');
const start = source.indexOf('  const loadDashboardTab =');
const loader = source.slice(start, source.indexOf('\n  };', start) + '\n  };'.length);
function fixture() {
  const panels = new Map(['home', 'inputs', 'outputs'].map(name => [name, { dataset: { loaded: name === 'home' ? '1' : '0' } }]));
  const requests = new Map();
  const result = { selected: 'home', header: 'home' };
  const context = vm.createContext({
    console, AbortController, DOMException, window: { setTimeout, clearTimeout }, CSS: { escape: name => name },
    document: { querySelector: selector => panels.get(selector.match(/data-panel="([^"]+)"/)?.[1]) },
    DOMParser: class { parseFromString(name) { return { name }; } },
    fetch: url => new Promise((resolve, reject) => requests.set(new URL(url, 'http://example.com').searchParams.get('tab'), { resolve, reject })),
    tabIsAvailable: name => panels.has(name),
    replaceFromDocument: (selector, doc) => {
      if (selector === '.site-header') { result.header = doc.name; return {}; }
      return panels.get(doc.name);
    },
    syncBannersFromDocument() { result.error = null; }, bindDashboardPanel() {},
    setBanner(tone, message) { result.error = { tone, message }; },
    selectTab: name => { assert.equal(panels.get(name).dataset.loaded, '1'); result.selected = name; },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../app/static/js/cnc-ui.js'), 'utf8') + '\nconst { withRequestDeadline } = window.CNCUI;', context);
  vm.runInContext('let latestTabRequestId = 0; const tabLoadRequests = new Map();' + loader + ';this.load = loadDashboardTab;', context);
  return { load: context.load, result, respond(name, ok = true, html = name) { requests.get(name).resolve({ ok, status: ok ? 200 : 503, text: async () => html }); }, reject(name) { requests.get(name).reject(new Error('offline')); } };
}
for (const scenario of [
  { name: 'double click', clicks: ['inputs', 'inputs'], responses: ['inputs'], selected: 'inputs' },
  { name: 'return to pending tab', clicks: ['inputs', 'outputs', 'inputs'], responses: ['inputs', 'outputs'], selected: 'inputs' },
  { name: 'return with reverse responses', clicks: ['inputs', 'outputs', 'inputs'], responses: ['outputs', 'inputs'], selected: 'inputs' },
  { name: 'older response cannot replace current selection', clicks: ['inputs', 'outputs'], responses: ['outputs', 'inputs'], selected: 'outputs' },
]) test(scenario.name, async () => {
  const f = fixture();
  const promises = scenario.clicks.map(name => f.load(name));
  for (const name of scenario.responses) f.respond(name);
  await Promise.all(promises);
  assert.equal(f.result.selected, scenario.selected);
  assert.equal(f.result.header, scenario.selected);
});

test('failed shared fetch leaves the selected panel intact and can retry', async () => {
  const f = fixture();
  const first = f.load('inputs'), second = f.load('inputs');
  f.respond('inputs', false);
  await Promise.all([first, second]);
  assert.equal(f.result.selected, 'home');
  assert.match(f.result.error.message, /Could not load inputs/);
  const retry = f.load('inputs');
  f.respond('inputs');
  await retry;
  assert.equal(f.result.selected, 'inputs');
});

for (const fault of ['offline', 'missing-panel']) for (const repeated of [false, true]) {
  test(`${fault} tab failure is visible and retryable (${repeated ? 'repeated' : 'single'} click)`, async () => {
    const f = fixture();
    const pending = [f.load('inputs')];
    if (repeated) pending.push(f.load('inputs'));
    if (fault === 'offline') f.reject('inputs');
    else f.respond('inputs', true, 'access-page');
    await Promise.all(pending);
    assert.equal(f.result.selected, 'home');
    assert.match(f.result.error.message, /Could not load inputs/);
    const retry = f.load('inputs');
    f.respond('inputs');
    await retry;
    assert.equal(f.result.selected, 'inputs');
    assert.equal(f.result.error, null);
  });
}

test('an older tab failure cannot overwrite the current navigation', async () => {
  const f = fixture();
  const old = f.load('inputs'), current = f.load('outputs');
  f.respond('outputs');
  await current;
  f.reject('inputs');
  await old;
  assert.equal(f.result.selected, 'outputs');
  assert.equal(f.result.error, null);
});
