/*
 * Tests for the serialised state store.
 * Run with: node --test extension/tests/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { createStateStore } from '../state-store.js';

const DEFAULTS = { status: 'Idle', sessionId: null, tabId: null, alertCode: null };

// A storage area whose operations take a (deterministic) while, like
// chrome.storage.session, so interleaving between callers is possible.
function slowStorage(delays = []) {
  let data = {};
  let call = 0;
  const wait = () => new Promise((resolve) => setTimeout(resolve, delays[call++ % delays.length] ?? 1));
  return {
    async get(key) {
      // The value is captured when the read begins; the caller receives it
      // later. A write that lands in between makes this read stale, as with
      // a real asynchronous storage area.
      const snapshot = key in data ? { [key]: structuredClone(data[key]) } : {};
      await wait();
      return snapshot;
    },
    async set(obj) {
      await wait();
      data = { ...data, ...structuredClone(obj) };
    },
    snapshot: () => structuredClone(data)
  };
}

// The pre-fix behaviour, kept here to show the tests can actually fail.
function unserialisedStore(storage, key, defaults) {
  const read = async () => ({ ...defaults, ...((await storage.get(key))[key] || {}) });
  return {
    read,
    write: async (patch) => {
      const next = { ...(await read()), ...patch };
      await storage.set({ [key]: next });
      return next;
    }
  };
}

test('the unserialised version loses the session id (the bug being fixed)', async () => {
  // Start's write and a backend "ready" write begin together. The backend write
  // read the state first (slow read), so it lands last with the older state.
  const storage = slowStorage([30, 1, 1, 1, 1, 1]);
  const store = unserialisedStore(storage, 's', DEFAULTS);
  const backendEvent = store.write({ status: 'Streaming' }); // slow read, starts first
  await new Promise((resolve) => setTimeout(resolve, 5));
  const startClick = store.write({ sessionId: 'abc', tabId: 7 }); // fast, finishes first
  await Promise.all([backendEvent, startClick]);
  const final = (await storage.get('s')).s;
  assert.equal(final.sessionId, null, 'expected the bug to reproduce here');
});

test('serialised writes keep the session id when a backend event races Start', async () => {
  const storage = slowStorage([30, 1, 1, 1, 1, 1]);
  const store = createStateStore({ storage, key: 's', defaults: DEFAULTS });
  const backendEvent = store.write({ status: 'Streaming' });
  await new Promise((resolve) => setTimeout(resolve, 5));
  const startClick = store.write({ sessionId: 'abc', tabId: 7 });
  await Promise.all([backendEvent, startClick]);
  const final = await store.read();
  assert.equal(final.sessionId, 'abc');
  assert.equal(final.tabId, 7);
  assert.equal(final.status, 'Streaming');
});

test('many concurrent writes to different fields all survive', async () => {
  const storage = slowStorage([3, 1, 5, 2, 1, 4]);
  const store = createStateStore({ storage, key: 's', defaults: { ...DEFAULTS, a: 0, b: 0, c: 0, d: 0 } });
  await Promise.all([
    store.write({ a: 1 }),
    store.write({ b: 2 }),
    store.write({ c: 3 }),
    store.write({ d: 4 }),
    store.write({ sessionId: 'x' }),
    store.write({ alertCode: 'CODE' })
  ]);
  assert.deepEqual(await store.read(), { ...DEFAULTS, a: 1, b: 2, c: 3, d: 4, sessionId: 'x', alertCode: 'CODE' });
});

test('later writes to the same field win, in call order', async () => {
  const storage = slowStorage([4, 1, 3, 1]);
  const store = createStateStore({ storage, key: 's', defaults: DEFAULTS });
  await Promise.all([store.write({ status: 'one' }), store.write({ status: 'two' }), store.write({ status: 'three' })]);
  assert.equal((await store.read()).status, 'three');
});

test('reset waits for earlier writes and then clears everything', async () => {
  const storage = slowStorage([3, 1]);
  const store = createStateStore({ storage, key: 's', defaults: DEFAULTS });
  const pending = store.write({ sessionId: 'abc', status: 'Listening' });
  const reset = store.reset();
  const after = store.write({ status: 'Idle again' });
  await Promise.all([pending, reset, after]);
  const final = await store.read();
  assert.equal(final.sessionId, null, 'reset ran after the write that preceded it');
  assert.equal(final.status, 'Idle again', 'a write queued after the reset still applies');
});

test('a failing write does not block the ones behind it', async () => {
  let failNext = true;
  const base = slowStorage([1]);
  const storage = {
    get: base.get,
    async set(obj) {
      if (failNext) {
        failNext = false;
        throw new Error('storage full');
      }
      return base.set(obj);
    }
  };
  const store = createStateStore({ storage, key: 's', defaults: DEFAULTS });
  const first = store.write({ status: 'will fail' });
  const second = store.write({ status: 'works' });
  await assert.rejects(first, /storage full/);
  await second;
  assert.equal((await store.read()).status, 'works');
});

test('onChange receives each new state in order', async () => {
  const seen = [];
  const store = createStateStore({
    storage: slowStorage([2, 1]),
    key: 's',
    defaults: DEFAULTS,
    onChange: (state) => seen.push(`${state.status}/${state.sessionId}`)
  });
  await Promise.all([store.write({ status: 'a' }), store.write({ sessionId: 'z' }), store.reset()]);
  assert.deepEqual(seen, ['a/null', 'a/z', 'Idle/null']);
});
