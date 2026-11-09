(function () {
  "use strict";

  var profiles = { global: null, souls: {}, sessions: {}, defaults: null };
  var activeSessionId = "";
  var CONV_KEY = "agent-toolkit.chat.conversations.v1";
  var SETTINGS_KEY = "agent-toolkit.chat.settings.v1";
  var KEY_NAME = "agent-toolkit.chat.apiKey.v1";

  var state = freshState();
  var settings = loadSettings();
  var busy = false;
  var memoryDb = null;
  var ready = false;

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
  document.getElementById("memory-on").addEventListener("change", function (event) {
    active().memoryOn = !!event.target.checked;
    setMemoryStatus(active());
    saveState();
  });
  document.getElementById("memory-scope").addEventListener("change", function (event) {
    active().scope = event.target.value === "all" ? "all" : "conversation";
    setMemoryStatus(active());
    saveState();
  });
  document.getElementById("edit-summary").addEventListener("click", openSummary);
  document.getElementById("clear-summary").addEventListener("click", function () {
    var conv = active();
    conv.summary = "";
    conv.summaryThroughTurn = 0;
    conv.summaryStatus = "Summary cleared.";
    document.getElementById("summary-text").value = "";
    saveState();
    setMemoryStatus(conv);
  });
  document.getElementById("summary-form").addEventListener("submit", function () {
    var conv = active();
    conv.summary = document.getElementById("summary-text").value.trim();
    conv.summaryStatus = conv.summary ? "Summary saved in this browser." : "Summary cleared.";
    saveState();
    setMemoryStatus(conv);
  });
  document.getElementById("clear-memory").addEventListener("click", function () {
    clearMemory(active());
  });
  document.getElementById("clear-all-memory").addEventListener("click", function () {
    if (!window.confirm("Clear keyword memory and summaries for every chat in this browser? Transcripts stay.")) return;
    state.conversations.forEach(clearMemory);
    setMemoryStatus(active());
  });
  document.getElementById("export-memory").addEventListener("click", exportMemory);
  document.getElementById("import-memory").addEventListener("click", function () {
    document.getElementById("import-file").click();
  });
  document.getElementById("import-file").addEventListener("change", importMemory);
  document.getElementById("session-pick").addEventListener("change", function (event) {
    activeSessionId = event.target.value;
    localStorage.setItem("agent-toolkit.chat.activeSession", activeSessionId);
    var vis = visibleConversations();
    state.activeId = vis.length ? vis[0].id : "";
    render();
  });
  document.getElementById("new-session").addEventListener("click", function () {
    var name = window.prompt("Project name");
    if (!name || !name.trim()) return;
    var id = "s_" + Date.now().toString(36);
    var soul = activeSoul();
    var session = {
      id: id,
      kind: "session",
      name: name.trim().slice(0, 80),
      instructions: "",
      config: { soulId: soul ? soul.id : "", goals: "", notes: "" },
    };
    profiles.sessions[id] = session;
    activeSessionId = id;
    localStorage.setItem("agent-toolkit.chat.activeSession", id);
    if (memoryDb) ChatProfiles.saveStored(memoryDb, session);
    createConversation();
  });
  document.getElementById("open-profiles").addEventListener("click", openProfileEditor);
  document.getElementById("profile-pick").addEventListener("change", showProfileEditor);
  document.getElementById("profile-save").addEventListener("click", saveProfileEditor);
  document.getElementById("profile-reset").addEventListener("click", resetProfileEditor);
  document.getElementById("profile-export").addEventListener("click", exportProfiles);
  document.getElementById("profile-import").addEventListener("click", function () {
    document.getElementById("profile-file").click();
  });
  document.getElementById("profile-file").addEventListener("change", importProfiles);
  document.getElementById("view-prompt").addEventListener("click", viewComposedPrompt);
  document.getElementById("abort-flight").addEventListener("click", function () {
    var conv = active();
    if (!ready || busy) return;
    busy = true;
    drive(conv, { type: "abort" });
  });
  document.getElementById("retry-hold").addEventListener("click", function () {
    var conv = active();
    if (!ready || busy) return;
    busy = true;
    drive(conv, { type: "retry" });
  });

  boot();

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
      summary: "",
      summaryThroughTurn: 0,
      summaryStatus: "",
      memoryOn: null,
      memoryFloor: 0,
      scope: "",
      sessionId: "",
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
      pushLocal(active(), "This browser refused to store the conversation in local storage. IndexedDB may still have it.");
    }
    if (!memoryDb) return Promise.resolve();
    var writes = state.conversations.map(function (conv) {
      return ChatMemory.replaceConversation(memoryDb, conv);
    });
    return Promise.all(writes).catch(function () {
      setMemoryStatus(active(), "Could not write IndexedDB. The local storage copy is still here.");
    });
  }

  function readLegacyConversations() {
    try {
      var raw = localStorage.getItem(CONV_KEY);
      if (!raw) return [];
      var data = JSON.parse(raw);
      if (!data || !Array.isArray(data.conversations)) return [];
      return data.conversations;
    } catch (err) {
      return [];
    }
  }

  function mergeConversations(primary, secondary) {
    var map = {};
    function take(list) {
      (list || []).forEach(function (conv) {
        if (!conv || !conv.id) return;
        var existing = map[conv.id];
        if (!existing || (conv.updatedAt || 0) >= (existing.updatedAt || 0)) map[conv.id] = conv;
      });
    }
    take(primary);
    take(secondary);
    var merged = [];
    for (var id in map) {
      if (Object.prototype.hasOwnProperty.call(map, id)) merged.push(map[id]);
    }
    merged.sort(function (a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); });
    return merged;
  }

  function cfg() {
    return ChatProfiles.resolveConfig(profiles.global, activeSoul(), activeSession());
  }

  function systemInstructions() {
    return ChatProfiles.composeInstructions(profiles.global, activeSoul(), activeSession());
  }

  function activeSession() {
    return profiles.sessions[activeSessionId] || firstValue(profiles.sessions);
  }

  function activeSoul() {
    var session = activeSession();
    var soulId = session && session.config && session.config.soulId;
    return profiles.souls[soulId] || firstValue(profiles.souls);
  }

  function firstValue(map) {
    var key;
    for (key in map) {
      if (Object.prototype.hasOwnProperty.call(map, key)) return map[key];
    }
    return null;
  }

  function memoryEnabled(conv) {
    if (conv && typeof conv.memoryOn === "boolean") return conv.memoryOn;
    return !cfg().memory || cfg().memory.on !== false;
  }

  function memoryScope(conv) {
    if (conv && (conv.scope === "all" || conv.scope === "conversation")) return conv.scope;
    return cfg().memory && cfg().memory.scope === "all" ? "all" : "conversation";
  }

  function visibleConversations() {
    return state.conversations.filter(function (conv) {
      return conv.sessionId === activeSessionId;
    });
  }

  function migrateLegacySettings(globalProfile) {
    var stored = settings.legacy || {};
    if (!stored || stored.migrated) return globalProfile;
    var config = globalProfile.config || {};
    if (stored.baseUrl) config.baseUrl = stored.baseUrl;
    if (stored.model) config.model = stored.model;
    if (stored.maxTokens) config.maxReplyTokens = stored.maxTokens;
    if (stored.temperature != null) config.temperature = stored.temperature;
    if (stored.maxBudgetTokens) {
      config.budget = config.budget || {};
      config.budget.maxTokens = stored.maxBudgetTokens;
    }
    if (stored.maxToolCalls) {
      config.budget = config.budget || {};
      config.budget.maxToolCalls = stored.maxToolCalls;
    }
    if (stored.maxBudgetCost) {
      config.budget = config.budget || {};
      config.budget.maxCostUsd = stored.maxBudgetCost;
    }
    globalProfile.config = config;
    return globalProfile;
  }

  function boot() {
    var legacy = readLegacyConversations();
    ChatProfiles.loadDefaults(fetch, "profiles/").then(function (defaults) {
      profiles.defaults = defaults;
      profiles.global = defaults.global;
      profiles.souls = defaults.souls;
      profiles.sessions = defaults.sessions;
      return ChatMemory.openDb();
    }).then(function (db) {
      memoryDb = db;
      return Promise.all([ChatMemory.loadAll(db), ChatProfiles.loadStored(db)]);
    }).then(function (loaded) {
      var storedProfiles = loaded[1] || [];
      var overlay = ChatProfiles.overlay(profiles.defaults, storedProfiles.map(function (row) {
        return row.profile || row;
      }));
      overlay.global = migrateLegacySettings(overlay.global);
      profiles.global = overlay.global;
      profiles.souls = overlay.souls;
      profiles.sessions = overlay.sessions;
      if (!Object.keys(profiles.sessions).length) {
        profiles.sessions.example = {
          id: "example",
          kind: "session",
          name: "Example project",
          instructions: "",
          config: { soulId: firstValue(profiles.souls).id },
        };
      }
      activeSessionId = localStorage.getItem("agent-toolkit.chat.activeSession") || firstValue(profiles.sessions).id;
      if (!profiles.sessions[activeSessionId]) activeSessionId = firstValue(profiles.sessions).id;
      var legacySoul = (profiles.global.config && profiles.global.config.legacySoulId) || firstValue(profiles.souls).id;
      if (!profiles.sessions["imported-chats"]) {
        profiles.sessions["imported-chats"] = {
          id: "imported-chats",
          kind: "session",
          name: "Imported chats",
          instructions: "Chats saved before profiles were added.",
          config: { soulId: legacySoul },
        };
      }
      var merged = mergeConversations(loaded[0] || [], legacy);
      if (!merged.length) merged = [blankConversation()];
      var needsImport = merged.some(function (conv) {
        return !conv.sessionId && conv.messages && conv.messages.length;
      });
      var freshInstall = !legacy.length && !(loaded[0] || []).length;
      merged.forEach(function (conv) {
        if (conv.sessionId) return;
        conv.sessionId = freshInstall && !needsImport ? (profiles.sessions.example ? "example" : firstValue(profiles.sessions).id) : "imported-chats";
      });
      state = { activeId: merged[0].id, conversations: merged };
      ensureMemoryFields(state);
      if (needsImport) activeSessionId = "imported-chats";
      localStorage.setItem("agent-toolkit.chat.activeSession", activeSessionId);
      saveSettings();
      return saveState().then(function () {
        return ChatProfiles.saveStored(memoryDb, profiles.sessions["imported-chats"]);
      });
    }).catch(function (err) {
      memoryDb = null;
      state = legacy.length ? { activeId: legacy[0].id, conversations: legacy } : freshState();
      ensureMemoryFields(state);
      setMemoryStatus(active(), "Profiles or IndexedDB failed to load. " + (err && err.message ? err.message : ""));
    }).then(function () {
      ready = true;
      render();
    });
  }

  function ensureMemoryFields(data) {
    if (!data.conversations || !data.conversations.length) data.conversations = [blankConversation()];
    if (!findConv(data, data.activeId)) data.activeId = data.conversations[0].id;
    data.conversations.forEach(function (conv) {
      if (typeof conv.summary !== "string") conv.summary = "";
      if (!conv.summaryThroughTurn) conv.summaryThroughTurn = 0;
      if (!conv.summaryStatus) conv.summaryStatus = "";
      if (conv.memoryOn === undefined) conv.memoryOn = null;
      if (!conv.memoryFloor) conv.memoryFloor = 0;
      if (conv.scope !== "all" && conv.scope !== "conversation") conv.scope = "";
      if (!conv.sessionId) conv.sessionId = activeSessionId;
      (conv.messages || []).forEach(function (message, index) {
        stampMessage(message, (conv.createdAt || Date.now()) + index);
      });
    });
  }

  function stampMessage(message, createdAt) {
    if (!message.id) message.id = "m_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    if (!message.createdAt) message.createdAt = createdAt || Date.now();
    return message;
  }

  function loadSettings() {
    var stored = {};
    try {
      stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {};
    } catch (err) {
      stored = {};
    }
    return { rememberKey: stored.rememberKey !== false, legacy: stored };
  }

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ rememberKey: settings.rememberKey, migrated: true }));
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
    document.getElementById("api-key").value = apiKey();
    document.getElementById("remember-key").checked = settings.rememberKey;
    if (typeof dialogEl.showModal === "function") dialogEl.showModal();
    else dialogEl.setAttribute("open", "");
  }

  function readSettingsForm() {
    settings.rememberKey = document.getElementById("remember-key").checked;
    setApiKey(document.getElementById("api-key").value);
    saveSettings();
    render();
  }

  function createConversation() {
    var conv = blankConversation();
    conv.sessionId = activeSessionId;
    state.conversations.unshift(conv);
    state.activeId = conv.id;
    saveState();
    render();
    inputEl.focus();
  }

  function render() {
    if (profiles.global) {
      var vis = visibleConversations();
      if (!vis.length) {
        var created = blankConversation();
        created.sessionId = activeSessionId;
        state.conversations.unshift(created);
        state.activeId = created.id;
      } else if (!vis.some(function (item) { return item.id === state.activeId; })) {
        state.activeId = vis[0].id;
      }
    }
    if (profiles.global) state.conversations.forEach(ensureFlight);
    var conv = active();
    titleEl.textContent = conv.title;
    var soul = profiles.global ? activeSoul() : null;
    var session = profiles.global ? activeSession() : null;
    var label = document.getElementById("active-profile");
    if (label) {
      label.textContent = session
        ? "Session: " + (session.name || session.id) + " · Soul: " + (soul ? soul.name || soul.id : "none")
        : "Profiles not loaded";
    }
    fillSessionSelect();
    budgetEl.textContent = budgetLine(conv, statusDecision(conv));
    document.getElementById("memory-on").checked = memoryEnabled(conv);
    document.getElementById("memory-scope").value = memoryScope(conv);
    setMemoryStatus(conv);
    renderTower(conv);
    listEl.textContent = "";
    visibleConversations().forEach(function (item) {
      var row = document.createElement("li");
      row.className = "conv-row" + (item.id === conv.id ? " active" : "");
      var pick = document.createElement("button");
      pick.type = "button";
      pick.className = "pick";
      var flightNo = item.flight && item.flight.number ? item.flight.number + " · " : "";
      var dest = (profiles.sessions[item.sessionId] && profiles.sessions[item.sessionId].config && profiles.sessions[item.sessionId].config.destination) || "";
      pick.textContent = flightNo + (dest || item.title);
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
      var pill = document.createElement("span");
      pill.className = "status-pill";
      pill.textContent = (item.flight && item.flight.status) || "Scheduled";
      row.appendChild(pick);
      row.appendChild(pill);
      row.appendChild(rename);
      row.appendChild(remove);
      listEl.appendChild(row);
    });
    threadEl.textContent = "";
    if (!conv.messages.length && soul && soul.greeting) {
      var greet = document.createElement("p");
      greet.className = "greeting";
      greet.textContent = soul.greeting;
      threadEl.appendChild(greet);
    }
    conv.messages.forEach(function (message) {
      var bubble = document.createElement("article");
      bubble.className = "bubble " + message.kind;
      if (message.role) {
        var who = document.createElement("p");
        who.className = "role";
        who.textContent = message.role;
        bubble.appendChild(who);
      }
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
      if (message.kind === "assistant" && message.memory) {
        var recall = document.createElement("details");
        recall.className = "recall";
        var summary = document.createElement("summary");
        summary.textContent = "What was recalled";
        recall.appendChild(summary);
        var note = document.createElement("p");
        note.textContent = message.memory.note;
        recall.appendChild(note);
        (message.memory.recalled || []).forEach(function (item) {
          var line = document.createElement("p");
          line.textContent = item.content;
          recall.appendChild(line);
        });
        bubble.appendChild(recall);
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
    if (memoryDb) ChatMemory.deleteConversation(memoryDb, id);
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
    if (!ready || busy) return;
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
    conv.messages.push(stampMessage({ kind: "user", content: text }));
    conv.updatedAt = Date.now();
    saveState();
    render();
    runTurn(conv, text);
  }

  function handleCommand(conv, text) {
    var cmd = text.trim().toLowerCase();
    conv.messages.push(stampMessage({ kind: "user", content: text }));
    if (cmd === "/help") {
      pushLocal(
        conv,
        "/help show commands\n/model show the active model\n/status show the local budget\n/trace show the latest simulated trace\n/trace on|off print the trace after each answer\n/quit does not close this page\nMemory is the checkbox above. It is a keyword match stored in this browser."
      );
      return;
    }
    if (cmd === "/model") {
      pushLocal(conv, "active model: " + cfg().model);
      return;
    }
    if (cmd === "/status") {
      pushLocal(conv, budgetLine(conv, statusDecision(conv)));
      return;
    }
    if (cmd === "/trace") {
      conv.messages.push(stampMessage({
        kind: "local",
        content: "Latest trace",
        trace: formatTrace(conv.lastTrace),
        forceTrace: true,
      }));
      return;
    }
    if (cmd === "/trace on" || cmd === "/trace off") {
      conv.autoTrace = cmd === "/trace on";
      conv.messages.push(stampMessage({
        kind: "local",
        content: "trace> auto trace " + (conv.autoTrace ? "enabled" : "disabled"),
        trace: formatTrace(conv.lastTrace),
        forceTrace: true,
      }));
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

  function ensureFlight(conv) {
    var plan = planFromSession(conv);
    var checklist = (cfg().airport && cfg().airport.checklist) || [];
    if (!conv.flight) {
      var departed = (conv.messages || []).some(function (message) { return message.kind === "assistant"; });
      conv.flight = Airport.createFlight(plan, checklist);
      if (departed) {
        conv.flight.status = "Departed";
        conv.flight.phase = "inflight";
        conv.flight.departed = true;
      }
    }
    conv.flight.id = conv.id;
    if (!conv.flight.number) conv.flight.number = plan.flight;
    if (!conv.flight.origin) conv.flight.origin = plan.origin;
    if (!conv.flight.destination) conv.flight.destination = plan.destination;
    if (!conv.flight.cargo) conv.flight.cargo = plan.cargo;
    if (!conv.flight.weather) conv.flight.weather = plan.weather;
    if (!conv.flight.checklist || !conv.flight.checklist.length) {
      conv.flight.checklist = Airport.createFlight(plan, checklist).checklist;
    }
    return conv.flight;
  }

  function planFromSession(conv) {
    var session = profiles.sessions[conv.sessionId] || activeSession() || {};
    var config = session.config || {};
    var airport = cfg().airport || {};
    return {
      flight: config.flight || session.name || "TK-100",
      origin: config.origin || airport.code || "",
      destination: config.destination || config.goals || "",
      cargo: config.cargo || config.notes || "",
      weather: config.weather || "",
    };
  }

  function crewSoul(role) {
    var crew = (activeSession() && activeSession().config && activeSession().config.crew) || {};
    var id = crew[role] || role;
    return profiles.souls[id] || { id: role, kind: "soul", name: role, instructions: "" };
  }

  function boardFlights(selfId) {
    return state.conversations.map(function (conv) {
      if (!conv.flight) return null;
      var copy = JSON.parse(JSON.stringify(conv.flight));
      copy.id = conv.id;
      return copy;
    }).filter(function (flight) { return flight && flight.id !== selfId; });
  }

  function airportContext(conv) {
    var ask = cfg().askHuman || {};
    var gate = checkBudget(conv, estimateTokens((conv.flight && conv.flight.request) || "") + 220, 1);
    return {
      id: conv.id,
      now: Date.now(),
      runway: (cfg().airport && cfg().airport.runway) || "18",
      askTemplate: ask.template,
      radio: (cfg().airport && cfg().airport.radio) || "",
      budget: {
        ok: gate.decision !== "stop-and-ask-human" || ask.enabled === false,
        reason: gate.reason,
      },
      flights: boardFlights(conv.id),
    };
  }

  function speak(conv, lines) {
    (lines || []).forEach(function (line) {
      conv.messages.push(stampMessage({
        kind: line.kind || "assistant",
        role: line.role,
        content: line.text,
        meta: conv.flight ? conv.flight.status : "",
      }));
    });
  }

  function finishTurn(conv) {
    busy = false;
    conv.updatedAt = Date.now();
    if (conv.flight && conv.flight.departed) {
      var last = conv.flight.log && conv.flight.log[conv.flight.log.length - 1];
      if (last && last.role === "cabin" && last.text) conv.summary = last.text;
    }
    saveState();
    render();
  }

  function drive(conv, event) {
    ensureFlight(conv);
    var step = Airport.advance(conv.flight, event, airportContext(conv));
    conv.flight = step.flight;
    speak(conv, step.say);
    if (!step.model) {
      finishTurn(conv);
      return;
    }
    if (!conv.flight.departed && conv.flight.calls >= 4) {
      conv.messages.push(stampMessage({
        kind: "error",
        role: "atc",
        content: "HOLD " + conv.flight.number + ". Takeoff already used 4 model calls.",
      }));
      finishTurn(conv);
      return;
    }
    var system = ChatProfiles.composeInstructions(profiles.global, crewSoul(step.model.role), activeSession());
    if (step.model.systemExtra) system += "\n\n" + step.model.systemExtra;
    var packed = packPrompt(conv, conv.flight.request || "", system, step.model.user);
    render();
    complete(packed.messages).then(function (reply) {
      record(conv, estimateTokens(reply));
      drive(conv, { type: "model", role: step.model.role, text: reply });
    }).catch(function (err) {
      if (err && err.status === 429) {
        conv.flight.status = "Holding";
        conv.flight.hold = { kind: "traffic", reason: "rate limit" };
        conv.messages.push(stampMessage({
          kind: "assistant",
          role: "atc",
          content: "HOLD " + conv.flight.number + ". Traffic delay. The frequency is busy. Use Retry.",
        }));
      } else {
        conv.messages.push(stampMessage({
          kind: "error",
          role: step.model.role,
          content: explainError(err),
          forceTrace: true,
        }));
      }
      finishTurn(conv);
    });
  }

  function runTurn(conv, userText) {
    conv.turn += 1;
    busy = true;
    render();
    drive(conv, { type: "user", text: userText });
  }

  function renderTower(conv) {
    var runway = document.getElementById("runway");
    var phases = document.getElementById("phase-list");
    var checks = document.getElementById("check-list");
    var log = document.getElementById("atc-log");
    var retry = document.getElementById("retry-hold");
    if (!runway || !conv) return;
    var flight = conv.flight;
    var phase = flight && flight.phase ? flight.phase : "preflight";
    runway.setAttribute("data-phase", phase);
    phases.textContent = "";
    Airport.PHASES.forEach(function (name) {
      var item = document.createElement("li");
      item.textContent = name;
      if (name === phase) item.className = "phase-now";
      phases.appendChild(item);
    });
    checks.textContent = "";
    ((flight && flight.checklist) || []).forEach(function (item) {
      var row = document.createElement("li");
      row.textContent = (item.done ? "✓ " : "○ ") + item.label;
      if (item.done) row.className = "check-done";
      checks.appendChild(row);
    });
    log.textContent = "";
    ((flight && flight.log) || []).slice(-8).forEach(function (entry) {
      var row = document.createElement("li");
      var when = new Date(entry.t || Date.now());
      var stamp = isNaN(when.getTime()) ? "" : when.toISOString().slice(11, 19) + " ";
      row.textContent = stamp + entry.role + ": " + entry.text;
      log.appendChild(row);
    });
    if (retry) retry.hidden = !(flight && flight.hold && (flight.hold.kind === "traffic" || flight.hold.kind === "runway"));
  }

  function complete(messages, maxTokens) {
    var key = apiKey();
    if (!key) {
      return Promise.reject(makeError(0, "Missing API key. Open Settings and enter one. It stays in this browser."));
    }
    var url = String(cfg().baseUrl || "").replace(/\/+$/, "") + "/chat/completions";
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = controller ? window.setTimeout(function () { controller.abort(); }, 60000) : 0;
    return fetch(url, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + key,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: cfg().model,
        messages: messages,
        max_tokens: maxTokens || cfg().maxReplyTokens || 256,
        temperature: cfg().temperature,
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
    var caps = (cfg().budget) || {};
    var tokenState = dimensionState(conv.spent.tokens, addTokens, caps.maxTokens || 1);
    var toolState = dimensionState(conv.spent.toolCalls, addTools, caps.maxToolCalls || 1);
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
    var limit = (cfg().budget && cfg().budget.maxTokens) || 1;
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
    var limit = (cfg().budget && cfg().budget.maxToolCalls) || 1;
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
      " | tokens " + conv.spent.tokens + "/" + ((cfg().budget && cfg().budget.maxTokens) || 0) +
      " | cost $" + conv.spent.costUsd.toFixed(4) + "/" + Number((cfg().budget && cfg().budget.maxCostUsd) || 0).toFixed(4) +
      " | tools " + conv.spent.toolCalls + "/" + ((cfg().budget && cfg().budget.maxToolCalls) || 0)
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
    conv.messages.push(stampMessage({ kind: "local", content: content }));
    conv.updatedAt = Date.now();
  }

  function eligibleMessages(conv) {
    return (conv.messages || []).filter(function (message) {
      if (message.kind !== "user" && message.kind !== "assistant") return false;
      if ((message.createdAt || 0) < (conv.memoryFloor || 0)) return false;
      return !!message.content;
    });
  }

  function packPrompt(conv, userText, system, currentUser) {
    var history = eligibleMessages(conv);
    var prior = history.slice(0, -1);
    var current = history.length ? history[history.length - 1] : null;
    var corpus = [];
    if (memoryEnabled(conv)) {
      var pool = memoryScope(conv) === "all" ? state.conversations : [conv];
      pool.forEach(function (item) {
        eligibleMessages(item).forEach(function (message) {
          if (current && message.id === current.id) return;
          corpus.push({
            id: message.id,
            convId: item.id,
            content: message.content,
          });
        });
      });
    }
    var packed = ChatMemory.assemblePrompt({
      system: system,
      summary: memoryEnabled(conv) ? conv.summary : "",
      query: userText,
      corpus: corpus,
      recent: prior.map(function (message) {
        return { id: message.id, role: message.kind, content: message.content };
      }),
      currentUser: currentUser,
      memoryOn: memoryEnabled(conv),
      replyTokens: Math.max(256, cfg().maxReplyTokens || 256),
      safetyTokens: cfg().safetyTokens == null ? 64 : cfg().safetyTokens,
      contextTokens: cfg().contextTokens || 2048,
      memoryTokens: (cfg().memory && cfg().memory.recallTokens) || 500,
      topK: (cfg().memory && cfg().memory.topK) || 4,
      synonyms: cfg().memory && cfg().memory.synonyms,
    });
    var note = !memoryEnabled(conv)
      ? "Memory is off. This reply did not include older turns."
      : (packed.recalled.length
        ? "Keyword matches from this browser. Not a semantic search."
        : "No earlier turn matched these words.");
    if (packed.summaryUsed) note += " A stored summary was included.";
    if (packed.recentCount) note += " Recent turns included: " + packed.recentCount + ".";
    return {
      messages: packed.messages,
      panel: { note: note, recalled: packed.recalled },
    };
  }

  function maybeSummarize(conv) {
    if (!memoryEnabled(conv)) return Promise.resolve();
    var memory = cfg().memory || {};
    var every = memory.summaryEveryTurns || 4;
    var overflowAt = memory.summaryOverflowTokens || 1200;
    var historyTokens = 0;
    eligibleMessages(conv).forEach(function (message) {
      historyTokens += ChatMemory.estimateTokens(message.content);
    });
    var due = conv.turn > 0 && conv.turn % every === 0;
    var overflow = historyTokens > overflowAt;
    if ((!due && !overflow) || conv.summaryThroughTurn >= conv.turn) return Promise.resolve();
    var recent = eligibleMessages(conv).slice(-8).map(function (message) {
      return message.kind + ": " + message.content;
    }).join("\n");
    var template = memory.summaryInstruction || "Summarize the conversation so far in under 120 words.\n\nPrevious summary:\n{summary}\n\nNew turns:\n{turns}";
    var prompt = ChatMemory.fit(
      template.replace("{summary}", conv.summary || "(none)").replace("{turns}", recent),
      700
    );
    conv.summaryStatus = "Updating summary…";
    setMemoryStatus(conv);
    return complete([
      { role: "system", content: "You write short neutral summaries. Do not give advice." },
      { role: "user", content: prompt },
    ], 180).then(function (summary) {
      conv.summary = summary;
      conv.summaryThroughTurn = conv.turn;
      conv.summaryStatus = "Summary updated in this browser.";
    }).catch(function (err) {
      if (err && err.status === 429) {
        conv.summaryStatus = "Summary skipped: rate limit (429). The reply was kept.";
      } else {
        conv.summaryStatus = "Summary skipped. The reply was kept.";
      }
    });
  }

  function clearMemory(conv) {
    conv.summary = "";
    conv.summaryThroughTurn = 0;
    conv.summaryStatus = "Memory cleared for this chat. The transcript is still visible.";
    conv.memoryFloor = Date.now();
    saveState();
    setMemoryStatus(conv);
    render();
  }

  function setMemoryStatus(conv, override) {
    var el = document.getElementById("memory-status");
    if (!el) return;
    if (override) {
      el.textContent = override;
      return;
    }
    var bits = [
      memoryEnabled(conv) ? "Memory on. Keyword recall stays on this device." : "Memory off. Only the current step is sent.",
      memoryScope(conv) === "all" ? "Scope: all chats." : "Scope: this chat.",
    ];
    if (conv.summaryStatus) bits.push(conv.summaryStatus);
    el.textContent = bits.join(" ");
  }

  function openSummary() {
    var conv = active();
    document.getElementById("summary-text").value = conv.summary || "";
    var dialog = document.getElementById("summary-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function exportMemory() {
    var payload = {
      version: 1,
      kind: "agent-toolkit-chat-memory",
      exportedAt: new Date().toISOString(),
      conversations: state.conversations,
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "agent-toolkit-chat-memory.json";
    link.click();
    URL.revokeObjectURL(url);
    setMemoryStatus(active(), "Exported memory JSON. It does not include the API key.");
  }

  function importMemory(event) {
    var file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(String(reader.result || ""));
        var list = data && data.conversations;
        if (!Array.isArray(list)) throw new Error("missing conversations");
        list.forEach(function (conv) {
          if (!conv || !conv.id) return;
          conv.messages = Array.isArray(conv.messages) ? conv.messages : [];
          ensureMemoryFields({ activeId: conv.id, conversations: [conv] });
          var existing = findConv(state, conv.id);
          if (existing) {
            var index = state.conversations.indexOf(existing);
            state.conversations[index] = conv;
          } else {
            state.conversations.unshift(conv);
          }
        });
        if (!findConv(state, state.activeId)) state.activeId = state.conversations[0].id;
        saveState();
        render();
        setMemoryStatus(active(), "Imported " + list.length + " conversation" + (list.length === 1 ? "" : "s") + " into this browser.");
      } catch (err) {
        setMemoryStatus(active(), "Import failed. The file was not a chat memory export.");
      }
    };
    reader.readAsText(file);
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

  function fillSessionSelect() {
    var select = document.getElementById("session-pick");
    if (!select || !profiles.global) return;
    var current = select.value;
    select.textContent = "";
    Object.keys(profiles.sessions).sort().forEach(function (id) {
      var session = profiles.sessions[id];
      var option = document.createElement("option");
      option.value = id;
      option.textContent = session.name || id;
      select.appendChild(option);
    });
    if (profiles.sessions[activeSessionId]) select.value = activeSessionId;
    else if (current && profiles.sessions[current]) select.value = current;
  }

  function profileChoices() {
    var list = [{ key: "global", profile: profiles.global }];
    Object.keys(profiles.souls).sort().forEach(function (id) {
      list.push({ key: "soul:" + id, profile: profiles.souls[id] });
    });
    Object.keys(profiles.sessions).sort().forEach(function (id) {
      list.push({ key: "session:" + id, profile: profiles.sessions[id] });
    });
    return list.filter(function (item) { return item.profile; });
  }

  function openProfileEditor() {
    var select = document.getElementById("profile-pick");
    select.textContent = "";
    profileChoices().forEach(function (item) {
      var option = document.createElement("option");
      option.value = item.key;
      option.textContent = item.profile.kind + ": " + (item.profile.name || item.profile.id);
      select.appendChild(option);
    });
    var soul = activeSoul();
    var session = activeSession();
    if (session) select.value = "session:" + session.id;
    else if (soul) select.value = "soul:" + soul.id;
    showProfileEditor();
    var dialog = document.getElementById("profile-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function selectedProfile() {
    var key = document.getElementById("profile-pick").value;
    if (key === "global") return profiles.global;
    if (key.indexOf("soul:") === 0) return profiles.souls[key.slice(5)];
    if (key.indexOf("session:") === 0) return profiles.sessions[key.slice(8)];
    return null;
  }

  function showProfileEditor() {
    var profile = selectedProfile();
    document.getElementById("profile-error").textContent = "";
    if (!profile) return;
    document.getElementById("profile-instructions").value = profile.instructions || "";
    document.getElementById("profile-config").value = JSON.stringify(profile.config || {}, null, 2);
  }

  function saveProfileEditor() {
    var profile = selectedProfile();
    var error = document.getElementById("profile-error");
    if (!profile) return;
    var config;
    try {
      config = JSON.parse(document.getElementById("profile-config").value);
    } catch (err) {
      error.textContent = "Config is not valid JSON. " + err.message;
      return;
    }
    var next = {
      id: profile.id,
      kind: profile.kind,
      name: profile.name,
      greeting: profile.greeting,
      instructions: document.getElementById("profile-instructions").value,
      config: config,
    };
    var problem = ChatProfiles.validateProfile(next);
    if (problem) {
      error.textContent = problem;
      return;
    }
    if (next.kind === "global") profiles.global = next;
    if (next.kind === "soul") profiles.souls[next.id] = next;
    if (next.kind === "session") profiles.sessions[next.id] = next;
    error.textContent = "";
    if (memoryDb) ChatProfiles.saveStored(memoryDb, next);
    render();
  }

  function resetProfileEditor() {
    var profile = selectedProfile();
    if (!profile || !profiles.defaults) return;
    var fresh = null;
    if (profile.kind === "global") fresh = profiles.defaults.global;
    if (profile.kind === "soul") fresh = profiles.defaults.souls[profile.id];
    if (profile.kind === "session") fresh = profiles.defaults.sessions[profile.id];
    if (!fresh) {
      document.getElementById("profile-error").textContent = "This profile has no shipped default.";
      return;
    }
    var copy = JSON.parse(JSON.stringify(fresh));
    if (copy.kind === "global") profiles.global = copy;
    if (copy.kind === "soul") profiles.souls[copy.id] = copy;
    if (copy.kind === "session") profiles.sessions[copy.id] = copy;
    if (memoryDb) ChatProfiles.deleteStored(memoryDb, copy);
    showProfileEditor();
    render();
  }

  function exportProfiles() {
    var payload = {
      version: 1,
      kind: "agent-toolkit-profiles",
      exportedAt: new Date().toISOString(),
      profiles: profileChoices().map(function (item) { return item.profile; }),
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "agent-toolkit-profiles.json";
    link.click();
    URL.revokeObjectURL(url);
  }

  function importProfiles(event) {
    var file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(String(reader.result || ""));
        var list = data && data.profiles;
        if (!Array.isArray(list)) throw new Error("missing profiles");
        list.forEach(function (profile) {
          var problem = ChatProfiles.validateProfile(profile);
          if (problem) throw new Error(problem);
          if (profile.kind === "global") profiles.global = profile;
          if (profile.kind === "soul") profiles.souls[profile.id] = profile;
          if (profile.kind === "session") profiles.sessions[profile.id] = profile;
          if (memoryDb) ChatProfiles.saveStored(memoryDb, profile);
        });
        render();
        setMemoryStatus(active(), "Imported " + list.length + " profile" + (list.length === 1 ? "" : "s") + ".");
      } catch (err) {
        setMemoryStatus(active(), "Profile import failed. " + (err.message || ""));
      }
    };
    reader.readAsText(file);
  }

  function viewComposedPrompt() {
    document.getElementById("composed-text").value = systemInstructions();
    var dialog = document.getElementById("prompt-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }
})();
