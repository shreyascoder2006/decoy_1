/*
 * state-store.js -- the service worker's saved state, with updates serialised.
 *
 * Every update is read-modify-write: read the saved state, merge a change, write
 * it back. Backend events, the popup and the Start button all trigger updates
 * from different places at nearly the same moment. Unserialised, an update that
 * read the state just *before* another one saved can finish *after* it and write
 * the older state back, silently undoing the other change. That is exactly how
 * the session id was lost when the backend answered "ready" almost instantly.
 *
 * Here every write waits for the one before it, so each starts from the previous
 * write's result and nothing is lost. Plain ES module with no Chrome API of its
 * own (the storage area is passed in), so it is unit tested in Node.
 */

export function createStateStore({ storage, key, defaults, onChange = () => {} }) {
  let queue = Promise.resolve();

  // Run tasks strictly one after another. A failed task must not block the
  // queue for the ones behind it, so the chain swallows errors; the caller of
  // the failed task still receives it.
  function enqueue(task) {
    const run = queue.then(task);
    queue = run.catch(() => {});
    return run;
  }

  async function read() {
    const stored = await storage.get(key);
    return { ...defaults, ...(stored[key] || {}) };
  }

  return {
    read,
    write(patch) {
      return enqueue(async () => {
        const next = { ...(await read()), ...patch };
        await storage.set({ [key]: next });
        onChange(next);
        return next;
      });
    },
    reset() {
      return enqueue(async () => {
        const next = { ...defaults };
        await storage.set({ [key]: next });
        onChange(next);
        return next;
      });
    }
  };
}
