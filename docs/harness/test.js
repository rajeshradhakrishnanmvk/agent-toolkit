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
  runPair,
  loadProfiles,
  checkHandoff,
  renderTemplate,
  parseHandoff,
  HandoffError,
  createClaims,
  ClaimError,
  stubDemoProvider,
  validDemoHandoff,
  blockedDemoHandoff,
  DEMO_MODEL_CAP,
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

function validHandoff(extra) {
  const payload = {
    goal: "Publish the week 2 row on the project page.",
    owner: "writer-agent",
    budget_left: "4000 tokens",
    constraints: ["Leave the visit counter script unchanged."],
    acceptance_criteria: ["The week 2 row shows a Live badge."],
    questions: [],
    artifacts: [],
  };
  Object.keys(extra || {}).forEach(function (key) { payload[key] = extra[key]; });
  return payload;
}

test("handoff accepts a complete document and rejects an incomplete one", function () {
  const ready = checkHandoff(validHandoff());
  equal(ready.status, "ready");
  equal(ready.ok_to_hand_off, true);
  equal(ready.summary, "ready to hand off");
  equal(ready.problems.length, 0);
  equal(ready.max_bytes, 16 * 1024);
  const missing = checkHandoff({
    goal: "   ",
    acceptance_criteria: [],
    constraints: ["Leave the visit counter script unchanged."],
    budget_left: "4000 tokens",
  });
  equal(missing.status, "needs-fixes");
  equal(missing.ok_to_hand_off, false);
  const byField = {};
  missing.problems.forEach(function (problem) { byField[problem.field] = problem; });
  equal(byField.goal.code, "missing-field");
  assert(byField.goal.message.indexOf("is empty") >= 0);
  assert(byField.acceptance_criteria.message.indexOf("is empty") >= 0);
  assert(byField.owner.message.indexOf("is missing") >= 0);
  assert(!byField.constraints);
  const blocked = checkHandoff(validHandoff({ budget_left: "0" }));
  equal(blocked.status, "blocked");
  equal(blocked.ok_to_hand_off, false);
  equal(blocked.problems[0].code, "budget-exhausted");
  assert(blocked.summary.indexOf("blocked:") === 0);
  const vague = checkHandoff(validHandoff({ acceptance_criteria: ["update the page properly"] }));
  equal(vague.status, "needs-fixes");
  equal(vague.problems[0].code, "vague-criterion");
  const schema = {
    required: ["ticket"],
    properties: { ticket: { type: "string", minLength: 3 } },
    additionalProperties: false,
  };
  const needsTicket = checkHandoff(validHandoff(), { schema: schema });
  equal(needsTicket.problems[0].field, "ticket");
  assert(needsTicket.problems[0].message.indexOf("schema requires it") >= 0);
  const withTicket = checkHandoff(validHandoff({ ticket: "AT-12" }), { schema: schema });
  equal(withTicket.status, "ready", JSON.stringify(withTicket.problems));
  let broke = false;
  try {
    checkHandoff(validHandoff(), { schema: { anyOf: [] } });
  } catch (err) {
    broke = err;
  }
  assert(broke instanceof HandoffError);
  const tiny = checkHandoff(validHandoff(), { maxBytes: 32 });
  equal(tiny.status, "blocked");
  equal(tiny.problems[tiny.problems.length - 1].code, "too-large");
  assert(tiny.problems[tiny.problems.length - 1].message.indexOf("cap is 32") >= 0);
});

test("handoff template is blank and markdown round-trips", function () {
  ["markdown", "json"].forEach(function (fmt) {
    const text = renderTemplate(fmt);
    const decision = checkHandoff(text);
    equal(decision.status, "needs-fixes");
    equal(decision.problems.map(function (problem) { return problem.field; }).join(","), "goal,acceptance_criteria,owner,constraints,budget_left");
    assert(decision.problems.every(function (problem) { return problem.code === "missing-field"; }));
  });
  let bad = false;
  try {
    renderTemplate("yaml");
  } catch (err) {
    bad = err;
  }
  assert(bad instanceof HandoffError);
  const document = [
    "---",
    "goal: Publish the week 2 row.",
    "owner: writer-agent",
    "budget_left:",
    "  tokens: 4000",
    "  cost_usd: 0.05",
    "constraints:",
    "  - \"note: leave the counter alone\"",
    "acceptance_criteria:",
    "  - The week 2 row shows a Live badge.",
    "questions:",
    "  - question: Which tone?",
    "    answer: Match week 1.",
    "---",
    "",
    "Ship it.",
    "",
  ].join("\n");
  const parsed = parseHandoff(document);
  equal(parsed.fields.budget_left.tokens, 4000);
  equal(parsed.fields.budget_left.cost_usd, 0.05);
  equal(parsed.fields.constraints[0], "note: leave the counter alone");
  equal(parsed.fields.questions[0].question, "Which tone?");
  equal(parsed.fields.questions[0].answer, "Match week 1.");
  const decision = checkHandoff(document);
  equal(decision.status, "ready", JSON.stringify(decision.problems));
  equal(decision.format, "markdown");
  const found = checkHandoff(validHandoff({
    artifacts: ["docs/index.html", "./docs/index.html"],
    acceptance_criteria: ["docs/index.html shows a Live badge."],
  }), { files: { "docs/index.html": true } });
  equal(found.status, "ready", JSON.stringify(found.problems));
  equal(found.paths.found.join(","), "docs/index.html");
  const absent = checkHandoff(validHandoff({ artifacts: ["docs/no-such-page.md"] }), { files: { "docs/index.html": true } });
  equal(absent.paths.missing.join(","), "docs/no-such-page.md");
  equal(absent.problems[0].code, "missing-path");
});

test("a second claim is busy until a stale lease can be taken over", async function () {
  const now = [new Date("2026-11-02T00:00:00.000Z")];
  const store = memoryStore();
  const claims = createClaims(store, { now: function () { return now[0]; } });
  const taken = await claims.take("fix-flaky-test", "agent-a", { ttlSeconds: 60, note: "editing" });
  equal(taken.result, "taken");
  equal(taken.ok, true);
  equal(taken.state, "active");
  equal(taken.expires_in, 60);
  equal(taken.note, "editing");
  const before = await store.read();
  now[0] = new Date(now[0].getTime() + 10000);
  const held = await claims.take("fix-flaky-test", "agent-a", { ttlSeconds: 60 });
  equal(held.result, "held");
  equal(held.expires_in, 50);
  equal(await store.read(), before);
  const busy = await claims.take("fix-flaky-test", "agent-b", { ttlSeconds: 60 });
  equal(busy.result, "busy");
  equal(busy.ok, false);
  equal(busy.owner, "agent-a");
  equal(await store.read(), before);
  now[0] = new Date("2026-11-02T00:00:59.000Z");
  equal((await claims.take("fix-flaky-test", "agent-b")).result, "busy");
  now[0] = new Date("2026-11-02T00:01:00.000Z");
  const stale = await claims.list({ state: "stale" });
  equal(stale.count, 1);
  equal(stale.claims[0].state, "expired");
  equal(stale.claims[0].takeable, true);
  const over = await claims.take("fix-flaky-test", "agent-b", { ttlSeconds: 30 });
  equal(over.result, "taken-over");
  equal(over.previous_owner, "agent-a");
  equal(over.owner, "agent-b");
  equal(over.expires_in, 30);
  assert(!over.note);
  const denied = await claims.heartbeat("fix-flaky-test", "agent-a");
  equal(denied.result, "not-owner");
  const beat = await claims.heartbeat("fix-flaky-test", "agent-b");
  equal(beat.result, "renewed");
  equal(beat.expires_in, 30);
  const released = await claims.release("fix-flaky-test", "agent-b");
  equal(released.result, "released");
  equal(released.ok, true);
  const absent = await claims.release("missing-item", "agent-a");
  equal(absent.result, "absent");
  let bad = false;
  try {
    await claims.take("-nope", "agent-a");
  } catch (err) {
    bad = err;
  }
  assert(bad instanceof ClaimError);
});

test("work_claim and handoff sit in the registry with their risk", function () {
  const tools = createTools();
  const handoff = tools.filter(function (tool) { return tool.name === "handoff"; })[0];
  const claim = tools.filter(function (tool) { return tool.name === "work_claim"; })[0];
  equal(handoff.risk, "low");
  equal(claim.risk({ action: "list" }), "low");
  equal(claim.risk({ action: "claim" }), "medium");
  equal(claim.risk({ action: "release" }), "medium");
  equal(claim.risk({ action: "heartbeat" }), "medium");
  equal(JSON.stringify(handoff.schema).length < 400, true);
  equal(JSON.stringify(claim.schema).length < 400, true);
  equal(tools[0].name, "clock");
  equal(tools[2].name, "ask_human");
});

test("two agents hand off in 4 model calls, and a blocked handoff stops the run", async function () {
  const bundle = await shippedProfiles();
  async function drive(handoff) {
    let calls = 0;
    let largest = 0;
    const provider = stubDemoProvider(handoff);
    const result = await runPair({
      global: bundle.global,
      souls: bundle.souls,
      sessions: bundle.sessions,
      sessionId: "handoff",
      tools: createTools(),
      budget: createBudget(memoryStore()),
      claims: createClaims(memoryStore()),
      provider: async function (request) {
        calls += 1;
        const blob = request.messages.map(function (message) { return message.content; }).join("\n");
        const tokens = estimateTokens(blob);
        if (tokens > largest) largest = tokens;
        assert(tokens <= 2048 - 256);
        return provider(request);
      },
    });
    result.calls = calls;
    result.largest = largest;
    return result;
  }
  const ready = await drive(validDemoHandoff());
  equal(ready.status, "done");
  equal(ready.text, "Paris is the capital of France.");
  assert(ready.modelCalls <= DEMO_MODEL_CAP);
  equal(ready.modelCalls, ready.calls);
  equal(ready.calls, 4);
  const souls = {};
  ready.trace.events.forEach(function (event) { if (event.soul) souls[event.soul] = true; });
  equal(souls.planner, true);
  equal(souls.writer, true);
  assert(ready.traceText.indexOf("\"name\":\"work_claim\"") >= 0);
  assert(ready.traceText.indexOf("\"name\":\"handoff\"") >= 0);
  assert(ready.messages.some(function (message) { return message.soul === "planner"; }));
  assert(ready.messages.some(function (message) { return message.soul === "writer"; }));
  assert(ready.trace.events.filter(function (event) { return event.kind === "model"; }).every(function (event) {
    return event.dropped === 0;
  }));
  const blocked = await drive(blockedDemoHandoff());
  equal(blocked.status, "blocked");
  assert(blocked.modelCalls <= DEMO_MODEL_CAP);
  assert(blocked.text.indexOf("blocked") === 0);
  assert(blocked.text.indexOf("exhausted") >= 0);
  assert(blocked.messages.some(function (message) { return message.soul === "writer" && String(message.content).indexOf("exhausted") >= 0; }));
  assert(blocked.traceText.indexOf("\"soul\":\"planner\"") >= 0);
  assert(blocked.traceText.indexOf("\"soul\":\"writer\"") >= 0);
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
