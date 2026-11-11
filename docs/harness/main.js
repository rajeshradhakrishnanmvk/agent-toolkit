import {
  loadProfiles,
  resolveConfig,
  composeInstructions,
  createBudget,
  createApprovals,
  createTools,
  createTrace,
  createProvider,
  runAgent,
  runPair,
  stubDemoProvider,
  webStore,
  redact,
  createClaims,
} from "./harness.js";
import { connectEval, hashEvalSet, runEval } from "./eval.js";

const KEY_NAME = "agent-toolkit.harness.apiKey.v1";
const PAUSE_NAME = "agent-toolkit.harness.pause.v1";
const BUDGET_NAME = "agent-toolkit.harness.budget.v1";
const APPROVAL_NAME = "agent-toolkit.harness.approvals.v1";
const CLAIM_NAME = "agent-toolkit.harness.claims.v1";

const budget = createBudget(webStore(localStorage, BUDGET_NAME));
const approvals = createApprovals(webStore(localStorage, APPROVAL_NAME));
const claims = createClaims(webStore(localStorage, CLAIM_NAME));
const handoffBox = { value: null };
const tools = createTools();
const stubPreview = new URLSearchParams(location.search).get("stub") === "1";

const sessionEl = document.getElementById("session");
const autonomyEl = document.getElementById("autonomy");
const baseEl = document.getElementById("base-url");
const modelEl = document.getElementById("model");
const keyEl = document.getElementById("api-key");
const budgetEl = document.getElementById("budget-line");
const toolsEl = document.getElementById("tools");
const pendingEl = document.getElementById("pending");
const statusEl = document.getElementById("run-status");
const approvalEl = document.getElementById("approval");
const threadEl = document.getElementById("thread");
const systemEl = document.getElementById("system-view");
const errorEl = document.getElementById("error");
const promptEl = document.getElementById("prompt");
const runEl = document.getElementById("run");
const duoEl = document.getElementById("duo");
const evalRunEl = document.getElementById("run-eval");
const evalDownloadEl = document.getElementById("download-eval");
const evalProgressEl = document.getElementById("eval-progress");
const evalTableEl = document.getElementById("eval-table");
const evalBodyEl = document.getElementById("eval-body");
const evalNoteEl = document.getElementById("eval-note");

let evalFile = null;

let bundle = null;
let messages = [];
let trace = createTrace();
let busy = false;
let paused = null;

function setError(text) {
  const secret = stubPreview ? "" : keyEl.value.trim();
  errorEl.textContent = redact(text || "", secret);
}

function currentSession() {
  return bundle.sessions[sessionEl.value] || bundle.sessions[Object.keys(bundle.sessions)[0]];
}

function currentSoul(session) {
  const soulId = session && session.config && session.config.soulId;
  return bundle.souls[soulId] || bundle.souls[Object.keys(bundle.souls)[0]];
}

function currentConfig() {
  const session = currentSession();
  return resolveConfig(bundle.global, currentSoul(session), session);
}

function riskLabel(tool) {
  if (typeof tool.risk !== "function") return tool.risk;
  const listed = tool.risk({ action: "list" });
  const claim = tool.risk({ action: "claim" });
  const status = tool.risk({ action: "status" });
  const record = tool.risk({ action: "record" });
  if (listed !== claim) return listed + " for list, " + claim + " for claim";
  if (status !== record) return status + ", " + record + " for record";
  return status;
}

function renderTools() {
  toolsEl.textContent = "";
  tools.forEach(function (tool) {
    const item = document.createElement("li");
    item.textContent = tool.name + " · " + riskLabel(tool);
    toolsEl.appendChild(item);
  });
}

function renderSystem() {
  const session = currentSession();
  systemEl.textContent = composeInstructions(bundle.global, currentSoul(session), session);
}

function renderThread() {
  threadEl.textContent = "";
  messages.forEach(function (message) {
    const article = document.createElement("article");
    const content = String(message.content || "");
    const tool = content.indexOf("Tool result") === 0;
    const soul = message.soul ? message.soul.charAt(0).toUpperCase() + message.soul.slice(1) : "";
    article.className = "bubble " + (message.role === "assistant" ? "assistant" : "user") + (message.soul ? " " + message.soul : "");
    const who = document.createElement("p");
    who.className = "who";
    who.textContent = soul ? (tool ? soul + " · tool" : soul) : (message.role === "assistant" ? "Assistant" : tool ? "Tool" : "You");
    const body = document.createElement("pre");
    body.textContent = content;
    article.appendChild(who);
    article.appendChild(body);
    threadEl.appendChild(article);
  });
  threadEl.scrollTop = threadEl.scrollHeight;
}

async function renderBudget() {
  try {
    const status = await budget.status(currentSession().id);
    const limits = status.limits || {};
    budgetEl.textContent = status.decision
      + " · tokens " + status.spent.tokens + "/" + (limits.max_tokens == null ? "∞" : limits.max_tokens)
      + " · calls " + status.spent.tool_calls + "/" + (limits.max_tool_calls == null ? "∞" : limits.max_tool_calls);
  } catch (err) {
    budgetEl.textContent = "Budget opens on the first run.";
  }
}

async function renderPending() {
  pendingEl.textContent = "";
  let listed = { requests: [] };
  try {
    listed = await approvals.list(currentSession().id);
  } catch (err) {
    return;
  }
  const waiting = (listed.requests || []).filter(function (row) { return row.status === "pending"; });
  if (!waiting.length) {
    const empty = document.createElement("li");
    empty.textContent = "None.";
    pendingEl.appendChild(empty);
    return;
  }
  waiting.forEach(function (row) {
    const item = document.createElement("li");
    item.textContent = row.request_id + " · " + row.action;
    pendingEl.appendChild(item);
  });
}

function showPause(result) {
  paused = {
    sessionId: currentSession().id,
    autonomy: autonomyEl.value,
    state: result.state,
    request: result.request,
  };
  sessionStorage.setItem(PAUSE_NAME, JSON.stringify(paused));
  approvalEl.hidden = false;
  document.getElementById("approval-question").textContent = result.request.question;
  document.getElementById("approval-action").textContent = result.request.action;
  document.getElementById("approval-id").textContent = result.request.request_id;
  statusEl.textContent = "Paused until you approve or deny this tool.";
}

function clearPause() {
  paused = null;
  sessionStorage.removeItem(PAUSE_NAME);
  approvalEl.hidden = true;
}

function provider() {
  const config = currentConfig();
  return createProvider({
    baseUrl: baseEl.value.trim() || config.baseUrl,
    apiKey: keyEl.value.trim(),
    model: modelEl.value.trim() || config.model,
    temperature: config.temperature,
    maxTokens: config.maxReplyTokens,
  });
}

function agentOptions(extra) {
  return Object.assign({
    global: bundle.global,
    souls: bundle.souls,
    sessions: bundle.sessions,
    sessionId: currentSession().id,
    autonomy: autonomyEl.value,
    tools: tools,
    budget: budget,
    approvals: approvals,
    claims: claims,
    handoffBox: handoffBox,
    trace: trace,
    provider: provider(),
    answeredBy: "person",
    approve: async function () { return "pause"; },
  }, extra || {});
}

function applyResult(result) {
  messages = result.messages || messages;
  renderThread();
  if (result.status === "paused") {
    showPause(result);
    return;
  }
  clearPause();
  if (result.status === "done") statusEl.textContent = "Done.";
  else if (result.status === "budget-stop") statusEl.textContent = result.text;
  else if (result.status === "capped") statusEl.textContent = "Stopped after 3 iterations.";
  else statusEl.textContent = result.status;
}

async function turn(extra) {
  if (busy) return;
  busy = true;
  runEl.disabled = true;
  evalRunEl.disabled = true;
  setError("");
  try {
    const result = await runAgent(agentOptions(extra));
    applyResult(result);
  } catch (err) {
    setError(err && err.message ? err.message : "The turn failed.");
    statusEl.textContent = "The turn stopped with an error.";
  } finally {
    busy = false;
    runEl.disabled = false;
    evalRunEl.disabled = false;
    await renderBudget();
    await renderPending();
  }
}

async function boot() {
  try {
    bundle = await loadProfiles(fetch, new URL("./profiles/", import.meta.url).href);
  } catch (err) {
    statusEl.textContent = "Profiles did not load.";
    setError(err && err.message ? err.message : "Profiles did not load.");
    return;
  }
  sessionEl.textContent = "";
  Object.keys(bundle.sessions).sort().forEach(function (id) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = bundle.sessions[id].name || id;
    sessionEl.appendChild(option);
  });
  const config = currentConfig();
  autonomyEl.value = config.autonomy || "supervised";
  baseEl.value = config.baseUrl || "";
  modelEl.value = config.model || "";
  if (!stubPreview) keyEl.value = sessionStorage.getItem(KEY_NAME) || "";
  renderTools();
  renderSystem();
  try {
    await budget.ensure(currentSession().id, config.budget || {});
  } catch (err) {
    setError(err && err.message ? err.message : "");
  }
  await renderBudget();
  await renderPending();
  const saved = sessionStorage.getItem(PAUSE_NAME);
  if (saved) {
    try {
      const parsed = JSON.parse(saved);
      if (parsed && parsed.state && parsed.request && bundle.sessions[parsed.sessionId]) {
        sessionEl.value = parsed.sessionId;
        if (parsed.autonomy) autonomyEl.value = parsed.autonomy;
        messages = parsed.state.messages || [];
        trace = createTrace({ events: parsed.state.traceEvents || [] });
        renderThread();
        renderSystem();
        showPause({ state: parsed.state, request: parsed.request });
      }
    } catch (err) {
      sessionStorage.removeItem(PAUSE_NAME);
    }
  }
  if (!paused) {
    statusEl.textContent = stubPreview
      ? "Ready. Stubbed preview is on. No API key is sent."
      : "Ready.";
  }
  if (stubPreview) {
    evalNoteEl.textContent = "Stubbed preview. This run does not read the API key and does not call a model.";
  }
  try {
    const hash = await hashEvalSet();
    evalNoteEl.textContent += " Eval set tiny-tools version 1. Hash " + hash + ".";
  } catch (err) {
    evalNoteEl.textContent += " The eval hash could not be computed in this browser.";
  }
}

sessionEl.addEventListener("change", async function () {
  const here = currentSession().id;
  if (paused && paused.sessionId === here) {
    messages = paused.state.messages || [];
    trace = createTrace({ events: paused.state.traceEvents || [] });
    approvalEl.hidden = false;
    statusEl.textContent = "Paused until you approve or deny this tool.";
  } else {
    messages = [];
    trace = createTrace();
    approvalEl.hidden = true;
    statusEl.textContent = "Ready.";
  }
  renderThread();
  renderSystem();
  try {
    await budget.ensure(here, currentConfig().budget || {});
  } catch (err) {
    setError(err && err.message ? err.message : "");
  }
  await renderBudget();
  await renderPending();
});

keyEl.addEventListener("change", function () {
  const value = keyEl.value.trim();
  if (value) sessionStorage.setItem(KEY_NAME, value);
  else sessionStorage.removeItem(KEY_NAME);
});

document.getElementById("composer").addEventListener("submit", function (event) {
  event.preventDefault();
  if (paused) {
    setError("Approve or deny the waiting tool before the next message.");
    return;
  }
  const text = promptEl.value.trim();
  if (!text) {
    setError("Write a message first.");
    return;
  }
  if (!keyEl.value.trim()) {
    setError("Type an API key. It stays in this browser and is sent only to the endpoint above.");
    return;
  }
  sessionStorage.setItem(KEY_NAME, keyEl.value.trim());
  promptEl.value = "";
  turn({ messages: messages, input: text });
});

duoEl.addEventListener("click", function () {
  if (!bundle) return;
  if (paused) {
    setError("Approve or deny the waiting tool before the next message.");
    return;
  }
  if (!stubPreview && !keyEl.value.trim()) {
    setError("Type an API key. It stays in this browser and is sent only to the endpoint above.");
    return;
  }
  if (!stubPreview) sessionStorage.setItem(KEY_NAME, keyEl.value.trim());
  if (bundle && bundle.sessions.handoff) {
    sessionEl.value = "handoff";
    renderSystem();
  }
  messages = [];
  trace = createTrace();
  renderThread();
  turnDuo();
});

async function turnDuo() {
  if (busy) return;
  busy = true;
  runEl.disabled = true;
  duoEl.disabled = true;
  evalRunEl.disabled = true;
  setError("");
  try {
    const customPrompt = promptEl.value.trim();
    const result = await runPair({
      global: bundle.global,
      souls: bundle.souls,
      sessions: bundle.sessions,
      sessionId: "handoff",
      tools: tools,
      budget: budget,
      claims: claims,
      trace: trace,
      prompt: customPrompt || undefined,
      provider: stubPreview ? stubDemoProvider(customPrompt ? validDemoHandoff(customPrompt) : null) : provider(),
      onStep: function (snap) {
        messages = snap.messages;
        renderThread();
        statusEl.textContent = snap.status;
      },
    });
    messages = result.messages || messages;
    renderThread();
    if (result.status === "done") {
      setError("");
      statusEl.textContent = "Done. " + result.modelCalls + " model calls." + (stubPreview ? " Stubbed preview. No API key was sent." : "");
    } else if (result.status === "blocked" || result.status === "stopped") {
      statusEl.textContent = "Stopped.";
      setError(result.text || "The handoff stopped the run.");
    } else {
      statusEl.textContent = result.text || result.status;
      if (result.status === "budget-stop") setError(result.text || "");
    }
  } catch (err) {
    setError(err && err.message ? err.message : "The turn failed.");
    statusEl.textContent = "The turn stopped with an error.";
  } finally {
    busy = false;
    runEl.disabled = false;
    duoEl.disabled = false;
    evalRunEl.disabled = false;
    await renderBudget();
    await renderPending();
  }
}

document.getElementById("approve").addEventListener("click", function () {
  if (!paused || busy) return;
  const state = paused.state;
  clearPause();
  turn({ state: state, decision: "approved" });
});

document.getElementById("deny").addEventListener("click", function () {
  if (!paused || busy) return;
  const state = paused.state;
  clearPause();
  turn({ state: state, decision: "denied" });
});

document.getElementById("reset-budget").addEventListener("click", async function () {
  setError("");
  try {
    await budget.ensure(currentSession().id, currentConfig().budget || {});
    await budget.reset(currentSession().id);
    await renderBudget();
    statusEl.textContent = "Budget reset for this session.";
  } catch (err) {
    setError(err && err.message ? err.message : "Could not reset the budget.");
  }
});

function percent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return (Math.round(value * 1000) / 10).toFixed(1) + "%";
}

function evalRow(id, category, score) {
  let row = document.getElementById("eval-row-" + id);
  if (!row) {
    row = document.createElement("tr");
    row.id = "eval-row-" + id;
    evalBodyEl.appendChild(row);
  }
  row.textContent = "";
  [id, category, score].forEach(function (text) {
    const cell = document.createElement("td");
    cell.textContent = text;
    row.appendChild(cell);
  });
}

evalRunEl.addEventListener("click", function () {
  startEval();
});

evalDownloadEl.addEventListener("click", function () {
  if (!evalFile) return;
  const blob = new Blob([JSON.stringify(evalFile, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "tiny-tools-result.json";
  link.click();
  URL.revokeObjectURL(url);
});

async function startEval() {
  if (busy) return;
  let connection;
  try {
    connection = connectEval({
      stub: stubPreview,
      readKey: function () { return keyEl.value.trim(); },
      readLabel: function () { return document.getElementById("model-label").value; },
      readModel: function () { return modelEl.value; },
      connect: function () { return provider(); },
    });
  } catch (err) {
    setError(err && err.message ? err.message : "Add an API key before a live eval.");
    return;
  }
  if (!stubPreview) sessionStorage.setItem(KEY_NAME, keyEl.value.trim());
  busy = true;
  runEl.disabled = true;
  duoEl.disabled = true;
  evalRunEl.disabled = true;
  evalDownloadEl.disabled = true;
  evalFile = null;
  setError("");
  evalBodyEl.textContent = "";
  evalTableEl.hidden = false;
  evalProgressEl.textContent = connection.stub ? "Starting the stubbed preview…" : "Starting the eval…";
  try {
    const file = await runEval({
      stub: connection.stub,
      provider: connection.provider,
      label: connection.label,
      gapMs: connection.stub ? 0 : undefined,
      onProgress: function (event) {
        if (event.phase === "wait") {
          const seconds = Math.max(1, Math.round(event.waitMs / 1000));
          evalProgressEl.textContent = "Waiting " + seconds + "s to stay under 10 requests a minute.";
          return;
        }
        if (event.phase === "start") {
          evalProgressEl.textContent = "Task " + (event.index + 1) + " of " + event.total + ": " + event.id;
          evalRow(event.id, event.category, "…");
          return;
        }
        if (event.phase === "done") {
          evalRow(event.id, event.category, event.passed + "/" + event.totalChecks);
        }
      },
    });
    evalFile = file;
    const suffix = file.preview ? " Stubbed preview. No API key was read and no model was called." : "";
    evalProgressEl.textContent = "Score " + percent(file.scores.value) + " (" + file.scores.passed + "/" + file.scores.total + ")." + suffix;
    evalDownloadEl.disabled = false;
  } catch (err) {
    setError(err && err.message ? err.message : "The eval failed.");
    evalProgressEl.textContent = "The eval stopped with an error.";
  } finally {
    busy = false;
    runEl.disabled = false;
    duoEl.disabled = false;
    evalRunEl.disabled = false;
  }
}

document.getElementById("trace-download").addEventListener("click", function () {
  const blob = new Blob([trace.toJSONL()], { type: "application/jsonl" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "harness-trace.jsonl";
  link.click();
  URL.revokeObjectURL(url);
});

boot();
