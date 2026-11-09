/* Browser-first agent harness. No imports. Works in a browser and in Node 18+. */

export const DEFAULT_BASE_URL = "https://chat.srv1536099.hstgr.cloud/v1";
export const DEFAULT_MODEL = "Llama-3.2-3B-Instruct";
export const MAX_ITERATIONS = 3;
export const CONTEXT_TOKENS = 2048;
export const REPLY_TOKENS = 256;

const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const REQUEST_ID = /^req_[0-9a-f]{32}$/;
const AUTONOMY = ["readonly", "supervised", "full"];
const RANKS = { continue: 0, warn: 1, "stop-and-ask-human": 2 };

export class HarnessError extends Error {
  constructor(message, extra) {
    super(message);
    this.name = "HarnessError";
    const info = extra || {};
    this.status = info.status || 0;
    this.code = info.code || "error";
  }
}

export function redact(text, secret) {
  const raw = String(text == null ? "" : text);
  if (!secret) return raw;
  return raw.split(String(secret)).join("[secret]");
}

function clip(text) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  if (flat.length > 280) return flat.slice(0, 277) + "...";
  return flat;
}

function isPlain(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function mergeConfig(base, extra) {
  const out = {};
  const source = base || {};
  const over = extra || {};
  Object.keys(source).forEach(function (key) {
    out[key] = source[key];
  });
  Object.keys(over).forEach(function (key) {
    if (over[key] == null) return;
    if (isPlain(out[key]) && isPlain(over[key])) out[key] = mergeConfig(out[key], over[key]);
    else out[key] = over[key];
  });
  return out;
}

export function resolveConfig(globalProfile, soulProfile, sessionProfile) {
  const globalConfig = (globalProfile && globalProfile.config) || {};
  const soulConfig = (soulProfile && soulProfile.config) || {};
  const sessionConfig = (sessionProfile && sessionProfile.config) || {};
  return mergeConfig(mergeConfig(globalConfig, soulConfig), sessionConfig);
}

function blocksFor(profile) {
  if (!profile) return [];
  const parts = [];
  if (profile.instructions && String(profile.instructions).trim()) {
    parts.push(String(profile.instructions).trim());
  }
  const config = profile.config || {};
  if (config.goals && String(config.goals).trim()) parts.push("Goals:\n" + String(config.goals).trim());
  if (config.notes && String(config.notes).trim()) parts.push("Project notes:\n" + String(config.notes).trim());
  return parts;
}

export function composeInstructions(globalProfile, soulProfile, sessionProfile) {
  const parts = [];
  [globalProfile, soulProfile, sessionProfile].forEach(function (profile) {
    blocksFor(profile).forEach(function (block) { parts.push(block); });
  });
  return parts.join("\n\n");
}

function firstValue(map) {
  const keys = Object.keys(map || {});
  return keys.length ? map[keys[0]] : null;
}

export function selectSession(bundle, sessionId) {
  const sessions = (bundle && bundle.sessions) || {};
  const souls = (bundle && bundle.souls) || {};
  const session = sessions[sessionId] || firstValue(sessions);
  const soulId = session && session.config && session.config.soulId;
  const soul = (soulId && souls[soulId]) || firstValue(souls);
  return { global: bundle.global, soul: soul, session: session };
}

export async function loadProfiles(fetchImpl, base) {
  const fetchFn = fetchImpl || globalThis.fetch;
  const root = base && String(base).slice(-1) === "/" ? String(base) : String(base || "profiles/");
  async function getJson(path) {
    const response = await fetchFn(root + path);
    if (!response.ok) throw new HarnessError("Could not load " + path, { status: response.status, code: "profile" });
    return response.json();
  }
  const manifest = await getJson("manifest.json");
  const globalProfile = await getJson("global.json");
  const souls = {};
  const sessions = {};
  const soulNames = manifest.souls || [];
  const sessionNames = manifest.sessions || [];
  for (let i = 0; i < soulNames.length; i += 1) {
    const soul = await getJson("souls/" + soulNames[i]);
    souls[soul.id] = soul;
  }
  for (let j = 0; j < sessionNames.length; j += 1) {
    const session = await getJson("sessions/" + sessionNames[j]);
    sessions[session.id] = session;
  }
  return { global: globalProfile, souls: souls, sessions: sessions };
}

export function estimateTokens(text) {
  const count = Array.from(String(text == null ? "" : text)).length;
  if (!count) return 0;
  return Math.ceil(count / 4);
}

function joinPrompt(system, messages) {
  const parts = [String(system || "")];
  (messages || []).forEach(function (message) {
    parts.push(String(message && message.content || ""));
  });
  return parts.join("\n");
}

export function fitMessages(options) {
  const opts = options || {};
  const contextTokens = opts.contextTokens || CONTEXT_TOKENS;
  const replyTokens = opts.replyTokens == null ? REPLY_TOKENS : opts.replyTokens;
  const budget = Math.max(1, contextTokens - replyTokens);
  const system = String(opts.system || "");
  const list = (opts.messages || []).map(function (message) {
    return { role: message.role, content: String(message.content || "") };
  });
  let dropped = 0;
  while (list.length > 1 && estimateTokens(joinPrompt(system, list)) > budget) {
    list.shift();
    dropped += 1;
  }
  if (list.length && estimateTokens(joinPrompt(system, list)) > budget) {
    const others = list.slice(0, -1);
    const room = budget - estimateTokens(joinPrompt(system, others));
    const keep = Math.max(1, room) * 4;
    const chars = Array.from(list[list.length - 1].content);
    if (chars.length > keep) list[list.length - 1].content = chars.slice(chars.length - keep).join("");
  }
  return {
    system: system,
    messages: list,
    tokens: estimateTokens(joinPrompt(system, list)),
    budget: budget,
    dropped: dropped,
  };
}

function looseJson(text) {
  const cleaned = String(text || "").trim().replace(/,\s*([}\]])/g, "$1");
  return JSON.parse(cleaned);
}

function asArgs(value) {
  if (value == null) return {};
  if (typeof value === "string") {
    const parsed = looseJson(value);
    if (!isPlain(parsed)) throw new Error("args");
    return parsed;
  }
  if (isPlain(value)) return value;
  throw new Error("args");
}

function toolTag(tag) {
  const word = String(tag || "").trim().toLowerCase().split(/\s+/)[0];
  return word === "tool" || word === "tools" || word === "tool-call";
}

function parseToolJson(text) {
  const data = looseJson(text);
  if (!isPlain(data) || typeof data.name !== "string" || !data.name.trim()) throw new Error("name");
  const args = data.args != null ? data.args : data.arguments;
  return { name: data.name.trim(), args: asArgs(args) };
}

function fences(text) {
  const src = String(text || "");
  const found = [];
  let cursor = 0;
  while (cursor < src.length) {
    const start = src.indexOf("```", cursor);
    if (start < 0) break;
    const end = src.indexOf("```", start + 3);
    if (end < 0) break;
    const inner = src.slice(start + 3, end);
    const breakAt = inner.search(/\r?\n/);
    let tag = inner;
    let body = "";
    if (breakAt >= 0) {
      tag = inner.slice(0, breakAt);
      body = inner.slice(breakAt).replace(/^\r?\n/, "").replace(/\r?\n$/, "");
    }
    found.push({ tag: tag.trim(), body: body.trim() });
    cursor = end + 3;
  }
  return found;
}

export function parseToolCall(text) {
  const blocks = fences(text);
  for (let i = 0; i < blocks.length; i += 1) {
    const block = blocks[i];
    if (!toolTag(block.tag)) continue;
    const payload = block.body || block.tag.replace(/^\s*tool(?:-call|s)?\s*/i, "");
    try {
      const call = parseToolJson(payload);
      let ignored = 0;
      for (let j = i + 1; j < blocks.length; j += 1) {
        if (toolTag(blocks[j].tag)) ignored += 1;
      }
      return { call: call, ignored: ignored, malformed: false };
    } catch (err) {
      return { call: null, ignored: 0, malformed: true };
    }
  }
  return { call: null, ignored: 0, malformed: false };
}

function toolLines(tools) {
  const lines = [
    "You may call at most one tool in a reply.",
    "To call a tool, end your reply with one fenced block of this shape:",
    "```tool",
    "{\"name\":\"clock\",\"args\":{}}",
    "```",
    "If you do not need a tool, answer in plain text with no tool block.",
    "Tools:",
  ];
  (tools || []).forEach(function (tool) {
    lines.push("- " + tool.name + ": " + tool.description);
  });
  return lines.join("\n");
}

export function memoryStore(initial) {
  let text = initial == null ? "" : String(initial);
  return {
    async read() { return text; },
    async write(next) { text = String(next); },
  };
}

export function webStore(storage, key) {
  return {
    async read() { return storage.getItem(key) || ""; },
    async write(next) { storage.setItem(key, String(next)); },
  };
}

function locked(store) {
  let chain = Promise.resolve();
  return function exclusive(fn) {
    const run = chain.then(fn, fn);
    chain = run.then(function () {}, function () {});
    return run;
  };
}

function jsonLedger(store, empty) {
  const exclusive = locked(store);
  return {
    async update(mutator) {
      return exclusive(async function () {
        const raw = await store.read();
        let data = empty();
        if (raw && raw.trim()) {
          try {
            data = JSON.parse(raw);
          } catch (err) {
            throw new HarnessError("Ledger is not valid JSON.", { code: "ledger" });
          }
        }
        const changed = await mutator(data);
        if (changed.write) await store.write(JSON.stringify(changed.data, null, 2) + "\n");
        return changed.result;
      });
    },
  };
}

function taskId(value) {
  if (typeof value !== "string" || !TASK_ID.test(value)) {
    throw new HarnessError("task id must be 1–128 characters: letters, digits, and . _ : -", { code: "budget" });
  }
  return value;
}

function integer(name, value) {
  if (typeof value === "boolean" || typeof value !== "number" || !Number.isInteger(value)) {
    throw new HarnessError(name + " must be an integer", { code: "budget" });
  }
  if (value < 0) throw new HarnessError(name + " must be >= 0", { code: "budget" });
  return value;
}

function money(name, value) {
  if (typeof value === "boolean" || typeof value !== "number" || !Number.isFinite(value)) {
    throw new HarnessError(name + " must be a finite number", { code: "budget" });
  }
  if (value < 0) throw new HarnessError(name + " must be >= 0", { code: "budget" });
  return Math.round(value * 1e8) / 1e8;
}

function positiveLimit(name, value, kind) {
  const number = kind === "cost" ? money(name, value) : integer(name, value);
  if (number <= 0) throw new HarnessError(name + " must be > 0", { code: "budget" });
  return number;
}

function pick(primary, secondary) {
  return primary != null ? primary : secondary;
}

export function normalizeLimits(input) {
  const src = input || {};
  const maxTokens = pick(src.maxTokens, src.max_tokens);
  const maxToolCalls = pick(src.maxToolCalls, src.max_tool_calls);
  const maxCost = pick(src.maxCostUsd, src.max_cost_usd);
  const warnRatio = pick(pick(src.warnRatio, src.warn_ratio), 0.8);
  const cleaned = {
    max_tokens: maxTokens == null ? null : positiveLimit("max_tokens", maxTokens, "int"),
    max_tool_calls: maxToolCalls == null ? null : positiveLimit("max_tool_calls", maxToolCalls, "int"),
    max_cost_usd: maxCost == null ? null : positiveLimit("max_cost_usd", maxCost, "cost"),
    warn_ratio: warnRatio,
  };
  if (cleaned.max_tokens == null && cleaned.max_tool_calls == null && cleaned.max_cost_usd == null) {
    throw new HarnessError("set at least one budget limit", { code: "budget" });
  }
  if (typeof warnRatio !== "number" || !Number.isFinite(warnRatio) || warnRatio <= 0 || warnRatio > 1) {
    throw new HarnessError("warn_ratio must be > 0 and <= 1", { code: "budget" });
  }
  return cleaned;
}

function noteText(note) {
  if (note == null || note === "") return "";
  if (typeof note !== "string") throw new HarnessError("note must be a string", { code: "budget" });
  if (/[\u0000-\u001f]/.test(note)) throw new HarnessError("note must not contain control characters", { code: "budget" });
  const text = note.trim();
  if (text.length > 200) throw new HarnessError("note must be at most 200 characters", { code: "budget" });
  return text;
}

function atWarn(projected, limit, ratio) {
  const scale = 1000000;
  return Math.round(projected * scale) >= Math.round(ratio * scale) * limit;
}

function grouped(value) {
  return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function pct(ratio) {
  return Math.round(ratio * 100) + "%";
}

function formatCost(value) {
  return "$" + (Math.round(Number(value) * 10000) / 10000).toFixed(4);
}

function budgetView(task, proposed) {
  const limits = task.limits;
  const spent = task.spent;
  const next = {
    tokens: spent.tokens + (proposed.tokens || 0),
    tool_calls: spent.tool_calls + (proposed.tool_calls || 0),
    cost_usd: Math.round((spent.cost_usd + (proposed.cost_usd || 0)) * 1e8) / 1e8,
  };
  const specs = [
    ["tokens", "max_tokens", "tokens", spent.tokens, next.tokens, "int"],
    ["tool_calls", "max_tool_calls", "tool calls", spent.tool_calls, next.tool_calls, "int"],
    ["cost_usd", "max_cost_usd", "cost", spent.cost_usd, next.cost_usd, "cost"],
  ];
  const dimensions = specs.map(function (spec) {
    const limit = limits[spec[1]];
    const stateLimit = spec[5] === "cost" && limit != null ? Math.round(limit * 1e8) : limit;
    const projected = spec[5] === "cost" ? Math.round(spec[4] * 1e8) : spec[4];
    const already = spec[5] === "cost" ? Math.round(spec[3] * 1e8) : spec[3];
    let state = "continue";
    let remaining = null;
    if (limit != null) {
      if (projected > stateLimit) state = "stop-and-ask-human";
      else if (atWarn(projected, stateLimit, limits.warn_ratio)) state = "warn";
      remaining = spec[5] === "cost"
        ? Math.round((limit - spec[4]) * 1e8) / 1e8
        : limit - spec[4];
    }
    return {
      name: spec[0],
      label: spec[2],
      limit: limit,
      spent: spec[3],
      projected: spec[4],
      remaining: remaining,
      state: state,
    };
  });
  const decision = dimensions.reduce(function (best, dim) {
    return RANKS[dim.state] > RANKS[best] ? dim.state : best;
  }, "continue");
  return {
    task_id: task.task_id,
    decision: decision,
    ok_to_proceed: decision !== "stop-and-ask-human",
    reason: budgetReason(decision, dimensions, limits.warn_ratio, proposed),
    spent: { tokens: spent.tokens, cost_usd: spent.cost_usd, tool_calls: spent.tool_calls },
    remaining: remainingMap(dimensions),
    limits: {
      max_tokens: limits.max_tokens,
      max_tool_calls: limits.max_tool_calls,
      max_cost_usd: limits.max_cost_usd,
      warn_ratio: limits.warn_ratio,
    },
    projected: { tokens: next.tokens, tool_calls: next.tool_calls, cost_usd: next.cost_usd },
  };
}

function remainingMap(dimensions) {
  const out = {};
  dimensions.forEach(function (dim) {
    if (dim.remaining != null) out[dim.name] = dim.remaining;
  });
  return out;
}

function budgetReason(decision, dimensions, warnRatio, proposed) {
  if (decision === "continue") return "continue: within budget";
  const parts = [];
  dimensions.forEach(function (dim) {
    if (dim.state !== decision || dim.limit == null) return;
    const limitText = dim.name === "cost_usd" ? formatCost(dim.limit) : grouped(dim.limit);
    const projectedText = dim.name === "cost_usd" ? formatCost(dim.projected) : grouped(dim.projected);
    if (decision === "stop-and-ask-human") {
      const already = dim.name === "cost_usd"
        ? Math.round(dim.spent * 1e8) > Math.round(dim.limit * 1e8)
        : dim.spent > dim.limit;
      if (already) {
        const amount = dim.name === "cost_usd" ? formatCost(dim.spent) : grouped(dim.spent);
        parts.push(dim.label + " are already " + amount + ", over the limit of " + limitText);
      } else {
        parts.push(dim.label + " would reach " + projectedText + ", over the limit of " + limitText);
      }
    } else {
      const ratio = dim.limit ? dim.projected / dim.limit : 0;
      const hypothetical = dim.name === "tokens"
        ? proposed.tokens > 0
        : dim.name === "tool_calls"
          ? proposed.tool_calls > 0
          : proposed.cost_usd > 0;
      parts.push(
        dim.label + " " + (hypothetical ? "would be at " : "at ") + pct(ratio) +
        " of " + limitText + " (warn at " + pct(warnRatio) + ")"
      );
    }
  });
  if (decision === "stop-and-ask-human") return "stop and ask a human: " + parts.join("; ");
  return "warn: " + parts.join("; ");
}

function emptyBudget() {
  return { version: 1, tasks: {} };
}

function spendOf(input) {
  const src = input || {};
  return {
    tokens: integer("tokens", src.tokens || 0),
    cost_usd: money("cost_usd", src.cost_usd || src.costUsd || 0),
    tool_calls: integer("tool_calls", src.tool_calls || src.toolCalls || 0),
  };
}

export function createBudget(store, options) {
  const opts = options || {};
  const ledger = jsonLedger(store, emptyBudget);
  const clock = opts.now || function () { return new Date(); };

  function requireTask(data, id) {
    const task = data.tasks && data.tasks[id];
    if (!task) throw new HarnessError("unknown task " + id + "; open a task first", { code: "budget" });
    return task;
  }

  return {
    async open(id, limits, reset) {
      const task = taskId(id);
      const cleaned = normalizeLimits(limits);
      return ledger.update(function (data) {
        if (!data.tasks) data.tasks = {};
        if (data.tasks[task] && !reset) {
          throw new HarnessError("task " + task + " already has a budget", { code: "budget" });
        }
        const now = clock().toISOString();
        data.tasks[task] = {
          task_id: task,
          opened_at: now,
          updated_at: now,
          limits: cleaned,
          spent: { tokens: 0, cost_usd: 0, tool_calls: 0 },
          events: [],
        };
        data.version = 1;
        return { write: true, data: data, result: budgetView(data.tasks[task], spendOf({})) };
      });
    },
    async ensure(id, limits) {
      const task = taskId(id);
      const existing = await this.status(task).catch(function () { return null; });
      if (existing) return existing;
      return this.open(task, limits, false);
    },
    async check(id, proposed) {
      const task = taskId(id);
      const spend = spendOf(proposed);
      return ledger.update(function (data) {
        return { write: false, data: data, result: budgetView(requireTask(data, task), spend) };
      });
    },
    async record(id, proposed) {
      const task = taskId(id);
      const spend = spendOf(proposed);
      const note = noteText(proposed && proposed.note);
      return ledger.update(function (data) {
        const row = requireTask(data, task);
        if (spend.tokens || spend.cost_usd || spend.tool_calls || note) {
          row.spent = {
            tokens: row.spent.tokens + spend.tokens,
            cost_usd: Math.round((row.spent.cost_usd + spend.cost_usd) * 1e8) / 1e8,
            tool_calls: row.spent.tool_calls + spend.tool_calls,
          };
          row.events.push({
            at: clock().toISOString(),
            tokens: spend.tokens,
            cost_usd: spend.cost_usd,
            tool_calls: spend.tool_calls,
            note: note,
          });
          if (row.events.length > 100) row.events = row.events.slice(-100);
          row.updated_at = clock().toISOString();
        }
        return { write: true, data: data, result: budgetView(row, spendOf({})) };
      });
    },
    async status(id) {
      const task = taskId(id);
      return ledger.update(function (data) {
        return { write: false, data: data, result: budgetView(requireTask(data, task), spendOf({})) };
      });
    },
    async reset(id) {
      const task = taskId(id);
      return ledger.update(function (data) {
        const row = requireTask(data, task);
        row.spent = { tokens: 0, cost_usd: 0, tool_calls: 0 };
        row.events = [];
        row.updated_at = clock().toISOString();
        return { write: true, data: data, result: budgetView(row, spendOf({})) };
      });
    },
  };
}

function bounded(name, value, limit, emptyOk) {
  if ((value == null || value === "") && emptyOk) return "";
  if (typeof value !== "string") throw new HarnessError(name + " must be a string", { code: "approval" });
  if (/[\u0000-\u001f]/.test(value)) throw new HarnessError(name + " must not contain control characters", { code: "approval" });
  const text = value.trim();
  if (!text && !emptyOk) throw new HarnessError(name + " must not be empty", { code: "approval" });
  if (text.length > limit) throw new HarnessError(name + " must be at most " + limit + " characters", { code: "approval" });
  return text;
}

function emptyApprovals() {
  return { version: 1, requests: {}, pending: {} };
}

function viewRequest(row) {
  return {
    task_id: row.task_id,
    request_id: row.request_id,
    status: row.status,
    question: row.question,
    action: row.action,
    asked_at: row.asked_at,
    answered_at: row.answered_at,
    by: row.by,
    note: row.note,
    answer_note: row.answer_note,
    result: row.result,
    ok: row.ok,
  };
}

function outcome(result, ok, row) {
  const body = viewRequest(row);
  body.result = result;
  body.ok = ok;
  return body;
}

function newRequestId(requests, createId) {
  if (createId) {
    const id = createId(requests);
    if (!REQUEST_ID.test(id)) throw new HarnessError("request id must look like req_ and 32 hex characters", { code: "approval" });
    return id;
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  let hex = "";
  for (let i = 0; i < bytes.length; i += 1) hex += bytes[i].toString(16).padStart(2, "0");
  return "req_" + hex;
}

export function createApprovals(store, options) {
  const opts = options || {};
  const ledger = jsonLedger(store, emptyApprovals);
  const clock = opts.now || function () { return new Date(); };
  return {
    async ask(id, question, extra) {
      const task = taskId(id);
      const info = extra || {};
      const asked = bounded("question", question, 1000, false);
      const action = bounded("action", info.action, 300, false);
      const note = bounded("note", info.note || "", 200, true);
      return ledger.update(function (data) {
        if (!data.requests) data.requests = {};
        if (!data.pending) data.pending = {};
        const current = data.pending[task];
        if (current) return { write: false, data: data, result: outcome("pending-exists", false, data.requests[current]) };
        const requestId = newRequestId(data.requests, opts.createId);
        const row = {
          request_id: requestId,
          task_id: task,
          question: asked,
          action: action,
          status: "pending",
          asked_at: clock().toISOString(),
          note: note,
          answered_at: null,
          by: "",
          answer_note: "",
        };
        data.requests[requestId] = row;
        data.pending[task] = requestId;
        data.version = 1;
        return { write: true, data: data, result: outcome("asked", true, row) };
      });
    },
    async answer(id, requestId, decision, extra) {
      const task = taskId(id);
      if (typeof requestId !== "string" || !REQUEST_ID.test(requestId)) {
        throw new HarnessError("request id must look like req_ and 32 hex characters", { code: "approval" });
      }
      if (decision !== "approved" && decision !== "denied") {
        throw new HarnessError("decision must be approved or denied", { code: "approval" });
      }
      const info = extra || {};
      const question = bounded("question", info.question, 1000, false);
      const action = bounded("action", info.action, 300, false);
      const by = bounded("by", info.by || "", 80, true);
      const note = bounded("note", info.note || "", 200, true);
      return ledger.update(function (data) {
        const row = data.requests && data.requests[requestId];
        if (!row) {
          return {
            write: false,
            data: data,
            result: {
              task_id: task,
              request_id: requestId,
              status: "none",
              question: null,
              action: null,
              result: "absent",
              ok: false,
            },
          };
        }
        if (row.task_id !== task || row.question !== question || row.action !== action) {
          return { write: false, data: data, result: outcome("mismatch", false, row) };
        }
        if (row.status !== "pending") return { write: false, data: data, result: outcome("already-decided", false, row) };
        row.status = decision;
        row.answered_at = clock().toISOString();
        row.by = by;
        row.answer_note = note;
        delete data.pending[task];
        return { write: true, data: data, result: outcome(decision, true, row) };
      });
    },
    async status(id, requestId) {
      const task = taskId(id);
      return ledger.update(function (data) {
        if (requestId) {
          const row = data.requests && data.requests[requestId];
          if (!row || row.task_id !== task) throw new HarnessError("no approval request " + requestId, { code: "approval" });
          return { write: false, data: data, result: viewRequest(row) };
        }
        const openId = data.pending && data.pending[task];
        if (openId) return { write: false, data: data, result: viewRequest(data.requests[openId]) };
        let latest = null;
        Object.keys(data.requests || {}).forEach(function (key) {
          const row = data.requests[key];
          if (row.task_id !== task || row.status === "pending") return;
          if (!latest || String(row.answered_at) > String(latest.answered_at)) latest = row;
        });
        if (!latest) {
          return {
            write: false,
            data: data,
            result: { task_id: task, request_id: null, status: "none", question: null, action: null },
          };
        }
        return { write: false, data: data, result: viewRequest(latest) };
      });
    },
    async list(id) {
      const task = id ? taskId(id) : null;
      return ledger.update(function (data) {
        const rows = [];
        Object.keys(data.requests || {}).sort().forEach(function (key) {
          const row = data.requests[key];
          if (task && row.task_id !== task) return;
          rows.push(viewRequest(row));
        });
        rows.sort(function (a, b) { return String(a.asked_at).localeCompare(String(b.asked_at)); });
        return { write: false, data: data, result: { count: rows.length, requests: rows } };
      });
    },
  };
}

export function createTrace(options) {
  const opts = options || {};
  const events = opts.events ? opts.events.slice() : [];
  return {
    events: events,
    async add(event) {
      const row = Object.assign({ t: new Date().toISOString() }, event || {});
      events.push(row);
      return row;
    },
    toJSONL() {
      if (!events.length) return "";
      return events.map(function (event) { return JSON.stringify(event); }).join("\n") + "\n";
    },
  };
}

function retryDelay(response, attempt) {
  let header = "";
  if (response && response.headers && typeof response.headers.get === "function") {
    header = response.headers.get("retry-after") || "";
  }
  const text = String(header).trim();
  if (/^\d+$/.test(text) && Number(text) > 0) return Number(text) * 1000;
  return 6000 * attempt;
}

export async function chatComplete(options) {
  const opts = options || {};
  const fetchImpl = opts.fetchImpl || globalThis.fetch;
  if (typeof fetchImpl !== "function") {
    throw new HarnessError("The request did not reach the endpoint. fetch is not available.", { code: "network" });
  }
  const base = String(opts.baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const url = base + "/chat/completions";
  const key = opts.apiKey || "";
  const sleep = opts.sleep || function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); };
  const attempts = opts.maxAttempts || 3;
  const body = {
    model: opts.model || DEFAULT_MODEL,
    messages: opts.messages || [],
    temperature: opts.temperature == null ? 0.2 : opts.temperature,
    max_tokens: opts.maxTokens || REPLY_TOKENS,
  };
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    let response;
    try {
      response = await fetchImpl(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + key,
        },
        body: JSON.stringify(body),
      });
    } catch (err) {
      const detail = redact(err && err.message, key);
      const generic = !detail || /failed to fetch|load failed|networkerror/i.test(detail);
      throw new HarnessError(
        generic
          ? "The request did not reach the endpoint. The browser may have blocked this origin, or the network failed."
          : "The request did not reach the endpoint. " + clip(detail),
        { code: "network" }
      );
    }
    if (response.status === 429 && attempt < attempts) {
      await sleep(retryDelay(response, attempt));
      continue;
    }
    const raw = await response.text();
    const safe = clip(redact(raw, key));
    if (response.status === 401) {
      throw new HarnessError("The endpoint rejected the API key (401).", { status: 401, code: "auth" });
    }
    if (response.status === 429) {
      throw new HarnessError(
        "The endpoint rate-limited this request (429). The public host allows about 10 requests a minute.",
        { status: 429, code: "rate_limit" }
      );
    }
    if (response.status >= 500) {
      throw new HarnessError("The endpoint returned " + response.status + ". " + safe, { status: response.status, code: "server" });
    }
    if (!response.ok) {
      throw new HarnessError("The endpoint returned " + response.status + ". " + safe, { status: response.status, code: "http" });
    }
    let data;
    try {
      data = JSON.parse(raw);
    } catch (err) {
      throw new HarnessError("The endpoint returned a body that is not JSON.", { code: "bad_body" });
    }
    const message = data && data.choices && data.choices[0] && data.choices[0].message;
    const content = message && typeof message.content === "string" ? message.content : "";
    return { content: content };
  }
  throw new HarnessError("The endpoint rate-limited this request (429). The public host allows about 10 requests a minute.", { status: 429, code: "rate_limit" });
}

export function createProvider(options) {
  const opts = options || {};
  return function provider(request) {
    const call = request || {};
    return chatComplete({
      fetchImpl: opts.fetchImpl,
      baseUrl: opts.baseUrl,
      apiKey: opts.apiKey,
      model: call.model || opts.model,
      temperature: call.temperature == null ? opts.temperature : call.temperature,
      maxTokens: call.maxTokens || opts.maxTokens,
      messages: call.messages,
      sleep: opts.sleep,
      maxAttempts: opts.maxAttempts,
    });
  };
}

function riskOf(tool, args) {
  const value = typeof tool.risk === "function" ? tool.risk(args || {}) : tool.risk;
  if (value !== "low" && value !== "medium" && value !== "high") {
    throw new HarnessError("tool " + tool.name + " has no risk level", { code: "tool" });
  }
  return value;
}

export function createTools() {
  return [
    {
      name: "clock",
      description: "Read the current UTC time. Low risk. args is an empty object.",
      schema: { type: "object", properties: {}, additionalProperties: false },
      risk: "low",
      async run(_args, ctx) {
        const current = (ctx.now || function () { return new Date(); })();
        return { iso: current.toISOString() };
      },
    },
    {
      name: "budget",
      description: "Estimate tokens, or check, record, and read the task budget. estimate, check, and status are low risk and check does not spend. record writes the ledger and is medium risk. args.action is estimate, check, record, or status.",
      schema: {
        type: "object",
        required: ["action"],
        properties: {
          action: { type: "string" },
          text: { type: "string" },
          tokens: { type: "integer" },
          tool_calls: { type: "integer" },
          cost_usd: { type: "number" },
          note: { type: "string" },
        },
      },
      risk: function (args) {
        return args && args.action === "record" ? "medium" : "low";
      },
      async run(args, ctx) {
        const action = args && args.action;
        if (action === "estimate") {
          return { tokens: estimateTokens(args.text || "") };
        }
        if (action === "check") return ctx.budget.check(ctx.taskId, args);
        if (action === "record") return ctx.budget.record(ctx.taskId, args);
        if (action === "status") return ctx.budget.status(ctx.taskId);
        throw new HarnessError("budget action must be estimate, check, record, or status", { code: "tool" });
      },
    },
    {
      name: "ask_human",
      description: "File a question for the person, or read approval status. Low risk. Does not grant approval. args.action is ask, status, or list. answer is reserved for the person.",
      schema: {
        type: "object",
        required: ["action"],
        properties: {
          action: { type: "string" },
          question: { type: "string" },
          note: { type: "string" },
        },
      },
      risk: "low",
      async run(args, ctx) {
        const action = args && args.action;
        if (action === "answer") {
          return { ok: false, error: "answer is reserved for the person using this page" };
        }
        if (action === "ask") {
          return ctx.approvals.ask(ctx.taskId, args.question || "", {
            action: args.note ? String(args.note) : "model asked a question",
            note: "",
          });
        }
        if (action === "status") return ctx.approvals.status(ctx.taskId);
        if (action === "list") return ctx.approvals.list(ctx.taskId);
        throw new HarnessError("ask_human action must be ask, status, or list", { code: "tool" });
      },
    },
  ];
}

function findTool(tools, name) {
  for (let i = 0; i < tools.length; i += 1) {
    if (tools[i].name === name) return tools[i];
  }
  return null;
}

function toolMessage(name, payload) {
  return "Tool result for " + name + ":\n" + JSON.stringify(payload);
}

function autonomyOf(value) {
  const mode = value || "supervised";
  if (AUTONOMY.indexOf(mode) < 0) throw new HarnessError("autonomy must be readonly, supervised, or full", { code: "config" });
  return mode;
}

async function finishTool(tool, args, ctx, trace, risk) {
  const started = Date.now();
  try {
    const payload = await tool.run(args || {}, ctx);
    await trace.add({ kind: "tool", name: tool.name, risk: risk, ok: true, ms: Date.now() - started });
    return { ok: true, text: toolMessage(tool.name, payload) };
  } catch (err) {
    const message = err && err.message ? err.message : "tool failed";
    await trace.add({ kind: "tool", name: tool.name, risk: risk, ok: false, ms: Date.now() - started, error: message });
    return { ok: false, text: toolMessage(tool.name, { ok: false, error: message }) };
  }
}

export async function runAgent(options) {
  const opts = options || {};
  const picked = selectSession(
    { global: opts.global, souls: opts.souls, sessions: opts.sessions },
    opts.sessionId
  );
  const globalProfile = opts.globalProfile || picked.global;
  const soul = opts.soul || picked.soul;
  const session = opts.session || picked.session;
  const config = resolveConfig(globalProfile, soul, session);
  const autonomy = autonomyOf(opts.autonomy || config.autonomy || "supervised");
  const tools = opts.tools || createTools();
  const trace = opts.trace || createTrace(opts.state && opts.state.traceEvents ? { events: opts.state.traceEvents } : {});
  const task = taskId((session && session.id) || opts.taskId || "demo");
  const limits = config.budget || {};
  if (opts.budget) await opts.budget.ensure(task, limits);
  const now = opts.now || function () { return new Date(); };
  const ctx = {
    now: now,
    taskId: task,
    budget: opts.budget,
    approvals: opts.approvals,
    fetch: opts.fetchImpl || globalThis.fetch,
  };
  const system = composeInstructions(globalProfile, soul, session) + "\n\n" + toolLines(tools);
  const contextTokens = config.contextTokens || CONTEXT_TOKENS;
  const replyTokens = config.maxReplyTokens || REPLY_TOKENS;
  let messages;
  let iteration;
  if (opts.state && opts.state.messages) {
    messages = opts.state.messages.map(function (message) {
      return { role: message.role, content: String(message.content || "") };
    });
    iteration = opts.state.nextIteration || 1;
  } else {
    messages = (opts.messages || []).map(function (message) {
      return { role: message.role, content: String(message.content || "") };
    });
    if (opts.input) messages.push({ role: "user", content: String(opts.input) });
    iteration = 1;
  }

  if (opts.state && opts.state.pending) {
    const pending = opts.state.pending;
    const decision = opts.decision;
    if (decision !== "approved" && decision !== "denied") {
      throw new HarnessError("resume needs an approved or denied decision", { code: "approval" });
    }
    const answered = await opts.approvals.answer(task, pending.request.request_id, decision, {
      question: pending.request.question,
      action: pending.request.action,
      by: opts.answeredBy || "person",
    });
    await trace.add({
      kind: "gate",
      autonomy: autonomy,
      tool: pending.name,
      risk: pending.risk,
      decision: decision,
      requestId: pending.request.request_id,
    });
    if (!answered.ok) {
      messages.push({ role: "user", content: toolMessage(pending.name, { ok: false, error: answered.result || "approval was not recorded" }) });
    } else if (decision === "denied") {
      messages.push({ role: "user", content: toolMessage(pending.name, { ok: false, error: "the person denied this tool" }) });
    } else {
      const tool = findTool(tools, pending.name);
      if (!tool) {
        messages.push({ role: "user", content: toolMessage(pending.name, { ok: false, error: "unknown tool" }) });
      } else if (opts.budget) {
        const gate = await opts.budget.check(task, { tool_calls: 1 });
        if (gate.decision === "stop-and-ask-human") {
          return finish(opts, {
            status: "budget-stop",
            text: gate.reason,
            messages: messages,
            trace: trace,
            iterations: Math.max(0, iteration - 1),
          });
        } else {
          const done = await finishTool(tool, pending.args, ctx, trace, pending.risk);
          await opts.budget.record(task, { tool_calls: 1, note: pending.name });
          messages.push({ role: "user", content: done.text });
        }
      } else {
        const done = await finishTool(tool, pending.args, ctx, trace, pending.risk);
        messages.push({ role: "user", content: done.text });
      }
    }
  }

  let lastText = "";
  for (; iteration <= MAX_ITERATIONS; iteration += 1) {
    const fitted = fitMessages({
      system: system,
      messages: messages,
      contextTokens: contextTokens,
      replyTokens: replyTokens,
    });
    let preflight = { decision: "continue", reason: "continue: within budget" };
    if (opts.budget) {
      preflight = await opts.budget.check(task, { tokens: fitted.tokens });
      await trace.add({
        kind: "budget",
        phase: "preflight",
        iteration: iteration,
        decision: preflight.decision,
        reason: preflight.reason,
        promptTokens: fitted.tokens,
      });
      if (preflight.decision === "stop-and-ask-human") {
        return finish(opts, {
          status: "budget-stop",
          text: preflight.reason,
          messages: messages,
          trace: trace,
          iterations: iteration - 1,
        });
      }
    }
    const started = Date.now();
    const completion = await opts.provider({
      messages: [{ role: "system", content: fitted.system }].concat(fitted.messages),
      model: config.model || DEFAULT_MODEL,
      temperature: config.temperature,
      maxTokens: replyTokens,
    });
    const text = completion && typeof completion.content === "string" ? completion.content : "";
    lastText = text;
    const replyTokensUsed = estimateTokens(text);
    if (opts.budget) {
      await opts.budget.record(task, { tokens: fitted.tokens + replyTokensUsed, note: "model" });
    }
    const parsed = parseToolCall(text);
    await trace.add({
      kind: "model",
      iteration: iteration,
      promptTokens: fitted.tokens,
      promptChars: Array.from(joinPrompt(fitted.system, fitted.messages)).length,
      replyTokens: replyTokensUsed,
      ms: Date.now() - started,
      decision: preflight.decision,
      dropped: fitted.dropped,
      tool: parsed.call ? parsed.call.name : "",
      ignored: parsed.ignored,
      malformed: parsed.malformed,
    });
    messages.push({ role: "assistant", content: text });
    if (!parsed.call) {
      return finish(opts, {
        status: "done",
        text: text,
        messages: messages,
        trace: trace,
        iterations: iteration,
      });
    }
    const tool = findTool(tools, parsed.call.name);
    if (!tool) {
      messages.push({ role: "user", content: toolMessage(parsed.call.name, { ok: false, error: "unknown tool" }) });
      continue;
    }
    const risk = riskOf(tool, parsed.call.args);
    if (autonomy === "readonly" && risk !== "low") {
      await trace.add({ kind: "gate", autonomy: autonomy, tool: tool.name, risk: risk, decision: "blocked" });
      messages.push({
        role: "user",
        content: toolMessage(tool.name, { ok: false, error: "readonly allows only low-risk tools" }),
      });
      continue;
    }
    if (autonomy === "supervised" && risk !== "low") {
      const question = "Allow " + tool.name + "? This tool is " + risk + " risk.";
      const action = tool.name + " " + JSON.stringify(parsed.call.args || {});
      const filed = await opts.approvals.ask(task, question, { action: action, note: risk });
      if (!filed.ok) {
        await trace.add({
          kind: "gate",
          autonomy: autonomy,
          tool: tool.name,
          risk: risk,
          decision: filed.result,
          requestId: filed.request_id,
        });
        messages.push({
          role: "user",
          content: toolMessage(tool.name, { ok: false, error: "a pending approval already exists for this task" }),
        });
        continue;
      }
      const choice = opts.approve ? await opts.approve(filed) : "pause";
      if (choice !== "approved" && choice !== "denied") {
        await trace.add({
          kind: "gate",
          autonomy: autonomy,
          tool: tool.name,
          risk: risk,
          decision: "pause",
          requestId: filed.request_id,
        });
        return finish(opts, {
          status: "paused",
          text: question,
          request: filed,
          messages: messages,
          trace: trace,
          iterations: iteration,
          state: {
            messages: messages,
            pending: { name: tool.name, args: parsed.call.args || {}, risk: risk, request: filed },
            nextIteration: iteration + 1,
            traceEvents: trace.events.slice(),
          },
        });
      }
      const answered = await opts.approvals.answer(task, filed.request_id, choice, {
        question: question,
        action: action,
        by: opts.answeredBy || "person",
      });
      await trace.add({
        kind: "gate",
        autonomy: autonomy,
        tool: tool.name,
        risk: risk,
        decision: choice,
        requestId: filed.request_id,
      });
      if (!answered.ok || choice === "denied") {
        messages.push({
          role: "user",
          content: toolMessage(tool.name, { ok: false, error: choice === "denied" ? "the person denied this tool" : answered.result }),
        });
        continue;
      }
    }
    if (opts.budget) {
      const callGate = await opts.budget.check(task, { tool_calls: 1 });
      if (callGate.decision === "stop-and-ask-human") {
        await trace.add({ kind: "budget", phase: "tool", decision: callGate.decision, reason: callGate.reason });
        return finish(opts, {
          status: "budget-stop",
          text: callGate.reason,
          messages: messages,
          trace: trace,
          iterations: iteration,
        });
      }
    }
    const done = await finishTool(tool, parsed.call.args, ctx, trace, risk);
    if (opts.budget) await opts.budget.record(task, { tool_calls: 1, note: tool.name });
    messages.push({ role: "user", content: done.text });
  }
  return finish(opts, {
    status: "capped",
    text: lastText || "Stopped after 3 iterations.",
    messages: messages,
    trace: trace,
    iterations: MAX_ITERATIONS,
  });
}

function finish(_opts, result) {
  result.traceText = result.trace.toJSONL();
  return result;
}
