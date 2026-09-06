const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../../app/static/js/operation-events.js"), "utf8");
const flush = () => new Promise(setImmediate);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

function harness(response = { ok: true, json: async () => ({ status: "success" }) }) {
  let stream;
  let fetchCount = 0;
  let callbackCount = 0;
  const timers = new Map();
  let timerId = 0;
  const gate = deferred();
  class EventSource {
    constructor() { stream = this; this.listeners = {}; this.closed = false; }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    close() { this.closed = true; }
    // Permit already queued events to arrive even after close().
    emit(type, payload) { return this.listeners[type]?.({ data: JSON.stringify(payload) }); }
  }
  const window = {
    EventSource, location: { origin: "http://example.com" },
    setTimeout: (callback) => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  };
  vm.runInNewContext(source, {
    window, EventSource, URL, Error,
    fetch: async () => { fetchCount++; return response; },
  });
  const watch = (handlers = {}) => window.CNCOperations.watchOperation(1, {
    onSuccess: async () => { callbackCount++; await gate.promise; },
    onFailure: async () => { callbackCount++; await gate.promise; },
    ...handlers,
  });
  return {
    watch, gate, timers,
    get stream() { return stream; },
    get fetchCount() { return fetchCount; },
    get callbackCount() { return callbackCount; },
  };
}

for (const status of ["success", "failed", "partial", "cancelled"]) {
  test(`${status} closes transport before async callback and handles queued events once`, async () => {
    const h = harness();
    const result = h.watch();
    const stream = h.stream;
    stream.emit("operation", { status });
    await flush();
    assert.equal(stream.closed, true);
    stream.emit("error");
    stream.emit("operation", { status });
    stream.emit("operation", { status: "running" });
    await flush();
    assert.equal(h.callbackCount, 1);
    assert.equal(h.fetchCount, 0);
    assert.equal(h.timers.size, 0);
    h.gate.resolve();
    assert.equal((await result).status, status);
  });
}

test("a success callback error rejects once without transport retries", async () => {
  const h = harness();
  const error = new Error("page refresh failed");
  let calls = 0;
  const result = h.watch({ onSuccess: async () => { calls++; throw error; } });
  const rejected = assert.rejects(result, (caught) => caught === error);
  h.stream.emit("operation", { status: "success" });
  await rejected;
  h.stream.emit("error");
  await flush();
  assert.equal(calls, 1);
  assert.equal(h.fetchCount, 0);
  assert.equal(h.timers.size, 0);
});

test("failure rejection still uses the failure callback result", async () => {
  const h = harness();
  const error = new Error("operation failed");
  const result = h.watch({ rejectOnFailure: true, onFailure: async () => error });
  const rejected = assert.rejects(result, (caught) => caught === error);
  h.stream.emit("operation", { status: "failed" });
  await rejected;
});

test("a running stream error still falls back to polling", async () => {
  const h = harness();
  const result = h.watch();
  h.stream.emit("error");
  await flush();
  assert.equal(h.fetchCount, 1);
  assert.equal(h.callbackCount, 1);
  h.gate.resolve();
  assert.equal((await result).status, "success");
  assert.equal(h.timers.size, 0);
});


test("an in-flight poll error cannot override terminal stream completion", async () => {
  const body = deferred();
  const h = harness({ ok: false, status: 404, json: () => body.promise });
  let errors = 0;
  const result = h.watch({ onError: () => { errors++; } });
  const stream = h.stream;
  stream.emit("error");
  await flush();
  assert.equal(h.fetchCount, 1);
  stream.emit("operation", { status: "success" });
  body.resolve({ detail: "operation missing" });
  await flush();
  assert.equal(errors, 0);
  assert.equal(h.callbackCount, 1);
  h.gate.resolve();
  assert.equal((await result).status, "success");
});
