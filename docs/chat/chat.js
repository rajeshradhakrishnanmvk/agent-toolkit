(function () {
  "use strict";

  var DEFAULTS = {
    baseUrl: "https://chat.srv1536099.hstgr.cloud/v1",
    model: "Llama-3.2-3B-Instruct",
    maxTokens: 256,
    temperature: 0.2,
    systemPrompt: "You are a concise helpful assistant.",
    maxBudgetTokens: 12000,
    maxToolCalls: 120,
    maxBudgetCost: 0.05,
    rememberKey: true,
  };
  var TIPS = [
    "Protect emergency fund first (3-6 months expenses).",
    "Avoid concentration risk and keep diversification broad.",
    "Prefer low-cost, transparent products when options are similar.",
    "Match debt prepayment vs investing with your risk and horizon.",
  ];
  var CONV_KEY = "agent-toolkit.chat.conversations.v1";
  var SETTINGS_KEY = "agent-toolkit.chat.settings.v1";
  var KEY_NAME = "agent-toolkit.chat.apiKey.v1";

  var state = loadState();
  var settings = loadSettings();
  var busy = false;

  var listEl = document.getElementById("conv-list");
  var threadEl = document.getElementById("thread");
  var titleEl = document.getElementById("title");
  var budgetEl = document.getElementById("budget");
  var formEl = document.getElementById("composer");
  var inputEl = document.getElementById("message");
  var sendEl = document.getElementById("send");
  var sidebarEl = document.getElementById("sidebar");
  var menuEl = document.getElementById("menu");
  var dialogEl = document.getElementById("settings");

  document.getElementById("new-chat").addEventListener("click", function () {
    createConversation();
    closeSidebar();
  });
  menuEl.addEventListener("click", function () {
    var open = sidebarEl.classList.toggle("open");
    menuEl.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.getElementById("open-settings").addEventListener("click", openSettings);
  document.getElementById("forget-key").addEventListener("click", function () {
    localStorage.removeItem(KEY_NAME);
    sessionStorage.removeItem(KEY_NAME);
    document.getElementById("api-key").value = "";
  });
  document.getElementById("settings-form").addEventListener("submit", function () {
    readSettingsForm();
  });
  formEl.addEventListener("submit", function (event) {
    event.preventDefault();
    sendCurrent();
  });

  render();

  function loadState() {
    try {
      var raw = localStorage.getItem(CONV_KEY);
      if (!raw) return freshState();
      var data = JSON.parse(raw);
      if (!data || !Array.isArray(data.conversations)) return freshState();
      if (!data.conversations.length) data.conversations.push(blankConversation());
      if (!findConv(data, data.activeId)) data.activeId = data.conversations[0].id;
      return data;
    } catch (err) {
      return freshState();
    }
  }

  function freshState() {
    var first = blankConversation();
    return { activeId: first.id, conversations: [first] };
  }

  function blankConversation() {
    return {
      id: "c_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
      title: "New chat",
      createdAt: Date.now(),
      updatedAt: Date.now(),
      messages: [],
      turn: 0,
      spent: { tokens: 0, costUsd: 0, toolCalls: 0 },
      lastTrace: null,
      autoTrace: false,
    };
  }

  function findConv(data, id) {
    for (var i = 0; i < data.conversations.length; i += 1) {
      if (data.conversations[i].id === id) return data.conversations[i];
    }
    return null;
  }

  function active() {
    return findConv(state, state.activeId) || state.conversations[0];
  }

  function saveState() {
    try {
      localStorage.setItem(CONV_KEY, JSON.stringify(state));
    } catch (err) {
      pushLocal(active(), "This browser refused to store the conversation. It may be out of space.");
    }
  }

  function loadSettings() {
    var stored = {};
    try {
      stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {};
    } catch (err) {
      stored = {};
    }
    var settingsOut = {
      baseUrl: cleanUrl(stored.baseUrl) || DEFAULTS.baseUrl,
      model: textOr(stored.model, DEFAULTS.model),
      maxTokens: positiveInt(stored.maxTokens, DEFAULTS.maxTokens),
      temperature: numberOr(stored.temperature, DEFAULTS.temperature),
      systemPrompt: textOr(stored.systemPrompt, DEFAULTS.systemPrompt),
      maxBudgetTokens: positiveInt(stored.maxBudgetTokens, DEFAULTS.maxBudgetTokens),
      maxToolCalls: positiveInt(stored.maxToolCalls, DEFAULTS.maxToolCalls),
      maxBudgetCost: DEFAULTS.maxBudgetCost,
      rememberKey: stored.rememberKey !== false,
    };
    return settingsOut;
  }

  function saveSettings() {
    var copy = {
      baseUrl: settings.baseUrl,
      model: settings.model,
      maxTokens: settings.maxTokens,
      temperature: settings.temperature,
      systemPrompt: settings.systemPrompt,
      maxBudgetTokens: settings.maxBudgetTokens,
      maxToolCalls: settings.maxToolCalls,
      rememberKey: settings.rememberKey,
    };
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(copy));
  }

  function apiKey() {
    if (settings.rememberKey) return localStorage.getItem(KEY_NAME) || "";
    return sessionStorage.getItem(KEY_NAME) || "";
  }

  function setApiKey(value) {
    var key = (value || "").trim();
    if (settings.rememberKey) {
      sessionStorage.removeItem(KEY_NAME);
      if (key) localStorage.setItem(KEY_NAME, key);
      else localStorage.removeItem(KEY_NAME);
    } else {
      localStorage.removeItem(KEY_NAME);
      if (key) sessionStorage.setItem(KEY_NAME, key);
      else sessionStorage.removeItem(KEY_NAME);
    }
  }

  function openSettings() {
    document.getElementById("base-url").value = settings.baseUrl;
    document.getElementById("model").value = settings.model;
    document.getElementById("api-key").value = apiKey();
    document.getElementById("remember-key").checked = settings.rememberKey;
    document.getElementById("max-tokens").value = String(settings.maxTokens);
    document.getElementById("temperature").value = String(settings.temperature);
    document.getElementById("budget-tokens").value = String(settings.maxBudgetTokens);
    document.getElementById("budget-tools").value = String(settings.maxToolCalls);
    if (typeof dialogEl.showModal === "function") dialogEl.showModal();
    else dialogEl.setAttribute("open", "");
  }

  function readSettingsForm() {
    settings.baseUrl = cleanUrl(document.getElementById("base-url").value) || DEFAULTS.baseUrl;
    settings.model = textOr(document.getElementById("model").value.trim(), DEFAULTS.model);
    settings.maxTokens = positiveInt(document.getElementById("max-tokens").value, DEFAULTS.maxTokens);
    settings.temperature = numberOr(document.getElementById("temperature").value, DEFAULTS.temperature);
    settings.maxBudgetTokens = positiveInt(document.getElementById("budget-tokens").value, DEFAULTS.maxBudgetTokens);
    settings.maxToolCalls = positiveInt(document.getElementById("budget-tools").value, DEFAULTS.maxToolCalls);
    settings.rememberKey = document.getElementById("remember-key").checked;
    setApiKey(document.getElementById("api-key").value);
    saveSettings();
    render();
  }

  function createConversation() {
    var conv = blankConversation();
    state.conversations.unshift(conv);
    state.activeId = conv.id;
    saveState();
    render();
    inputEl.focus();
  }

  function render() {
    var conv = active();
    titleEl.textContent = conv.title;
    budgetEl.textContent = budgetLine(conv, statusDecision(conv));
    listEl.textContent = "";
    state.conversations.forEach(function (item) {
      var row = document.createElement("li");
      row.className = "conv-row" + (item.id === conv.id ? " active" : "");
      var pick = document.createElement("button");
      pick.type = "button";
      pick.className = "pick";
      pick.textContent = item.title;
      pick.addEventListener("click", function () {
        state.activeId = item.id;
        saveState();
        closeSidebar();
        render();
      });
      var rename = document.createElement("button");
      rename.type = "button";
      rename.textContent = "Rename";
      rename.addEventListener("click", function () {
        startRename(row, item);
      });
      var remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Delete";
      remove.addEventListener("click", function () {
        deleteConversation(item.id);
      });
      row.appendChild(pick);
      row.appendChild(rename);
      row.appendChild(remove);
      listEl.appendChild(row);
    });
    threadEl.textContent = "";
    conv.messages.forEach(function (message) {
      var bubble = document.createElement("article");
      bubble.className = "bubble " + message.kind;
      var body = document.createElement("div");
      body.textContent = message.content;
      bubble.appendChild(body);
      if (message.meta) {
        var meta = document.createElement("p");
        meta.className = "meta";
        meta.textContent = message.meta;
      bubble.appendChild(meta);
      }
      if (message.trace && (conv.autoTrace || message.forceTrace)) {
        var trace = document.createElement("pre");
        trace.className = "trace";
        trace.textContent = message.trace;
        bubble.appendChild(trace);
      }
      threadEl.appendChild(bubble);
    });
    threadEl.scrollTop = threadEl.scrollHeight;
    sendEl.disabled = busy;
  }

  function startRename(row, item) {
    var input = document.createElement("input");
    input.type = "text";
    input.value = item.title;
    input.setAttribute("aria-label", "Conversation name");
    var pick = row.querySelector(".pick");
    row.replaceChild(input, pick);
    input.focus();
    input.select();
    function finish(save) {
      if (save) {
        var next = input.value.trim();
        if (next) item.title = next.slice(0, 80);
        item.updatedAt = Date.now();
        saveState();
      }
      render();
    }
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") finish(true);
      if (event.key === "Escape") finish(false);
    });
    input.addEventListener("blur", function () {
      finish(true);
    });
  }

  function deleteConversation(id) {
    var item = findConv(state, id);
    if (!item) return;
    if (!window.confirm("Delete “" + item.title + "”? This removes it from this browser only.")) return;
    state.conversations = state.conversations.filter(function (conv) {
      return conv.id !== id;
    });
    if (!state.conversations.length) state.conversations.push(blankConversation());
    if (!findConv(state, state.activeId)) state.activeId = state.conversations[0].id;
    saveState();
    render();
  }

  function closeSidebar() {
    sidebarEl.classList.remove("open");
    menuEl.setAttribute("aria-expanded", "false");
  }

  function sendCurrent() {
    if (busy) return;
    var text = inputEl.value.trim();
    if (!text) return;
    var conv = active();
    inputEl.value = "";
    if (text.charAt(0) === "/") {
      handleCommand(conv, text);
      saveState();
      render();
      return;
    }
    if (conv.title === "New chat") conv.title = text.slice(0, 48);
    conv.messages.push({ kind: "user", content: text });
    conv.updatedAt = Date.now();
    saveState();
    render();
    runTurn(conv, text);
  }

  function handleCommand(conv, text) {
    var cmd = text.trim().toLowerCase();
    conv.messages.push({ kind: "user", content: text });
    if (cmd === "/help") {
      pushLocal(
        conv,
        "/help show commands\n/model show the active model\n/status show the local budget\n/trace show the latest simulated trace\n/trace on|off print the trace after each answer\n/quit does not close this page"
      );
      return;
    }
    if (cmd === "/model") {
      pushLocal(conv, "active model: " + settings.model);
      return;
    }
    if (cmd === "/status") {
      pushLocal(conv, budgetLine(conv, statusDecision(conv)));
      return;
    }
    if (cmd === "/trace") {
      conv.messages.push({
        kind: "local",
        content: "Latest trace",
        trace: formatTrace(conv.lastTrace),
        forceTrace: true,
      });
      return;
    }
    if (cmd === "/trace on" || cmd === "/trace off") {
      conv.autoTrace = cmd === "/trace on";
      conv.messages.push({
        kind: "local",
        content: "trace> auto trace " + (conv.autoTrace ? "enabled" : "disabled"),
        trace: formatTrace(conv.lastTrace),
        forceTrace: true,
      });
      return;
    }
    if (cmd.indexOf("/trace") === 0) {
      pushLocal(conv, "trace> usage: /trace or /trace on|off");
      return;
    }
    if (cmd === "/quit" || cmd === "/exit") {
      pushLocal(conv, "This page stays open. Delete the conversation from the list if you are done with it.");
      return;
    }
    pushLocal(conv, "Unknown command. /help lists the ones this page understands.");
  }

  function runTurn(conv, userText) {
    conv.turn += 1;
    var projected = estimateTokens(userText) + 220;
    var gate = checkBudget(conv, projected, 1);
    var trace = {
      turn: conv.turn,
      outcome: "",
      error: "",
      handoffs: [],
      claims: [],
      gate: gate.decision,
    };
    if (gate.decision === "stop-and-ask-human") {
      trace.outcome = "blocked-by-budget";
      conv.lastTrace = trace;
      conv.messages.push({
        kind: "assistant",
        content: "I need human approval before continuing: " + gate.reason,
        meta: budgetLine(conv, gate.decision),
        trace: formatTrace(trace),
      });
      saveState();
      render();
      return;
    }

    busy = true;
    render();
    var turnId = "turn-" + String(conv.turn).padStart(4, "0") + "-" + slug(userText).slice(0, 24);
    var intake = "Clarify goals, timeline, cash flow, debt obligations, and risk tolerance. Current prompt: " + userText;
    var analysis =
      "User request: " + userText + "\nIntake summary: " + intake + "\nPlanning note: " + TIPS[(conv.turn - 1) % TIPS.length];
    trace.handoffs = [
      { stage: "intake->analysis", text: intake },
      { stage: "analysis->advisor", text: analysis },
    ];
    trace.claims = [
      "take " + turnId + ":intake owner=intake-bot",
      "done " + turnId + ":intake owner=intake-bot",
      "take " + turnId + ":analysis owner=analysis-bot",
      "done " + turnId + ":analysis owner=analysis-bot",
      "take " + turnId + ":advisor owner=advisor-bot",
    ];
    var messages = [
      {
        role: "system",
        content:
          settings.systemPrompt +
          "\nYou are an educational financial consultant bot. Provide practical steps, assumptions, and risks. Do not claim guaranteed returns.",
      },
      {
        role: "user",
        content: "Use the internal brief to answer the user.\n\nINTAKE:\n" + intake + "\n\nANALYSIS:\n" + analysis + "\n\nUSER:\n" + userText,
      },
    ];
    record(conv, estimateTokens(intake));
    record(conv, estimateTokens(analysis));
    complete(messages).then(function (reply) {
      record(conv, estimateTokens(reply));
      trace.claims.push("done " + turnId + ":advisor owner=advisor-bot");
      trace.outcome = "ok";
      conv.lastTrace = trace;
      var decision = statusDecision(conv);
      conv.messages.push({
        kind: "assistant",
        content: reply,
        meta: budgetLine(conv, decision),
        trace: formatTrace(trace),
      });
    }).catch(function (err) {
      trace.outcome = "workflow-error";
      trace.error = err.message || String(err);
      conv.lastTrace = trace;
      conv.messages.push({
        kind: "error",
        content: explainError(err),
        trace: formatTrace(trace),
        forceTrace: true,
      });
    }).then(function () {
      busy = false;
      conv.updatedAt = Date.now();
      saveState();
      render();
    });
  }

  function complete(messages) {
    var key = apiKey();
    if (!key) {
      return Promise.reject(makeError(0, "Missing API key. Open Settings and enter one. It stays in this browser."));
    }
    var url = settings.baseUrl.replace(/\/+$/, "") + "/chat/completions";
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = controller ? window.setTimeout(function () { controller.abort(); }, 60000) : 0;
    return fetch(url, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + key,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: settings.model,
        messages: messages,
        max_tokens: settings.maxTokens,
        temperature: settings.temperature,
      }),
      signal: controller ? controller.signal : undefined,
    }).then(function (response) {
      return response.text().then(function (body) {
        if (!response.ok) throw makeError(response.status, stripKey(body));
        var parsed;
        try {
          parsed = JSON.parse(body);
        } catch (err) {
          throw makeError(response.status, "The endpoint did not return JSON.");
        }
        var content = parsed && parsed.choices && parsed.choices[0] && parsed.choices[0].message && parsed.choices[0].message.content;
        if (typeof content !== "string") throw makeError(response.status, "Unexpected response shape.");
        return content.trim();
      });
    }).catch(function (err) {
      if (err && err.status) throw err;
      if (err && err.name === "AbortError") throw makeError(0, "The request timed out after 60 seconds.");
      throw makeError(0, err && err.message ? err.message : "Network error");
    }).then(function (text) {
      if (timer) window.clearTimeout(timer);
      return text;
    }, function (err) {
      if (timer) window.clearTimeout(timer);
      throw err;
    });
  }

  function explainError(err) {
    var status = err && err.status;
    var detail = stripKey((err && err.detail) || "");
    if (!status) {
      if (/missing api key/i.test(detail)) return detail;
      if (/timed out/i.test(detail)) return detail;
      return (
        "The browser could not complete the request (network or CORS). " +
        "A no-key preflight to the default endpoint succeeded for https://rajeshradhakrishnanmvk.github.io, " +
        "and a browser on localhost received HTTP 401 rather than a CORS block. " +
        "If you opened this file directly, or the host refused your origin, the browser stops the call. " +
        "This page does not change the server. " +
        detail
      );
    }
    if (status === 401) return "The endpoint rejected the API key (401). Check Settings. The key stays in this browser. " + clip(detail);
    if (status === 429) return "The endpoint rate-limited this request (429). The public host allows about 10 requests a minute. Wait and try again. " + clip(detail);
    if (status >= 500) return "The endpoint returned " + status + ". " + clip(detail);
    return "The endpoint returned " + status + ". " + clip(detail);
  }

  function makeError(status, detail) {
    var err = new Error(detail || "");
    err.status = status;
    err.detail = detail || "";
    return err;
  }

  function stripKey(text) {
    var key = apiKey();
    var out = String(text || "");
    if (key) out = out.split(key).join("[key]");
    return out;
  }

  function clip(text) {
    var flat = String(text || "").replace(/\s+/g, " ").trim();
    if (flat.length > 280) return flat.slice(0, 277) + "...";
    return flat;
  }

  function checkBudget(conv, addTokens, addTools) {
    var tokenState = dimensionState(conv.spent.tokens, addTokens, settings.maxBudgetTokens);
    var toolState = dimensionState(conv.spent.toolCalls, addTools, settings.maxToolCalls);
    var decision = "continue";
    if (tokenState === "stop" || toolState === "stop") decision = "stop-and-ask-human";
    else if (tokenState === "warn" || toolState === "warn") decision = "warn";
    if (decision === "continue") return { decision: decision, reason: "continue: within budget" };
    var parts = [];
    if (tokenState === (decision === "stop-and-ask-human" ? "stop" : "warn") || (decision === "warn" && tokenState === "warn")) {
      parts.push(tokenReason(conv, addTokens, decision));
    }
    if (toolState === "stop" || (decision === "warn" && toolState === "warn")) {
      parts.push(toolReason(conv, addTools, decision));
    }
    var prefix = decision === "stop-and-ask-human" ? "stop and ask a human: " : "warn: ";
    return { decision: decision, reason: prefix + parts.filter(Boolean).join("; ") };
  }

  function dimensionState(spent, add, limit) {
    var projected = spent + add;
    if (projected > limit) return "stop";
    if (limit > 0 && projected / limit >= 0.8) return "warn";
    return "ok";
  }

  function tokenReason(conv, add, decision) {
    var limit = settings.maxBudgetTokens;
    var projected = conv.spent.tokens + add;
    if (decision === "stop-and-ask-human") {
      if (conv.spent.tokens > limit) {
        return "tokens are already " + grouped(conv.spent.tokens) + ", over the limit of " + grouped(limit);
      }
      return "tokens would reach " + grouped(projected) + ", over the limit of " + grouped(limit);
    }
    return "tokens would be at " + percent(projected / limit) + " of " + grouped(limit) + " (warn at 80%)";
  }

  function toolReason(conv, add, decision) {
    var limit = settings.maxToolCalls;
    var projected = conv.spent.toolCalls + add;
    if (decision === "stop-and-ask-human") {
      if (conv.spent.toolCalls > limit) {
        return "tool calls are already " + grouped(conv.spent.toolCalls) + ", over the limit of " + grouped(limit);
      }
      return "tool calls would reach " + grouped(projected) + ", over the limit of " + grouped(limit);
    }
    return "tool calls would be at " + percent(projected / limit) + " of " + grouped(limit) + " (warn at 80%)";
  }

  function statusDecision(conv) {
    return checkBudget(conv, 0, 0).decision;
  }

  function record(conv, tokens) {
    conv.spent.tokens += tokens;
    conv.spent.toolCalls += 1;
  }

  function budgetLine(conv, decision) {
    return (
      "budget> " + decision +
      " | tokens " + conv.spent.tokens + "/" + settings.maxBudgetTokens +
      " | cost $" + conv.spent.costUsd.toFixed(4) + "/" + settings.maxBudgetCost.toFixed(4) +
      " | tools " + conv.spent.toolCalls + "/" + settings.maxToolCalls
    );
  }

  function formatTrace(trace) {
    if (!trace) return "trace> no completed turns yet";
    var lines = [
      "trace> turn=" + trace.turn,
      "trace> outcome=" + (trace.outcome || "-"),
    ];
    if (trace.error) lines.push("trace> error=" + trace.error);
    if (trace.gate) lines.push("trace> gate=" + trace.gate);
    lines.push("trace> handoffs");
    (trace.handoffs || []).forEach(function (item, index) {
      lines.push("  " + (index + 1) + ". " + item.stage);
      lines.push("  " + item.text);
    });
    if (!trace.handoffs || !trace.handoffs.length) lines.push("  none");
    lines.push("trace> claim lifecycle");
    (trace.claims || []).forEach(function (item, index) {
      lines.push("  " + (index + 1) + ". " + item);
    });
    if (!trace.claims || !trace.claims.length) lines.push("  none");
    lines.push("trace> local simulation only; the Python ledgers were not updated");
    return lines.join("\n");
  }

  function pushLocal(conv, content) {
    conv.messages.push({ kind: "local", content: content });
    conv.updatedAt = Date.now();
  }

  function estimateTokens(text) {
    var count = Array.from(text || "").length;
    if (!count) return 1;
    return Math.max(1, Math.floor(count / 4));
  }

  function slug(text) {
    return String(text || "")
      .toLowerCase()
      .replace(/[^a-z0-9._:-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 80) || "query";
  }

  function grouped(value) {
    return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function percent(ratio) {
    return Math.round(ratio * 100) + "%";
  }

  function cleanUrl(value) {
    var text = String(value || "").trim().replace(/\/+$/, "");
    if (!text) return "";
    if (!/^https?:\/\//i.test(text)) return "";
    return text;
  }

  function textOr(value, fallback) {
    var text = String(value || "").trim();
    return text || fallback;
  }

  function positiveInt(value, fallback) {
    var number = parseInt(value, 10);
    if (!isFinite(number) || number < 1) return fallback;
    return number;
  }

  function numberOr(value, fallback) {
    var number = Number(value);
    if (!isFinite(number)) return fallback;
    return number;
  }
})();
