/* Work-claim ledger for the browser harness. Same results as work_claim.py. */

export const CLAIM_TTL = 900;
export const CLAIM_MAX_TTL = 24 * 60 * 60;
const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 100;
const MAX_NOTE = 200;
const MAX_DONE = 100;
const MAX_CLAIMS = 1000;
const MAX_LEDGER_BYTES = 8 * 1024 * 1024;
const ITEM_RE = /^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,159}$/;
const OWNER_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const STATES = { open: true, active: true, expired: true, stale: true, done: true, all: true };

export class ClaimError extends Error {
  constructor(message) {
    super(message);
    this.name = "ClaimError";
  }
}

function locked(store) {
  let chain = Promise.resolve();
  return function exclusive(fn) {
    const run = chain.then(fn, fn);
    chain = run.then(function () {}, function () {});
    return run;
  };
}

function isPlain(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function itemId(value) {
  if (typeof value !== "string" || !ITEM_RE.test(value)) {
    throw new ClaimError("item must be 1–160 characters: letters, digits, and . _ : / # - (not starting with a symbol)");
  }
  return value;
}

function ownerId(value) {
  if (typeof value !== "string" || !OWNER_RE.test(value)) {
    throw new ClaimError("owner must be 1–128 characters: letters, digits, and . _ : - (not starting with a symbol)");
  }
  return value;
}

function asInt(name, value) {
  if (typeof value === "boolean" || typeof value !== "number" || !Number.isFinite(value) || !Number.isInteger(value)) {
    throw new ClaimError(name + " must be an integer");
  }
  return value;
}

function ttlOf(value) {
  const number = asInt("ttl_seconds", value);
  if (number < 1 || number > CLAIM_MAX_TTL) throw new ClaimError("ttl_seconds must be from 1 to " + CLAIM_MAX_TTL);
  return number;
}

function limitOf(value) {
  const number = asInt("limit", value);
  if (number < 1 || number > MAX_LIMIT) throw new ClaimError("limit must be from 1 to " + MAX_LIMIT);
  return number;
}

function cleanState(value) {
  if (value == null || value === "") return "open";
  if (typeof value !== "string") throw new ClaimError("state must be a string");
  const name = value.trim().toLowerCase();
  if (!STATES[name]) throw new ClaimError("state must be one of: open, active, expired, stale, done, all");
  return name === "stale" ? "expired" : name;
}

function cleanNote(note) {
  if (note == null) return "";
  if (typeof note !== "string") throw new ClaimError("note must be a string");
  if (/[\u0000-\u001f]/.test(note)) throw new ClaimError("note must not contain control characters");
  const text = note.trim();
  if (text.length > MAX_NOTE) throw new ClaimError("note must be at most " + MAX_NOTE + " characters");
  return text;
}

function stamp(date) {
  const moment = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(moment.getTime())) throw new ClaimError("clock must return a Date");
  return new Date(moment.getTime());
}

function iso(moment) {
  return stamp(moment).toISOString();
}

function parseTime(value, item) {
  if (typeof value !== "string") throw new ClaimError("claim '" + item + "' in the ledger has a bad expires_at");
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) throw new ClaimError("claim '" + item + "' in the ledger has a bad expires_at");
  return parsed;
}

function result(name, ok, view, previous) {
  const body = { result: name, ok: ok };
  Object.keys(view).forEach(function (key) { body[key] = view[key]; });
  if (previous) body.previous_owner = previous;
  return body;
}

function absent(item) {
  return { result: "absent", ok: false, item: item };
}

function fresh(item, owner, ttl, note, now) {
  const record = {
    item: item,
    owner: owner,
    status: "claimed",
    ttl_seconds: ttl,
    claimed_at: iso(now),
    expires_at: iso(new Date(now.getTime() + ttl * 1000)),
  };
  if (note) record.note = note;
  return record;
}

function viewOf(record, now) {
  if (!isPlain(record)) throw new ClaimError("a claim in the ledger is corrupt");
  const item = record.item;
  const owner = record.owner;
  const status = record.status;
  if (typeof item !== "string" || typeof owner !== "string" || (status !== "claimed" && status !== "done")) {
    throw new ClaimError("a claim in the ledger is corrupt");
  }
  const view = { item: item, owner: owner };
  if (status === "done") {
    view.state = "done";
  } else {
    const expires = parseTime(record.expires_at, item);
    const remaining = (expires - now.getTime()) / 1000;
    if (remaining <= 0) {
      view.state = "expired";
      view.expires_in = 0;
      view.takeable = true;
    } else {
      view.state = "active";
      view.expires_in = Math.max(1, Math.ceil(remaining - 1e-9));
    }
  }
  if (typeof record.note === "string" && record.note) view.note = record.note;
  return view;
}

function matches(view, state, owner) {
  if (owner != null && view.owner !== owner) return false;
  if (state === "all") return true;
  if (state === "open") return view.state !== "done";
  return view.state === state;
}

function storedTtl(record) {
  const value = record.ttl_seconds == null ? CLAIM_TTL : record.ttl_seconds;
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1 || value > CLAIM_MAX_TTL) {
    throw new ClaimError("claim '" + record.item + "' in the ledger has a bad ttl");
  }
  return value;
}

function trimDone(claims, maxDone) {
  const doneKeys = Object.keys(claims).filter(function (key) {
    return isPlain(claims[key]) && claims[key].status === "done";
  });
  if (doneKeys.length <= maxDone) return;
  doneKeys.sort(function (a, b) {
    return String(claims[a].done_at || "").localeCompare(String(claims[b].done_at || ""));
  });
  const extra = doneKeys.length - maxDone;
  for (let i = 0; i < extra; i += 1) delete claims[doneKeys[i]];
}

export function createClaims(store, options) {
  const opts = options || {};
  const clock = opts.now || function () { return new Date(); };
  const maxDone = opts.maxDone == null ? MAX_DONE : opts.maxDone;
  const maxClaims = opts.maxClaims == null ? MAX_CLAIMS : opts.maxClaims;
  if (typeof maxDone !== "number" || !Number.isInteger(maxDone) || maxDone < 1) {
    throw new ClaimError("max_done must be an integer >= 1");
  }
  if (typeof maxClaims !== "number" || !Number.isInteger(maxClaims) || maxClaims < 1) {
    throw new ClaimError("max_claims must be an integer >= 1");
  }
  const exclusive = locked(store);

  function now() {
    return stamp(clock());
  }

  async function transaction(fn, create) {
    return exclusive(async function () {
      const raw = await store.read();
      const missing = !raw || !String(raw).trim();
      if (!missing && String(raw).length > MAX_LEDGER_BYTES) {
        throw new ClaimError("claim ledger is " + String(raw).length + " bytes; refusing to load past " + MAX_LEDGER_BYTES);
      }
      if (missing && !create) return fn(null);
      let data = { version: 1, claims: {} };
      if (!missing) {
        try {
          data = JSON.parse(raw);
        } catch (err) {
          throw new ClaimError("claim ledger is not valid JSON");
        }
        if (!isPlain(data) || !isPlain(data.claims)) {
          throw new ClaimError("claim ledger is not a work-claim file (expected a claims object)");
        }
      }
      const changed = fn(data);
      if (changed.write) {
        await store.write(JSON.stringify({ version: 1, claims: data.claims }, null, 2) + "\n");
      }
      return changed.result;
    });
  }

  function ttlArg(extra) {
    const value = extra && (extra.ttlSeconds != null ? extra.ttlSeconds : extra.ttl_seconds);
    return value == null ? null : ttlOf(value);
  }

  return {
    async take(item, owner, extra) {
      const id = itemId(item);
      const who = ownerId(owner);
      const info = extra || {};
      const ttl = ttlArg(info) == null ? CLAIM_TTL : ttlArg(info);
      const note = cleanNote(info.note);
      if (info.reopen != null && typeof info.reopen !== "boolean") throw new ClaimError("reopen must be a boolean");
      const reopen = info.reopen === true;
      const moment = now();
      return transaction(function (data) {
        const claims = data.claims;
        const record = claims[id];
        let previous = null;
        if (record != null) {
          const view = viewOf(record, moment);
          const same = record.owner === who;
          if (view.state === "active" && same) return { write: false, result: result("held", true, view) };
          if (view.state === "active") return { write: false, result: result("busy", false, view) };
          if (view.state === "done" && !reopen) return { write: false, result: result("closed", false, view) };
          if (!same) previous = record.owner;
        } else if (Object.keys(claims).length >= maxClaims) {
          throw new ClaimError("ledger is full (" + maxClaims + " claims); release or finish one first");
        }
        const row = fresh(id, who, ttl, note, moment);
        claims[id] = row;
        const name = previous ? "taken-over" : "taken";
        return { write: true, result: result(name, true, viewOf(row, moment), previous) };
      }, true);
    },
    async renew(item, owner, extra) {
      const id = itemId(item);
      const who = ownerId(owner);
      const ttl = ttlArg(extra || {});
      const moment = now();
      return transaction(function (data) {
        if (!data) return { write: false, result: absent(id) };
        const record = data.claims[id];
        if (!record) return { write: false, result: absent(id) };
        const view = viewOf(record, moment);
        if (view.state === "done") return { write: false, result: result("closed", false, view) };
        if (record.owner !== who) return { write: false, result: result("not-owner", false, view) };
        const used = ttl == null ? storedTtl(record) : ttl;
        record.status = "claimed";
        record.ttl_seconds = used;
        record.expires_at = iso(new Date(moment.getTime() + used * 1000));
        record.renewed_at = iso(moment);
        return { write: true, result: result("renewed", true, viewOf(record, moment)) };
      }, false);
    },
    async heartbeat(item, owner, extra) {
      return this.renew(item, owner, extra);
    },
    async release(item, owner) {
      const id = itemId(item);
      const who = ownerId(owner);
      const moment = now();
      return transaction(function (data) {
        if (!data) return { write: false, result: absent(id) };
        const record = data.claims[id];
        if (!record) return { write: false, result: absent(id) };
        const view = viewOf(record, moment);
        if (view.state === "done") return { write: false, result: result("closed", false, view) };
        if (record.owner !== who) return { write: false, result: result("not-owner", false, view) };
        delete data.claims[id];
        return { write: true, result: { result: "released", ok: true, item: id } };
      }, false);
    },
    async done(item, owner, extra) {
      const id = itemId(item);
      const who = ownerId(owner);
      const note = cleanNote(extra && extra.note);
      const moment = now();
      return transaction(function (data) {
        if (!data) return { write: false, result: absent(id) };
        const record = data.claims[id];
        if (!record) return { write: false, result: absent(id) };
        const view = viewOf(record, moment);
        if (record.owner !== who) {
          if (view.state === "done") return { write: false, result: result("closed", false, view) };
          return { write: false, result: result("not-owner", false, view) };
        }
        if (view.state === "done") return { write: false, result: result("done", true, view) };
        record.status = "done";
        record.done_at = iso(moment);
        delete record.expires_at;
        if (note) record.note = note;
        trimDone(data.claims, maxDone);
        return { write: true, result: result("done", true, viewOf(record, moment)) };
      }, false);
    },
    async list(extra) {
      const info = extra || {};
      const state = cleanState(info.state);
      const owner = info.owner == null ? null : ownerId(info.owner);
      const id = info.item == null ? null : itemId(info.item);
      const limit = info.limit == null ? DEFAULT_LIMIT : limitOf(info.limit);
      const moment = now();
      return transaction(function (data) {
        if (!data) return { write: false, result: { count: 0, claims: [] } };
        const claims = data.claims;
        if (id != null) {
          const record = claims[id];
          if (!record) return { write: false, result: { count: 0, claims: [] } };
          const view = viewOf(record, moment);
          if (owner != null && view.owner !== owner) return { write: false, result: { count: 0, claims: [] } };
          return { write: false, result: { count: 1, claims: [view] } };
        }
        const rows = [];
        Object.keys(claims).sort().forEach(function (key) {
          const view = viewOf(claims[key], moment);
          if (matches(view, state, owner)) rows.push(view);
        });
        const shown = rows.slice(0, limit);
        const body = { count: shown.length, claims: shown };
        if (rows.length > shown.length) {
          body.total = rows.length;
          body.truncated = true;
        }
        return { write: false, result: body };
      }, false);
    },
  };
}
