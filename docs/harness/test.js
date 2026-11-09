import {
  mergeConfig,
  resolveConfig,
  composeInstructions,
  parseToolCall,
  estimateTokens,
  fitMessages,
  createBudget,
  createApprovals,
  createTools,
  createTrace,
  memoryStore,
  chatComplete,
  runAgent,
  loadProfiles,
} from "./harness.js";

function assert(condition, message) {
  if (!condition) throw new Error(message || "assertion failed");
}

function equal(actual, expected, message) {
  if (actual !== expected) {
    throw new Error((message || "not equal") + "\n  actual: " + JSON.stringify(actual) + "\n  expected: " + JSON.stringify(expected));
  }
}

function isNode() {
  return typeof process !== "undefined" && process.versions && process.versions.node;
}

function profiles(extra) {
  const over = extra || {};
  return {
    global: {
      id: "global",
      kind: "global",
      instructions: "Follow the notes.",
      config: Object.assign({
        autonomy: "supervised",
        contextTokens: 2048,
        maxReplyTokens: 256,
        model: "Llama-3.2-3B-Instruct",
        budget: { maxTokens: 12000, maxToolCalls: 20, maxCostUsd: 1, warnRatio: 0.8 },
      }, over.globalConfig || {}),
    },
    souls: {
      general: {
        id: "general",
        kind: "soul",
        instructions: "Be brief.",
        config: over.soulConfig || {},
      },
    },
    sessions: {
      demo: {
        id: "demo",
        kind: "session",
        instructions: "Demo session.",
        config: Object.assign({ soulId: "general", goals: "Answer.", notes: "Stay short." }, over.sessionConfig || {}),
      },
    },
  };
}

function requestId() {
  let n = 0;
  return function () {
    n += 1;
    return "req_" + String(n).padStart(32, "0");
  };
}

function jsonResponse(status, body, headers) {
  const head = headers || {};
  return {
    status: status,
    ok: status >= 200 && status < 300,
    headers: { get: function (name) { return head[String(name).toLowerCase()] || null; } },
    text: async function () { return typeof body === "string" ? body : JSON.stringify(body); },
  };
}

async function shippedProfiles() {
  if (isNode()) {
    const fs = await import("node:fs/promises");
    const path = await import("node:path");
    const url = await import("node:url");
    const dir = path.dirname(url.fileURLToPath(import.meta.url));
    const read = async function (rel) {
      return JSON.parse(await fs.readFile(path.join(dir, rel), "utf8"));
    };
    const manifest = await read("profiles/manifest.json");
    const globalProfile = await read("profiles/global.json");
    const souls = {};
    const sessions = {};
    for (let i = 0; i < manifest.souls.length; i += 1) {
      const soul = await read("profiles/souls/" + manifest.souls[i]);
      souls[soul.id] = soul;
    }
    for (let j = 0; j < manifest.sessions.length; j += 1) {
      const session = await read("profiles/sessions/" + manifest.sessions[j]);
      sessions[session.id] = session;
    }
    return { global: globalProfile, souls: souls, sessions: sessions };
  }
  return loadProfiles(globalThis.fetch, new URL("./profiles/", import.meta.url).href);
}

const cases = [];
function test(name, fn) { cases.push({ name: name, fn: fn }); }

test("profile merge nests objects, replaces arrays, and joins instructions", function () {
  const merged = mergeConfig(
    { temperature: 0.2, budget: { maxTokens: 10, maxToolCalls: 2 }, list: [1], drop: "keep" },
    { temperature: 0.4, budget: { maxTokens: 99 }, list: [2], drop: null }
  );
  equal(merged.temperature, 0.4);
  equal(merged.budget.maxTokens, 99);
  equal(merged.budget.maxToolCalls, 2);
  equal(merged.drop, "keep");
  equal(merged.list.length, 1);
  equal(merged.list[0], 2);
  const globalProfile = { instructions: "Global line.", config: { budget: { maxTokens: 10 } } };
  const soul = { instructions: "Soul line.", config: { temperature: 0.4 } };
  const session = { instructions: "Session line.", config: { goals: "finish the note", notes: "be careful", autonomy: "readonly" } };
  const config = resolveConfig(globalProfile, soul, session);
  equal(config.autonomy, "readonly");
  equal(config.temperature, 0.4);
  equal(config.budget.maxTokens, 10);
  const text = composeInstructions(globalProfile, soul, session);
  assert(text.indexOf("Global line.") < text.indexOf("Soul line."));
  assert(text.indexOf("Soul line.") < text.indexOf("Session line."));
  assert(text.indexOf("Session line.") < text.indexOf("Goals:"));
  assert(text.indexOf("finish the note") > 0);
  assert(text.indexOf("Project notes:") > 0);
  assert(text.indexOf("be careful") > text.indexOf("Project notes:"));
});

test("shipped demo profiles resolve to supervised general", async function () {
  const bundle = await shippedProfiles();
  const config = resolveConfig(bundle.global, bundle.souls.general, bundle.sessions.demo);
  equal(bundle.sessions.demo.config.soulId, "general");
  equal(config.autonomy, "supervised");
  equal(config.model, "Llama-3.2-3B-Instruct");
  equal(config.contextTokens, 2048);
  equal(config.maxReplyTokens, 256);
  equal(config.baseUrl, "https://chat.srv1536099.hstgr.cloud/v1");
  const text = composeInstructions(bundle.global, bundle.souls.general, bundle.sessions.demo);
  assert(text.indexOf("clock") > 0);
  assert(text.indexOf("Say the current time") > 0);
});

test("tool-call parser accepts a good call, a malformed call, and no call", function () {
  const good = parseToolCall("Looking.\n```tool\n{\"name\":\"clock\",\"args\":{}}\n```\n");
  equal(good.malformed, false);
  equal(good.call.name, "clock");
  equal(Object.keys(good.call.args).length, 0);
  const alias = parseToolCall("```tool\n{\"name\":\"budget\",\"arguments\":{\"action\":\"status\"}}\n```");
  equal(alias.call.name, "budget");
  equal(alias.call.args.action, "status");
  const trailing = parseToolCall("```tool\n{\"name\":\"clock\",\"args\":{},}\n```");
  equal(trailing.call.name, "clock");
  const broken = parseToolCall("```tool\n{nope}\n```");
  equal(broken.call, null);
  equal(broken.malformed, true);
  const plain = parseToolCall("The time is not in this message.");
  equal(plain.call, null);
  equal(plain.malformed, false);
  const jsonFence = parseToolCall("```json\n{\"name\":\"clock\",\"args\":{}}\n```");
  equal(jsonFence.call, null);
});

test("one tool per reply and a hard cap of 3 iterations", async function () {
  const ran = [];
  const tools = createTools().concat([{
    name: "marker",
    description: "Test tool.",
    risk: "low",
    async run() { ran.push("marker"); return { ok: true }; },
  }]);
  const clock = tools[0];
  const original = clock.run;
  clock.run = async function () { ran.push("clock"); return original.apply(clock, arguments); };
  let step = 0;
  const doubled = await runAgent({
    ...profiles(),
    sessionId: "demo",
    input: "mark it",
    tools: tools,
    budget: createBudget(memoryStore()),
    approvals: createApprovals(memoryStore(), { createId: requestId() }),
    provider: async function () {
      step += 1;
      if (step === 1) {
        return { content: "```tool\n{\"name\":\"clock\",\"args\":{}}\n```\n```tool\n{\"name\":\"marker\",\"args\":{}}\n```" };
      }
      return { content: "done" };
    },
  });
  equal(doubled.status, "done");
  equal(ran.join(","), "clock");
  equal(doubled.trace.events.filter(function (event) { return event.kind === "model"; })[0].ignored, 1);
  ran.length = 0;
  let calls = 0;
  const capped = await runAgent({
    ...profiles(),
    sessionId: "demo",
    input: "keep going",
    tools: tools,
    budget: createBudget(memoryStore()),
    provider: async function () {
      calls += 1;
      return { content: "```tool\n{\"name\":\"clock\",\"args\":{}}\n```" };
    },
  });
  equal(capped.status, "capped");
  equal(calls, 3);
  equal(ran.length, 3);
});

test("supervised approval pauses and resume runs the tool", async function () {
  const ran = [];
  const tools = [{
    name: "note",
    description: "Store a note.",
    risk: "medium",
    async run(args) { ran.push(args.text || ""); return { stored: true }; },
  }];
  const approvals = createApprovals(memoryStore(), { createId: requestId() });
  let calls = 0;
  const provider = async function () {
    calls += 1;
    if (calls === 1) return { content: "```tool\n{\"name\":\"note\",\"args\":{\"text\":\"hello\"}}\n```" };
    return { content: "saved" };
  };
  const paused = await runAgent({
    ...profiles(),
    sessionId: "demo",
    input: "store hello",
    tools: tools,
    autonomy: "supervised",
    budget: createBudget(memoryStore()),
    approvals: approvals,
    provider: provider,
    approve: async function () { return "pause"; },
  });
  equal(paused.status, "paused");
  equal(ran.length, 0);
  equal(paused.request.status, "pending");
  assert(paused.request.request_id.indexOf("req_") === 0);
  const resumed = await runAgent({
    ...profiles(),
    sessionId: "demo",
    tools: tools,
    autonomy: "supervised",
    budget: paused.trace && createBudget(memoryStore()),
    approvals: approvals,
    provider: provider,
    state: paused.state,
    decision: "approved",
    trace: paused.trace,
  });
  equal(resumed.status, "done");
  equal(resumed.text, "saved");
  equal(ran.length, 1);
  equal(ran[0], "hello");
  const status = await approvals.status("demo", paused.request.request_id);
  equal(status.status, "approved");
});

test("readonly blocks a medium-risk tool", async function () {
  const ran = [];
  const tools = [{
    name: "note",
    description: "Store a note.",
    risk: "medium",
    async run() { ran.push("ran"); return { stored: true }; },
  }];
  let calls = 0;
  const result = await runAgent({
    ...profiles(),
    sessionId: "demo",
    input: "store it",
    tools: tools,
    autonomy: "readonly",
    budget: createBudget(memoryStore()),
    provider: async function () {
      calls += 1;
      if (calls === 1) return { content: "```tool\n{\"name\":\"note\",\"args\":{}}\n```" };
      return { content: "I cannot store that." };
    },
  });
  equal(result.status, "done");
  equal(ran.length, 0);
  const gate = result.trace.events.filter(function (event) { return event.kind === "gate"; })[0];
  equal(gate.decision, "blocked");
  assert(result.messages.some(function (message) { return String(message.content).indexOf("readonly allows only low-risk tools") >= 0; }));
});

test("budget stop refuses the model call", async function () {
  let calls = 0;
  const budget = createBudget(memoryStore());
  const result = await runAgent({
    ...profiles({ globalConfig: { budget: { maxTokens: 1, maxToolCalls: 5, warnRatio: 0.8 } } }),
    sessionId: "demo",
    input: "What time is it and how much budget is left?",
    tools: createTools(),
    budget: budget,
    provider: async function () { calls += 1; return { content: "should not run" }; },
  });
  equal(result.status, "budget-stop");
  equal(calls, 0);
  assert(result.text.indexOf("stop and ask a human") === 0);
  const preflight = result.trace.events.filter(function (event) { return event.kind === "budget"; })[0];
  equal(preflight.decision, "stop-and-ask-human");
  const status = await budget.status("demo");
  equal(status.spent.tokens, 0);
  equal(status.decision, "continue");
});

test("trace records prompt size, tool calls, decisions, and timings", async function () {
  const trace = createTrace();
  const result = await runAgent({
    ...profiles(),
    sessionId: "demo",
    input: "hi",
    tools: createTools(),
    budget: createBudget(memoryStore()),
    trace: trace,
    now: function () { return new Date("2026-11-09T12:00:00.000Z"); },
    provider: async function () {
      return { content: "```tool\n{\"name\":\"clock\",\"args\":{}}\n```" };
    },
  });
  const text = result.traceText;
  assert(text.indexOf("\"kind\":\"model\"") >= 0);
  assert(text.indexOf("\"kind\":\"tool\"") >= 0);
  assert(text.indexOf("\"name\":\"clock\"") >= 0);
  assert(text.indexOf("promptTokens") >= 0);
  const model = trace.events.filter(function (event) { return event.kind === "model"; })[0];
  assert(typeof model.ms === "number");
  assert(model.promptTokens > 0);
  equal(model.decision, "continue");
  assert(text.indexOf("super-secret-value") < 0);
});

test("429 backs off and then accepts the next response", async function () {
  const secret = "super-secret-value";
  const waits = [];
  let calls = 0;
  const completion = await chatComplete({
    apiKey: secret,
    baseUrl: "https://chat.srv1536099.hstgr.cloud/v1",
    model: "Llama-3.2-3B-Instruct",
    messages: [{ role: "user", content: "ping" }],
    sleep: async function (ms) { waits.push(ms); },
    fetchImpl: async function () {
      calls += 1;
      if (calls === 1) return jsonResponse(429, "slow " + secret);
      return jsonResponse(200, { choices: [{ message: { content: "pong" } }] });
    },
  });
  equal(completion.content, "pong");
  equal(waits.length, 1);
  equal(waits[0], 6000);
  const waited = [];
  let retryCalls = 0;
  const second = await chatComplete({
    apiKey: secret,
    messages: [{ role: "user", content: "ping" }],
    sleep: async function (ms) { waited.push(ms); },
    fetchImpl: async function () {
      retryCalls += 1;
      if (retryCalls === 1) return jsonResponse(429, "later", { "retry-after": "2" });
      return jsonResponse(200, { choices: [{ message: { content: "pong" } }] });
    },
  });
  equal(second.content, "pong");
  equal(waited[0], 2000);
  let failed;
  try {
    await chatComplete({
      apiKey: secret,
      messages: [{ role: "user", content: "ping" }],
      fetchImpl: async function () { return jsonResponse(401, "bad " + secret); },
    });
  } catch (err) {
    failed = err;
  }
  assert(failed);
  equal(failed.status, 401);
  assert(failed.message.indexOf("401") >= 0);
  assert(failed.message.indexOf(secret) < 0);
  let server;
  try {
    await chatComplete({
      messages: [{ role: "user", content: "ping" }],
      fetchImpl: async function () { return jsonResponse(503, "down"); },
    });
  } catch (err) {
    server = err;
  }
  equal(server.status, 503);
  assert(server.message.indexOf("503") >= 0);
  let network;
  try {
    await chatComplete({
      apiKey: secret,
      messages: [{ role: "user", content: "ping" }],
      fetchImpl: async function () { throw new Error("socket closed " + secret); },
    });
  } catch (err) {
    network = err;
  }
  equal(network.code, "network");
  assert(network.message.indexOf(secret) < 0);
  let blocked;
  try {
    await chatComplete({
      messages: [{ role: "user", content: "ping" }],
      fetchImpl: async function () { throw new Error("Failed to fetch"); },
    });
  } catch (err) {
    blocked = err;
  }
  equal(blocked.code, "network");
  assert(blocked.message.indexOf("blocked this origin") >= 0);
});

test("demo question completes against a stubbed provider", async function () {
  const bundle = await shippedProfiles();
  const budget = createBudget(memoryStore());
  const approvals = createApprovals(memoryStore(), { createId: requestId() });
  let step = 0;
  let asked = false;
  const result = await runAgent({
    global: bundle.global,
    souls: bundle.souls,
    sessions: bundle.sessions,
    sessionId: "demo",
    input: "What time is it and how much budget is left?",
    tools: createTools(),
    budget: budget,
    approvals: approvals,
    now: function () { return new Date("2026-11-09T12:00:00.000Z"); },
    approve: async function () { asked = true; return "pause"; },
    provider: async function (request) {
      step += 1;
      if (step === 1) return { content: "```tool\n{\"name\":\"clock\",\"args\":{}}\n```" };
      if (step === 2) return { content: "```tool\n{\"name\":\"budget\",\"args\":{\"action\":\"status\"}}\n```" };
      const tools = request.messages.filter(function (message) {
        return String(message.content).indexOf("Tool result") === 0;
      }).map(function (message) { return message.content; }).join("\n");
      return { content: "Here is what the tools returned.\n" + tools };
    },
  });
  equal(result.status, "done");
  equal(asked, false);
  equal(step, 3);
  assert(result.text.indexOf("2026-11-09T12:00:00.000Z") >= 0);
  assert(result.text.indexOf("continue") >= 0);
  assert(result.traceText.indexOf("\"name\":\"clock\"") >= 0);
  assert(result.traceText.indexOf("\"name\":\"budget\"") >= 0);
  const status = await budget.status("demo");
  assert(status.spent.tokens > 0);
  equal(status.spent.tool_calls, 2);
});

test("history is trimmed to the 2048 context with a 256 reply reserve", function () {
  equal(estimateTokens(""), 0);
  equal(estimateTokens("abcd"), 1);
  equal(estimateTokens("abcde"), 2);
  const messages = [];
  for (let i = 0; i < 6; i += 1) {
    messages.push({ role: "user", content: "OLD-" + i + "-" + "x".repeat(2000) });
  }
  messages.push({ role: "user", content: "What time is it and how much budget is left?" });
  const fitted = fitMessages({ system: "System.", messages: messages, contextTokens: 2048, replyTokens: 256 });
  assert(fitted.tokens <= 2048 - 256);
  equal(fitted.budget, 1792);
  assert(fitted.dropped > 0);
  equal(fitted.messages[fitted.messages.length - 1].content, "What time is it and how much budget is left?");
  assert(fitted.messages.some(function (message) { return message.content.indexOf("OLD-0-") === 0; }) === false);
});

test("ask_human does not let the model record an answer", async function () {
  const approvals = createApprovals(memoryStore(), { createId: requestId() });
  const tools = createTools();
  const ask = tools[2];
  const ctx = { taskId: "demo", approvals: approvals, budget: createBudget(memoryStore()) };
  await ctx.budget.open("demo", { maxTokens: 100 });
  const denied = await ask.run({ action: "answer", question: "Allow it?" }, ctx);
  equal(denied.ok, false);
  const filed = await ask.run({ action: "ask", question: "May I record spend?" }, ctx);
  equal(filed.result, "asked");
  equal(filed.ok, true);
  const again = await ask.run({ action: "ask", question: "Another?" }, ctx);
  equal(again.result, "pending-exists");
  const listed = await ask.run({ action: "list" }, ctx);
  equal(listed.count, 1);
  equal(listed.requests[0].status, "pending");
});

export async function runTests() {
  const results = [];
  for (let i = 0; i < cases.length; i += 1) {
    const item = cases[i];
    try {
      await item.fn();
      results.push({ name: item.name, ok: true });
    } catch (err) {
      results.push({ name: item.name, ok: false, error: err && err.stack ? err.stack : String(err) });
    }
  }
  const failed = results.filter(function (item) { return !item.ok; }).length;
  return { passed: results.length - failed, failed: failed, results: results };
}

if (isNode()) {
  const summary = await runTests();
  summary.results.forEach(function (item) {
    if (item.ok) console.log("ok " + item.name);
    else console.log("FAIL " + item.name + "\n" + item.error);
  });
  console.log("harness tests: " + summary.passed + " passed, " + summary.failed + " failed");
  process.exit(summary.failed ? 1 : 0);
}
