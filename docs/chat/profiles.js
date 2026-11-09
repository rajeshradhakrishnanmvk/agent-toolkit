/* Profile merge and prompt composition. No persona text lives here. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ChatProfiles = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function isPlain(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function mergeConfig(base, extra) {
    var out = {};
    var key;
    var source = base || {};
    for (key in source) {
      if (Object.prototype.hasOwnProperty.call(source, key)) out[key] = source[key];
    }
    var over = extra || {};
    for (key in over) {
      if (!Object.prototype.hasOwnProperty.call(over, key) || over[key] == null) continue;
      if (isPlain(out[key]) && isPlain(over[key])) out[key] = mergeConfig(out[key], over[key]);
      else out[key] = over[key];
    }
    return out;
  }

  function resolveConfig(globalProfile, soulProfile, sessionProfile) {
    var globalConfig = (globalProfile && globalProfile.config) || {};
    var soulConfig = (soulProfile && soulProfile.config) || {};
    var sessionConfig = (sessionProfile && sessionProfile.config) || {};
    return mergeConfig(mergeConfig(globalConfig, soulConfig), sessionConfig);
  }

  function blocksFor(profile) {
    if (!profile) return [];
    var parts = [];
    if (profile.instructions && String(profile.instructions).trim()) {
      parts.push(String(profile.instructions).trim());
    }
    var config = profile.config || {};
    if (config.goals && String(config.goals).trim()) parts.push("Goals:\n" + String(config.goals).trim());
    if (config.notes && String(config.notes).trim()) parts.push("Project notes:\n" + String(config.notes).trim());
    return parts;
  }

  function composeInstructions(globalProfile, soulProfile, sessionProfile) {
    var parts = [];
    [globalProfile, soulProfile, sessionProfile].forEach(function (profile) {
      blocksFor(profile).forEach(function (block) { parts.push(block); });
    });
    return parts.join("\n\n");
  }

  function fill(template, vars) {
    return String(template || "").replace(/\{\{(\w+)\}\}/g, function (_match, key) {
      return vars[key] == null ? "" : String(vars[key]);
    });
  }

  function buildTurn(config, userText, turn) {
    var stages = (config && config.stages) || [];
    var rendered = {};
    var previous = "";
    var handoffs = [];
    var claims = [];
    var localTexts = [];
    var modelStage = null;
    var step = Math.max(1, turn || 1);
    for (var i = 0; i < stages.length; i += 1) {
      var stage = stages[i] || {};
      if (stage.model) {
        modelStage = stage;
        continue;
      }
      var tip = "";
      if (stage.tips && stage.tips.length) tip = stage.tips[(step - 1) % stage.tips.length];
      var vars = { user: userText, previous: previous, tip: tip };
      var name;
      for (name in rendered) {
        if (Object.prototype.hasOwnProperty.call(rendered, name)) vars[name] = rendered[name];
      }
      var text = fill(stage.template, vars);
      rendered[stage.id || ("stage-" + (i + 1))] = text;
      if (previous) {
        handoffs.push({
          stage: (stages[i - 1] && stages[i - 1].id ? stages[i - 1].id : "stage") + "->" + (stage.id || "stage"),
          text: previous,
        });
      }
      localTexts.push(text);
      var owner = stage.owner || stage.id || "stage";
      var stageId = stage.id || ("stage-" + (i + 1));
      claims.push("take " + stageId + " owner=" + owner);
      claims.push("done " + stageId + " owner=" + owner);
      previous = text;
    }
    var currentUser = userText;
    if (modelStage) {
      var modelVars = { user: userText, previous: previous, tip: "" };
      for (name in rendered) {
        if (Object.prototype.hasOwnProperty.call(rendered, name)) modelVars[name] = rendered[name];
      }
      currentUser = modelStage.template ? fill(modelStage.template, modelVars) : currentUser;
      claims.push("take " + (modelStage.id || "model") + " owner=" + (modelStage.owner || modelStage.id || "model"));
    } else if (localTexts.length) {
      currentUser = localTexts.join("\n\n") + "\n\nUser:\n" + userText;
    }
    return {
      currentUser: currentUser,
      handoffs: handoffs,
      claims: claims,
      localTexts: localTexts,
      modelStage: modelStage,
    };
  }

  function validateProfile(data) {
    if (!isPlain(data)) return "Profile must be a JSON object.";
    if (typeof data.id !== "string" || !data.id.trim()) return "id must be a non-empty string.";
    if (data.kind !== "global" && data.kind !== "soul" && data.kind !== "session") {
      return "kind must be global, soul, or session.";
    }
    if (data.name != null && typeof data.name !== "string") return "name must be text.";
    if (data.instructions != null && typeof data.instructions !== "string") return "instructions must be text.";
    if (data.greeting != null && typeof data.greeting !== "string") return "greeting must be text.";
    if (data.config != null && !isPlain(data.config)) return "config must be a JSON object.";
    if (data.kind === "session" && data.config && data.config.soulId != null && typeof data.config.soulId !== "string") {
      return "soulId must be a string.";
    }
    if (data.config && data.config.stages != null && !Array.isArray(data.config.stages)) {
      return "stages must be an array.";
    }
    return "";
  }

  function indexById(list) {
    var map = {};
    (list || []).forEach(function (item) {
      if (item && item.id) map[item.id] = item;
    });
    return map;
  }

  function overlay(defaults, records) {
    var next = {
      global: clone(defaults.global),
      souls: clone(defaults.souls || {}),
      sessions: clone(defaults.sessions || {}),
    };
    (records || []).forEach(function (record) {
      var profile = record && (record.profile || record);
      if (!profile || !profile.id || !profile.kind) return;
      if (profile.kind === "global") next.global = profile;
      if (profile.kind === "soul") next.souls[profile.id] = profile;
      if (profile.kind === "session") next.sessions[profile.id] = profile;
    });
    return next;
  }

  function loadDefaults(fetchImpl, base) {
    var fetchFn = fetchImpl;
    var root = base || "profiles/";
    if (root.slice(-1) !== "/") root += "/";
    function getJson(path) {
      return fetchFn(root + path).then(function (response) {
        if (!response.ok) throw new Error("Could not load " + path);
        return response.json();
      });
    }
    return getJson("global.json").then(function (global) {
      return getJson("manifest.json").then(function (manifest) {
        var souls = manifest.souls || [];
        var sessions = manifest.sessions || [];
        return Promise.all(souls.map(function (name) { return getJson("souls/" + name); })).then(function (soulList) {
          return Promise.all(sessions.map(function (name) { return getJson("sessions/" + name); })).then(function (sessionList) {
            return { global: global, souls: indexById(soulList), sessions: indexById(sessionList) };
          });
        });
      });
    });
  }

  function profileKey(profile) {
    return profile.kind + ":" + profile.id;
  }

  function loadStored(db) {
    return new Promise(function (resolve, reject) {
      if (!db.objectStoreNames.contains("profiles")) {
        resolve([]);
        return;
      }
      var request = db.transaction(["profiles"], "readonly").objectStore("profiles").getAll();
      request.onsuccess = function () { resolve(request.result || []); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function saveStored(db, profile) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(["profiles"], "readwrite");
      tx.objectStore("profiles").put({ key: profileKey(profile), profile: profile });
      tx.oncomplete = function () { resolve(); };
      tx.onerror = function () { reject(tx.error); };
    });
  }

  function deleteStored(db, profile) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(["profiles"], "readwrite");
      tx.objectStore("profiles").delete(profileKey(profile));
      tx.oncomplete = function () { resolve(); };
      tx.onerror = function () { reject(tx.error); };
    });
  }

  return {
    mergeConfig: mergeConfig,
    resolveConfig: resolveConfig,
    composeInstructions: composeInstructions,
    buildTurn: buildTurn,
    validateProfile: validateProfile,
    overlay: overlay,
    loadDefaults: loadDefaults,
    loadStored: loadStored,
    saveStored: saveStored,
    deleteStored: deleteStored,
    profileKey: profileKey,
  };
});
