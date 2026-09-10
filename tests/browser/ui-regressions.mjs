import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { performance } from 'node:perf_hooks';

const { chromium } = await import(pathToFileURL(process.env.CNC_PLAYWRIGHT_MODULE));
const fixtures = process.argv[2];
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const metrics = JSON.parse(await fs.readFile(path.join(fixtures, 'metrics.json')));
const status = JSON.parse(await fs.readFile(path.join(fixtures, 'status.json')));
const browser = await chromium.launch({ headless: true, channel: process.env.CNC_BROWSER_CHANNEL || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' });
const page = await context.newPage();
page.setDefaultTimeout(5000);
page.setDefaultNavigationTimeout(5000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
let deleted = false, failDelete = false, holdStatus = null, holdMetric = null;
let metricOverride = null, failMetric = false;
const requests = [];
const json = (route, body, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) });
const latch = () => {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
};

// Every request is intercepted: a new/unexpected endpoint fails instead of reaching a host.
await context.route('**/*', async route => {
  const url = new URL(route.request().url());
  const name = url.pathname;
  requests.push(name);
  if (url.origin !== 'http://example.test') {
    errors.push(`Unexpected external request: ${url.origin}${name}`);
    return route.abort();
  }
  if (name.startsWith('/static/')) {
    const asset = path.resolve(root, 'app', name.slice(1));
    assert.ok(asset.startsWith(path.join(root, 'app/static/')));
    const contentType = name.endsWith('.css') ? 'text/css' : 'text/javascript';
    return route.fulfill({ contentType, body: await fs.readFile(asset) });
  }
  if (name === '/' || name === '/outputs/1') {
    const file = name === '/outputs/1' ? 'output.html' : deleted ? 'outputs-deleted.html' : 'outputs.html';
    return route.fulfill({ contentType: 'text/html', body: await fs.readFile(path.join(fixtures, file)) });
  }
  if (name === '/api/status') {
    if (holdStatus) await holdStatus.promise;
    return json(route, status);
  }
  if (name.endsWith('/metrics-history')) {
    const pending = holdMetric;
    const key = url.searchParams.get('metric');
    const payload = metricOverride || metrics[key];
    const failed = failMetric;
    if (pending) await pending.promise;
    return json(route, failed ? { detail: 'Fixture history unavailable' } : payload, failed ? 503 : 200);
  }
  if (name.endsWith('/ssh-keys')) return json(route, { keys: [] });
  if (name === '/api/active-operations') return json(route, { operations: [] });
  if (name.endsWith('/runtime-signals')) return json(route, { pending: false, runtime_health_label: 'Healthy', runtime_health_tone: 'success' });
  if (name.endsWith('/backup-signals')) return json(route, { backups: [] });
  if (name === '/ui/backends/1/delete') {
    assert.equal(route.request().method(), 'POST');
    if (failDelete) return json(route, { detail: 'Fixture deletion rejected' }, 409);
    deleted = true;
    return json(route, { operation_id: 42 }, 202);
  }
  if (name === '/api/operations/42/events') {
    const operation = { id: 42, status: 'success', phase: 'delete', details: { progress: 100, message: 'Output deleted.', redirect_url: '/?tab=outputs&defer_status=1' } };
    return route.fulfill({ contentType: 'text/event-stream', body: `event: operation\ndata: ${JSON.stringify(operation)}\n\n` });
  }
  errors.push(`Unexpected request: ${route.request().method()} ${name}`);
  return route.abort();
});

async function run(name, action) {
  const start = performance.now();
  try {
    await action();
    assert.deepEqual(errors, [], 'browser errors or unhandled requests');
    console.log(`PASS ${name} (${((performance.now() - start) / 1000).toFixed(2)}s)`);
  } catch (error) {
    await page.screenshot({ path: path.join(fixtures, 'failure.png'), fullPage: true });
    await fs.writeFile(path.join(fixtures, 'failure.html'), await page.content());
    throw new Error(`${name}: ${error.message}\nArtifacts: ${fixtures}`, { cause: error });
  }
}
const chart = () => page.evaluate(() => {
  const canvas = document.querySelector('#output-metrics-chart');
  const instance = window.Chart?.getChart(canvas);
  const rect = canvas.getBoundingClientRect();
  return instance ? { id: instance.id, width: rect.width, height: rect.height, animation: instance.options.animation, loading: !document.querySelector('#output-metrics-loading').hidden } : null;
});
const waitChart = () => page.waitForFunction(() => !!window.Chart?.getChart(document.querySelector('#output-metrics-chart')));
function assertSteadyChart(actual, expected) {
  assert.equal(actual.id, expected.id, 'refresh must reuse the mounted chart');
  assert.equal(actual.height, expected.height);
  // Chart.js rounds fractional CSS widths when its ResizeObserver first runs.
  assert.ok(Math.abs(actual.width - expected.width) <= 1, 'chart width changed');
  assert.equal(actual.animation, false);
  assert.equal(actual.loading, false, 'same-selection refresh must not fade the chart');
}


try {
  await run('chart refresh stays mounted; export follows loaded selection', async () => {
    await page.goto('http://example.test/outputs/1');
    await waitChart();
    const before = await chart();
    assert.equal(before.animation, false);
    holdMetric = latch();
    const request = page.waitForRequest(r => r.url().includes('/metrics-history'));
    await page.locator('#output-timeframe-select').selectOption('day');
    await request;
    assertSteadyChart(await chart(), before);
    const response = page.waitForResponse(r => r.url().includes('/metrics-history'));
    holdMetric.release(); holdMetric = null;
    await response;
    await page.waitForFunction(() => document.querySelector('#output-metrics-loading').hidden);
    assertSteadyChart(await chart(), before);

    const exportButton = page.locator('[data-export-toggle]');
    await exportButton.click();
    assert.equal(await exportButton.getAttribute('aria-expanded'), 'true');
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download CSV', exact: true }).click();
    const csv = await download;
    assert.equal(csv.suggestedFilename(), 'output-1-cpu-day.csv');
    const csvContent = await fs.readFile(await csv.path(), 'utf8');
    assert.match(csvContent, /"output-1","cpu","percent"/);
    assert.equal(csvContent.trim().split('\r\n').length, metrics.cpu.series.length + 1);
    assert.equal(await exportButton.getAttribute('aria-expanded'), 'false');
    await exportButton.click();
    await page.keyboard.press('Escape');
    assert.equal(await exportButton.getAttribute('aria-expanded'), 'false');

    holdMetric = latch();
    const changed = page.waitForRequest(r => r.url().includes('metric=memory'));
    await page.locator('#output-metric-select').selectOption('memory');
    await changed;
    assert.equal(await exportButton.isEnabled(), false, 'cannot export old metric during selection change');
    holdMetric.release(); holdMetric = null;
    await exportButton.waitFor({ state: 'visible' });
    await page.waitForFunction(() => !document.querySelector('[data-export-toggle]').disabled);
    await exportButton.click();
    const jsonDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download JSON', exact: true }).click();
    const file = await jsonDownload;
    assert.equal(file.suggestedFilename(), 'output-1-memory-day.json');
    assert.deepEqual(JSON.parse(await fs.readFile(await file.path(), 'utf8')), metrics.memory);
  });

  await run('failed and empty history responses recover without stale exports', async () => {
    failMetric = true;
    let response = page.waitForResponse(r => r.url().includes('metric=network'));
    await page.locator('#output-metric-select').selectOption('network');
    await response;
    await page.getByText('Metric history could not be loaded.', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-export-toggle]').isEnabled(), false);
    failMetric = false;
    metricOverride = { ...metrics.network, series: [] };
    response = page.waitForResponse(r => r.url().includes('metric=network'));
    await page.locator('#output-timeframe-select').selectOption('day');
    await response;
    await page.locator('#output-metrics-empty').waitFor({ state: 'visible' });
    assert.equal(await chart(), null);
    assert.equal(await page.locator('[data-export-toggle]').isEnabled(), false);
    metricOverride = null;
    await page.locator('#output-timeframe-select').selectOption('day');
    await waitChart();
    await page.waitForFunction(() => !document.querySelector('[data-export-toggle]').disabled);
  });

  await run('delete failure retains output; successful delete leaves peers unknown then healthy', async () => {
    failDelete = true;
    await page.locator('[data-delete-output-trigger]').click();
    await page.locator('[data-confirm-accept]').click();
    await page.getByText(/Fixture deletion rejected/).first().waitFor();
    assert.equal(new URL(page.url()).pathname, '/outputs/1');
    assert.equal(deleted, false);
    failDelete = false;
    holdStatus = latch();
    await page.locator('[data-confirm-accept]').click();
    await page.locator('[data-delete-progress-note]').filter({ hasText: 'Done.' }).waitFor();
    await page.getByRole('button', { name: 'Close delete dialog' }).click();
    await page.waitForURL('**/?tab=outputs**');
    assert.equal(await page.locator('[data-output-name="demo-app"]').count(), 0);
    assert.equal(await page.locator('[data-output-name="survivor"] [data-output-runtime-pill]').innerText(), 'unknown');
    assert.equal(await page.locator('[data-outputs-unhealthy-summary]').innerText(), '0 unhealthy');
    holdStatus.release(); holdStatus = null;
    await page.locator('[data-output-name="survivor"] [data-output-runtime-pill]').filter({ hasText: 'healthy' }).waitFor();
    assert.equal(await page.locator('[data-outputs-unhealthy-summary]').innerText(), '0 unhealthy');
    assert.equal(await page.locator('[data-outputs-enabled-summary]').innerText(), '2/2 enabled');
  });

  await run('compact metrics controls fit without changing chart geometry', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('http://example.test/outputs/1');
    await waitChart();
    const geometry = await page.locator('.output-metrics-heading').evaluate(heading => {
      const title = heading.querySelector('h2').getBoundingClientRect();
      const button = heading.querySelector('button').getBoundingClientRect();
      return { aligned: Math.abs((title.top + title.height / 2) - (button.top + button.height / 2)) < 3, right: button.right, viewport: innerWidth };
    });
    assert.equal(geometry.aligned, true);
    assert.ok(geometry.right <= geometry.viewport);
    const before = await chart();
    await page.locator('[data-export-toggle]').click();
    const menu = await page.locator('.metric-export-options').boundingBox();
    assert.ok(menu.x >= 0 && menu.x + menu.width <= 390);
    assertSteadyChart(await chart(), before);
  });
  console.log(`Requests: ${requests.length}; browser engines: 1; external requests: 0`);
} finally {
  await context.close();
  await browser.close();
}
