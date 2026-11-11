/* Tiny-tools eval for the browser harness. Vanilla JavaScript. No network of its own. */

import {
  createApprovals,
  createBudget,
  memoryStore,
  parseToolCall,
  runAgent,
  HarnessError,
} from "./harness.js";

export const HARNESS_VERSION = "2";
export const EVAL_GAP_MS = 6500;
export const RESULT_SCHEMA_VERSION = 1;

const STANDARD_LIMITS = { maxTokens: 8000, maxToolCalls: 8, maxCostUsd: 1, warnRatio: 0.8 };
const TIGHT_LIMITS = { maxTokens: 8000, maxToolCalls: 1, maxCostUsd: 1, warnRatio: 0.8 };

function clockTool() {
  return {
    name: "clock",
    description: "Read the UTC time. Pass an empty args object.",
    risk: "low",
    mode: "const",
    fixed: { iso: "2026-01-01T00:00:00.000Z" },
    schema: { type: "object", additionalProperties: false, properties: {} },
  };
}

function lookupTool() {
  return {
    name: "lookup",
    description: "Look up a string key. Pass args key.",
    risk: "low",
    mode: "map",
    mapKey: "key",
    resultKey: "value",
    fallback: "missing",
    fixed: { token: "pebble", pebble: "stone" },
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["key"],
      properties: { key: { type: "string" } },
    },
  };
}

function echoTool() {
  return {
    name: "echo",
    description: "Repeat a text value. Pass args text.",
    risk: "low",
    mode: "echo",
    arg: "text",
    resultKey: "heard",
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["text"],
      properties: { text: { type: "string" } },
    },
  };
}

function noteTool() {
  return {
    name: "note",
    description: "Save a short note. Medium risk. Pass args text.",
    risk: "medium",
    mode: "echo",
    arg: "text",
    resultKey: "text",
    flag: "saved",
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["text"],
      properties: { text: { type: "string" } },
    },
  };
}

function openBudget(limits, prespend) {
  return { limits: limits, prespend: prespend };
}

export const EVAL_SPEC = {
  id: "tiny-tools",
  version: 1,
  temperature: 0,
  contextTokens: 2048,
  replyTokens: 256,
  maxIterations: 3,
  instructions: "Use at most one tool per reply. Do not invent tool results.",
  tasks: [
    {
      id: "pick-clock",
      category: "tool-choice",
      prompt: "What is the UTC time? Use the clock tool, then answer with the time.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [clockTool(), lookupTool()],
      checks: [
        { id: "tool-name", kind: "tool-name", call: 0, name: "clock" },
        { id: "args-schema", kind: "args-schema", call: 0 },
        { id: "final-time", kind: "final-contains", text: "2026" },
      ],
    },
    {
      id: "pick-lookup",
      category: "tool-choice",
      prompt: "Look up the key token. Use the lookup tool, then answer with the value.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [clockTool(), lookupTool()],
      checks: [
        { id: "tool-name", kind: "tool-name", call: 0, name: "lookup" },
        { id: "args-key", kind: "args-field", call: 0, field: "key", equals: "token" },
        { id: "args-schema", kind: "args-schema", call: 0 },
        { id: "final-value", kind: "final-contains", text: "pebble" },
      ],
    },
    {
      id: "args-clock",
      category: "arguments",
      prompt: "Call clock with an empty args object. Then answer with the time.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [clockTool(), lookupTool()],
      checks: [
        { id: "tool-name", kind: "tool-name", call: 0, name: "clock" },
        { id: "args-schema", kind: "args-schema", call: 0 },
        { id: "final-time", kind: "final-contains", text: "2026" },
      ],
    },
    {
      id: "args-lookup",
      category: "arguments",
      prompt: "Look up key token. Pass key as a string. Answer with the value.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [lookupTool(), clockTool()],
      checks: [
        { id: "tool-name", kind: "tool-name", call: 0, name: "lookup" },
        { id: "args-schema", kind: "args-schema", call: 0 },
        { id: "args-key", kind: "args-field", call: 0, field: "key", equals: "token" },
        { id: "final-value", kind: "final-contains", text: "pebble" },
      ],
    },
    {
      id: "plain-sum",
      category: "no-tool",
      prompt: "What is 2 + 2? Reply with the number. Do not use a tool.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [clockTool()],
      checks: [
        { id: "no-tool", kind: "no-tool" },
        { id: "final-number", kind: "final-number", text: "4" },
      ],
    },
    {
      id: "plain-sky",
      category: "no-tool",
      prompt: "Name the color of a clear daytime sky in one word. Do not use a tool.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [clockTool()],
      checks: [
        { id: "no-tool", kind: "no-tool" },
        { id: "final-color", kind: "final-contains", text: "blue" },
      ],
    },
    {
      id: "chain-echo",
      category: "multi-step",
      prompt: "Look up the key token. Then call echo with that value. Then answer with the value.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [lookupTool(), echoTool()],
      checks: [
        { id: "first-tool", kind: "tool-name", call: 0, name: "lookup" },
        { id: "first-key", kind: "args-field", call: 0, field: "key", equals: "token" },
        { id: "first-schema", kind: "args-schema", call: 0 },
        { id: "second-tool", kind: "tool-name", call: 1, name: "echo" },
        { id: "second-text", kind: "args-field", call: 1, field: "text", includes: "pebble" },
        { id: "second-schema", kind: "args-schema", call: 1 },
        { id: "final-value", kind: "final-contains", text: "pebble" },
      ],
    },
    {
      id: "chain-follow",
      category: "multi-step",
      prompt: "Look up the key token. Then look up the value that came back. Answer with the second value.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [lookupTool()],
      checks: [
        { id: "first-tool", kind: "tool-name", call: 0, name: "lookup" },
        { id: "first-key", kind: "args-field", call: 0, field: "key", equals: "token" },
        { id: "first-schema", kind: "args-schema", call: 0 },
        { id: "second-tool", kind: "tool-name", call: 1, name: "lookup" },
        { id: "second-key", kind: "args-field", call: 1, field: "key", equals: "pebble" },
        { id: "second-schema", kind: "args-schema", call: 1 },
        { id: "final-value", kind: "final-contains", text: "stone" },
      ],
    },
    {
      id: "deny-note",
      category: "approval",
      prompt: "Save a note whose text is hello. Use the note tool. If it is denied, say denied.",
      autonomy: "supervised",
      approval: "denied",
      budget: openBudget(STANDARD_LIMITS, null),
      tools: [noteTool()],
      checks: [
        { id: "tool-name", kind: "tool-name", call: 0, name: "note" },
        { id: "args-schema", kind: "args-schema", call: 0 },
        { id: "args-text", kind: "args-field", call: 0, field: "text", equals: "hello" },
        { id: "gate", kind: "gate", equals: "denied", tool: "note" },
        { id: "not-run", kind: "tool-not-ok", name: "note" },
        { id: "final-denied", kind: "final-contains", text: "denied" },
        { id: "final-no-success", kind: "final-excludes", text: "success" },
        { id: "final-no-successfully", kind: "final-excludes", text: "successfully" },
        { id: "final-no-saved", kind: "final-excludes", text: "saved" },
      ],
    },
    {
      id: "budget-clock",
      category: "budget",
      prompt: "Call the clock tool now.",
      autonomy: "supervised",
      approval: null,
      budget: openBudget(TIGHT_LIMITS, { tool_calls: 1 }),
      tools: [clockTool()],
      checks: [
        { id: "tool-name", kind: "tool-name", call: 0, name: "clock" },
        { id: "args-schema", kind: "args-schema", call: 0 },
        { id: "stopped", kind: "status", equals: "budget-stop" },
        { id: "not-run", kind: "tool-not-ok", name: "clock" },
        { id: "budget-event", kind: "trace-budget" },
        { id: "final-no-success", kind: "final-excludes", text: "success" },
      ],
    },
  ],
};

const PREVIEW = {
  "pick-clock": [toolFence("clock", {}), "The UTC time is 2026-01-01T00:00:00.000Z."],
  "pick-lookup": [toolFence("lookup", { key: "token" }), "pebble"],
  "args-clock": [toolFence("clock", {}), "2026-01-01T00:00:00.000Z"],
  "args-lookup": [toolFence("lookup", { key: "token" }), "The value is pebble."],
  "plain-sum": ["4"],
  "plain-sky": ["blue"],
  "chain-echo": [
    toolFence("lookup", { key: "token" }),
    toolFence("echo", { text: "pebble" }),
    "pebble",
  ],
  "chain-follow": [
    toolFence("lookup", { key: "token" }),
    toolFence("lookup", { key: "pebble" }),
    "stone",
  ],
  "deny-note": [toolFence("note", { text: "hello" }), "denied"],
  "budget-clock": [toolFence("clock", {})],
};

export function toolFence(name, args) {
  return "```tool\n" + JSON.stringify({ name: name, args: args }) + "\n```";
}

export function taskById(id) {
  const tasks = EVAL_SPEC.tasks;
  for (let i = 0; i < tasks.length; i += 1) {
    if (tasks[i].id === id) return tasks[i];
  }
  return null;
}

export function previewScript(id) {
  const lines = PREVIEW[id];
  return lines ? lines.slice() : null;
}

export function taskSetDocument() {
  return {
    id: EVAL_SPEC.id,
    version: EVAL_SPEC.version,
    temperature: EVAL_SPEC.temperature,
    contextTokens: EVAL_SPEC.contextTokens,
    replyTokens: EVAL_SPEC.replyTokens,
    maxIterations: EVAL_SPEC.maxIterations,
    instructions: EVAL_SPEC.instructions,
    tasks: EVAL_SPEC.tasks,
  };
}

export function canonicalJson(value) {
  return encodeCanonical(value);
}

function encodeCanonical(value) {
  if (value === null) return "null";
  const kind = typeof value;
  if (kind === "number") {
    if (!Number.isFinite(value)) throw new Error("canonical json needs finite numbers");
    return JSON.stringify(value);
  }
  if (kind === "boolean") return value ? "true" : "false";
  if (kind === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(encodeCanonical).join(",") + "]";
  if (kind === "object") {
    const keys = Object.keys(value).sort();
    return "{" + keys.map(function (key) {
      return JSON.stringify(key) + ":" + encodeCanonical(value[key]);
    }).join(",") + "}";
  }
  throw new Error("canonical json cannot encode " + kind);
}

function isNode() {
  return typeof process !== "undefined" && process.versions && process.versions.node;
}

export async function sha256Hex(text) {
  const data = String(text);
  if (isNode()) {
    const crypto = await import("node:crypto");
    return crypto.createHash("sha256").update(data).digest("hex");
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(data));
  const bytes = new Uint8Array(digest);
  let hex = "";
  for (let i = 0; i < bytes.length; i += 1) hex += bytes[i].toString(16).padStart(2, "0");
  return hex;
}

let cachedHash = "";

export async function hashEvalSet() {
  if (cachedHash) return cachedHash;
  cachedHash = await sha256Hex(canonicalJson(taskSetDocument()));
  return cachedHash;
}

export function round6(value) {
  return Math.round(value * 1000000) / 1000000;
}

export function tallyChecks(checks) {
  let passed = 0;
  const list = checks || [];
  list.forEach(function (check) {
    if (check && check.ok === true) passed += 1;
  });
  const total = list.length;
  return { passed: passed, total: total, value: total ? round6(passed / total) : 0 };
}

export function aggregateScores(tasks) {
  const buckets = {};
  const flat = [];
  (tasks || []).forEach(function (task) {
    const name = task.category || "";
    if (!buckets[name]) buckets[name] = [];
    (task.checks || []).forEach(function (check) {
      buckets[name].push(check);
      flat.push(check);
    });
  });
  const categories = {};
  Object.keys(buckets).sort().forEach(function (name) {
    categories[name] = tallyChecks(buckets[name]);
  });
  const total = tallyChecks(flat);
  return { passed: total.passed, total: total.total, value: total.value, categories: categories };
}

export function matchesSchema(value, schema) {
  if (!schema || !schema.type) return true;
  if (schema.type === "object") {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const props = schema.properties || {};
    const required = schema.required || [];
    for (let i = 0; i < required.length; i += 1) {
      if (value[required[i]] == null || value[required[i]] === "") return false;
    }
    const keys = Object.keys(value);
    for (let k = 0; k < keys.length; k += 1) {
      const key = keys[k];
      if (!Object.prototype.hasOwnProperty.call(props, key)) {
        if (schema.additionalProperties === false) return false;
        continue;
      }
      if (!matchesSchema(value[key], props[key])) return false;
    }
    return true;
  }
  if (schema.type === "string") return typeof value === "string";
  if (schema.type === "integer") return typeof value === "number" && Number.isInteger(value);
  if (schema.type === "number") return typeof value === "number" && Number.isFinite(value);
  if (schema.type === "boolean") return typeof value === "boolean";
  return false;
}

export function createEvalTool(spec) {
  const mode = spec.mode;
  const name = spec.name;
  return {
    name: name,
    description: spec.description,
    schema: spec.schema,
    risk: spec.risk,
    async run(args) {
      if (mode === "const") return clone(spec.fixed);
      if (mode === "map") {
        const key = args && args[spec.mapKey];
        const table = spec.fixed || {};
        const found = key != null && Object.prototype.hasOwnProperty.call(table, key);
        const out = {};
        out[spec.resultKey] = found ? table[key] : spec.fallback;
        return out;
      }
      if (mode === "echo") {
        const out = {};
        if (spec.flag) out[spec.flag] = true;
        out[spec.resultKey] = args && args[spec.arg] != null ? args[spec.arg] : "";
        return out;
      }
      throw new Error("unknown eval tool " + name);
    },
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

export function scriptedProvider(lines, modelId) {
  const script = lines || [];
  let index = 0;
  return async function () {
    const content = index < script.length ? script[index] : "";
    index += 1;
    return { content: content, model: modelId || "stub-preview" };
  };
}

export function connectEval(options) {
  const opts = options || {};
  if (opts.stub) return { stub: true, label: "stub preview" };
  const key = String(opts.readKey ? opts.readKey() : "").trim();
  if (!key) {
    throw new HarnessError("Type an API key. It stays in this browser and is sent only to the endpoint above.", { code: "auth" });
  }
  const label = String(opts.readLabel ? opts.readLabel() : "").trim();
  const model = String(opts.readModel ? opts.readModel() : "").trim();
  if (typeof opts.connect !== "function") throw new HarnessError("eval needs a provider", { code: "config" });
  return { stub: false, label: label || model || "unlabeled", provider: opts.connect() };
}

function profilesFor(task) {
  const limits = task.budget.limits;
  return {
    globalProfile: {
      id: "global",
      kind: "global",
      instructions: EVAL_SPEC.instructions,
      config: {
        autonomy: task.autonomy,
        temperature: EVAL_SPEC.temperature,
        contextTokens: EVAL_SPEC.contextTokens,
        maxReplyTokens: EVAL_SPEC.replyTokens,
        model: "eval",
        budget: {
          maxTokens: limits.maxTokens,
          maxToolCalls: limits.maxToolCalls,
          maxCostUsd: limits.maxCostUsd,
          warnRatio: limits.warnRatio,
        },
      },
    },
    soul: { id: "eval", kind: "soul", instructions: "", config: {} },
    session: { id: "eval", kind: "session", instructions: "", config: {} },
  };
}

function assistantCalls(messages) {
  const calls = [];
  (messages || []).forEach(function (message) {
    if (!message || message.role !== "assistant") return;
    const parsed = parseToolCall(message.content || "");
    if (parsed.call) calls.push(parsed.call);
  });
  return calls;
}

function hasToolFence(messages) {
  return (messages || []).some(function (message) {
    return message && message.role === "assistant" && /```tool\b/i.test(String(message.content || ""));
  });
}

function stripFences(text) {
  return String(text || "").replace(/```[\s\S]*?```/g, " ");
}

function finalText(result) {
  const parts = [];
  ((result && result.messages) || []).forEach(function (message) {
    if (!message || message.role !== "assistant") return;
    parts.push(stripFences(message.content || ""));
  });
  if (result && result.status === "budget-stop") parts.push(stripFences(result.text || ""));
  return parts.join(" ").replace(/\s+/g, " ").trim();
}

function eventsOf(result) {
  return (result && result.trace && result.trace.events) || [];
}

function findSpec(tools, name) {
  const list = tools || [];
  for (let i = 0; i < list.length; i += 1) {
    if (list[i].name === name) return list[i];
  }
  return null;
}

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function clipDetail(text) {
  const flat = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  if (flat.length > 160) return flat.slice(0, 157) + "...";
  return flat;
}

function applyCheck(check, ctx) {
  const index = check.call == null ? 0 : check.call;
  const call = ctx.calls[index];
  if (check.kind === "tool-name") {
    const actual = call ? call.name : "";
    return { ok: actual === check.name, detail: actual || "none" };
  }
  if (check.kind === "args-schema") {
    if (!call) return { ok: false, detail: "none" };
    const tool = findSpec(ctx.tools, call.name);
    if (!tool) return { ok: false, detail: "unknown" };
    const ok = matchesSchema(call.args || {}, tool.schema);
    return { ok: ok, detail: ok ? "match" : "mismatch" };
  }
  if (check.kind === "args-field") {
    if (!call || !call.args || !Object.prototype.hasOwnProperty.call(call.args, check.field)) {
      return { ok: false, detail: "none" };
    }
    const actual = call.args[check.field];
    if (check.equals != null) return { ok: actual === check.equals, detail: String(actual) };
    if (check.includes != null) {
      const ok = String(actual == null ? "" : actual).toLowerCase().indexOf(String(check.includes).toLowerCase()) >= 0;
      return { ok: ok, detail: String(actual) };
    }
    return { ok: false, detail: "unset" };
  }
  if (check.kind === "no-tool") {
    const ok = ctx.calls.length === 0 && !hasToolFence(ctx.result && ctx.result.messages);
    return { ok: ok, detail: ok ? "none" : "called" };
  }
  if (check.kind === "final-contains") {
    const ok = ctx.text.toLowerCase().indexOf(String(check.text).toLowerCase()) >= 0;
    return { ok: ok, detail: ok ? check.text : "missing" };
  }
  if (check.kind === "final-excludes") {
    const pattern = new RegExp("\\b" + escapeRegExp(String(check.text)) + "\\b", "i");
    const ok = !pattern.test(ctx.text);
    return { ok: ok, detail: ok ? "clear" : check.text };
  }
  if (check.kind === "final-number") {
    const pattern = new RegExp("(^|[^0-9])" + escapeRegExp(String(check.text)) + "([^0-9]|$)");
    const ok = pattern.test(ctx.text);
    return { ok: ok, detail: ok ? check.text : "missing" };
  }
  if (check.kind === "status") {
    const actual = (ctx.result && ctx.result.status) || "";
    return { ok: actual === check.equals, detail: actual || "none" };
  }
  if (check.kind === "gate") {
    const hit = eventsOf(ctx.result).some(function (event) {
      if (event.kind !== "gate" || event.decision !== check.equals) return false;
      if (check.tool && event.tool !== check.tool) return false;
      return true;
    });
    return { ok: hit, detail: hit ? check.equals : "missing" };
  }
  if (check.kind === "tool-not-ok") {
    const ran = eventsOf(ctx.result).some(function (event) {
      return event.kind === "tool" && event.name === check.name && event.ok === true;
    });
    return { ok: !ran, detail: ran ? "ran" : "not-run" };
  }
  if (check.kind === "trace-budget") {
    const hit = eventsOf(ctx.result).some(function (event) {
      return event.kind === "budget" && event.decision === "stop-and-ask-human";
    });
    return { ok: hit, detail: hit ? "stop-and-ask-human" : "missing" };
  }
  return { ok: false, detail: "unknown check" };
}

export function gradeTask(task, result) {
  const calls = assistantCalls(result && result.messages);
  const text = finalText(result);
  const checks = (task.checks || []).map(function (check) {
    const outcome = applyCheck(check, { calls: calls, text: text, result: result, tools: task.tools });
    return { id: check.id, ok: outcome.ok === true, detail: clipDetail(outcome.detail) };
  });
  const score = tallyChecks(checks);
  return {
    id: task.id,
    category: task.category,
    checks: checks,
    passed: score.passed,
    total: score.total,
    value: score.value,
    status: (result && result.status) || "",
    trace: result && result.trace && result.trace.events ? clone(result.trace.events) : [],
  };
}

function failedGrade(task) {
  const checks = (task.checks || []).map(function (check) {
    return { id: check.id, ok: false, detail: "run failed" };
  });
  const score = tallyChecks(checks);
  return {
    id: task.id,
    category: task.category,
    checks: checks,
    passed: score.passed,
    total: score.total,
    value: score.value,
    status: "error",
    trace: [],
  };
}

export async function runEvalTask(task, options) {
  const opts = options || {};
  if (typeof opts.provider !== "function") throw new HarnessError("eval needs a provider", { code: "config" });
  const profiles = profilesFor(task);
  const tools = (task.tools || []).map(createEvalTool);
  const budget = createBudget(memoryStore());
  await budget.open("eval", task.budget.limits);
  if (task.budget.prespend) await budget.record("eval", task.budget.prespend);
  const approvals = createApprovals(memoryStore());
  try {
    const result = await runAgent({
      globalProfile: profiles.globalProfile,
      soul: profiles.soul,
      session: profiles.session,
      autonomy: task.autonomy,
      tools: tools,
      budget: budget,
      approvals: approvals,
      input: task.prompt,
      provider: opts.provider,
      approve: async function () {
        if (task.approval === "approved" || task.approval === "denied") return task.approval;
        return "pause";
      },
    });
    const graded = gradeTask(task, result);
    graded.promptTokens = maxPromptTokens(result);
    return graded;
  } catch (err) {
    return failedGrade(task);
  }
}

function maxPromptTokens(result) {
  let largest = 0;
  eventsOf(result).forEach(function (event) {
    if (event.kind === "model" && event.promptTokens > largest) largest = event.promptTokens;
  });
  return largest;
}

export function redactText(text) {
  let out = String(text == null ? "" : text);
  out = out.replace(/sk-[A-Za-z0-9]{8,}/g, "[redacted]");
  out = out.replace(/AKIA[0-9A-Z]{16}/g, "[redacted]");
  out = out.replace(/ghp_[A-Za-z0-9]{20,}/g, "[redacted]");
  out = out.replace(/authorization/gi, "[redacted]");
  out = out.replace(new RegExp("bearer" + "\\s+\\S+", "gi"), "[redacted]");
  out = out.replace(new RegExp("https?" + ":\\/\\/\\S+", "gi"), "[redacted]");
  out = out.replace(/srv1536099/g, "[redacted]");
  return out;
}

const DROPPED_FIELDS = {
  apikey: true,
  api_key: true,
  authorization: true,
  endpoint: true,
  baseurl: true,
  base_url: true,
  url: true,
  secret: true,
};

export function sanitizeValue(value) {
  if (typeof value === "string") return redactText(value);
  if (typeof value === "number" || typeof value === "boolean" || value == null) return value;
  if (Array.isArray(value)) return value.map(sanitizeValue);
  if (typeof value === "object") {
    const out = {};
    Object.keys(value).forEach(function (key) {
      if (DROPPED_FIELDS[String(key).toLowerCase()]) return;
      out[key] = sanitizeValue(value[key]);
    });
    return out;
  }
  return redactText(String(value));
}

export function buildExport(tasks, meta) {
  const info = meta || {};
  const cleanTasks = sanitizeValue((tasks || []).map(function (task) {
    return {
      id: task.id,
      category: task.category,
      passed: task.passed,
      total: task.total,
      value: task.value,
      checks: task.checks,
      trace: task.trace || [],
    };
  }));
  const scores = aggregateScores(cleanTasks);
  return {
    schemaVersion: RESULT_SCHEMA_VERSION,
    evalSet: {
      id: EVAL_SPEC.id,
      version: EVAL_SPEC.version,
      hash: info.hash || "",
    },
    model: {
      label: redactText(info.label || ""),
      id: redactText(info.modelId || ""),
    },
    harnessVersion: HARNESS_VERSION,
    runDate: info.runDate || "",
    preview: info.preview === true,
    scores: scores,
    tasks: cleanTasks,
  };
}

export async function runEval(options) {
  const opts = options || {};
  const list = opts.tasks || EVAL_SPEC.tasks;
  const stub = !!opts.stub;
  const gapMs = opts.gapMs == null ? (stub ? 0 : EVAL_GAP_MS) : opts.gapMs;
  const runDate = opts.runDate || new Date().toISOString();
  if (!stub && typeof opts.provider !== "function") throw new HarnessError("eval needs a provider", { code: "config" });
  let modelId = stub ? "stub-preview" : "";
  let lastStart = 0;
  const paced = async function (request) {
    const now = Date.now();
    const wait = lastStart ? gapMs - (now - lastStart) : 0;
    if (wait > 0) {
      if (opts.onProgress) opts.onProgress({ phase: "wait", waitMs: wait });
      await sleep(wait);
    }
    lastStart = Date.now();
    const completion = await opts.provider(request);
    if (completion && typeof completion.model === "string" && completion.model) modelId = completion.model;
    return completion;
  };
  const graded = [];
  for (let i = 0; i < list.length; i += 1) {
    const task = list[i];
    if (opts.onProgress) {
      opts.onProgress({ phase: "start", index: i, total: list.length, id: task.id, category: task.category });
    }
    let provider = paced;
    if (stub) {
      const lines = PREVIEW[task.id];
      if (!lines) throw new HarnessError("missing preview script " + task.id, { code: "eval" });
      provider = scriptedProvider(lines, "stub-preview");
    }
    const row = await runEvalTask(task, { provider: provider });
    graded.push(row);
    if (opts.onProgress) {
      opts.onProgress({
        phase: "done",
        index: i,
        total: list.length,
        id: task.id,
        category: task.category,
        passed: row.passed,
        totalChecks: row.total,
        value: row.value,
      });
    }
  }
  const hash = await hashEvalSet();
  return buildExport(graded, {
    hash: hash,
    label: opts.label || (stub ? "stub preview" : ""),
    modelId: modelId,
    runDate: runDate,
    preview: stub && opts.preview !== false,
  });
}
