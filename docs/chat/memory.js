/* Per-browser chat memory: tokenizer, BM25, and prompt packing.
   Edit SYNONYMS to add keyword aliases. This is not a semantic search. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ChatMemory = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var STOPWORDS = {
    a: 1, an: 1, the: 1, and: 1, or: 1, of: 1, to: 1, in: 1, on: 1, for: 1,
    is: 1, it: 1, i: 1, you: 1, we: 1, my: 1, me: 1, with: 1, that: 1, this: 1,
    be: 1, as: 1, at: 1, by: 1, from: 1, are: 1, was: 1, were: 1, do: 1, did: 1,
    have: 1, has: 1, not: 1, but: 1, if: 1, so: 1, about: 1, your: 1, our: 1,
  };

  /* Canonical form for a few surface words. Add pairs as plain strings. */
  var SYNONYMS = {
    kid: "child",
    kids: "child",
    children: "child",
    child: "child",
    retire: "retire",
    retiring: "retire",
    retirement: "retire",
    dollar: "dollar",
    dollars: "dollar",
    usd: "dollar",
    rupee: "inr",
    rupees: "inr",
    inr: "inr",
  };

  var DB_NAME = "agent-toolkit-chat";
  var DB_VERSION = 1;

  function estimateTokens(text) {
    var count = Array.from(String(text || "")).length;
    if (!count) return 0;
    return Math.ceil(count / 4);
  }

  function stem(token) {
    var word = SYNONYMS[token] || token;
    if (word.length > 5 && word.slice(-3) === "ing") word = word.slice(0, -3);
    else if (word.length > 4 && word.slice(-2) === "ed") word = word.slice(0, -2);
    else if (word.length > 4 && word.slice(-2) === "es") word = word.slice(0, -2);
    else if (word.length > 3 && word.slice(-1) === "s") word = word.slice(0, -1);
    if (SYNONYMS[word]) word = SYNONYMS[word];
    return word;
  }

  function tokenize(text) {
    var raw = String(text || "").toLowerCase().split(/[^a-z0-9]+/);
    var out = [];
    for (var i = 0; i < raw.length; i += 1) {
      if (!raw[i] || STOPWORDS[raw[i]]) continue;
      var token = stem(raw[i]);
      if (!token || STOPWORDS[token]) continue;
      out.push(token);
    }
    return out;
  }

  function termFreq(tokens) {
    var tf = {};
    for (var i = 0; i < tokens.length; i += 1) tf[tokens[i]] = (tf[tokens[i]] || 0) + 1;
    return tf;
  }

  function rank(query, docs) {
    var qTokens = tokenize(query);
    var prepared = [];
    var df = {};
    var lengthSum = 0;
    for (var i = 0; i < docs.length; i += 1) {
      var tokens = tokenize(docs[i].content || "");
      var tf = termFreq(tokens);
      prepared.push({ doc: docs[i], tf: tf, len: tokens.length });
      lengthSum += tokens.length;
      var seen = {};
      for (var term in tf) {
        if (!Object.prototype.hasOwnProperty.call(tf, term) || seen[term]) continue;
        seen[term] = 1;
        df[term] = (df[term] || 0) + 1;
      }
    }
    var n = prepared.length || 1;
    var avg = prepared.length ? lengthSum / prepared.length : 0;
    var k1 = 1.2;
    var b = 0.75;
    var scored = [];
    for (var d = 0; d < prepared.length; d += 1) {
      var row = prepared[d];
      var score = 0;
      var unique = {};
      for (var q = 0; q < qTokens.length; q += 1) unique[qTokens[q]] = 1;
      for (var term in unique) {
        if (!Object.prototype.hasOwnProperty.call(unique, term)) continue;
        var freq = row.tf[term] || 0;
        if (!freq) continue;
        var docFreq = df[term] || 0;
        var idf = Math.log(1 + (n - docFreq + 0.5) / (docFreq + 0.5));
        var norm = freq * (k1 + 1) / (freq + k1 * (1 - b + b * (avg ? row.len / avg : 0)));
        score += idf * norm;
      }
      scored.push({
        id: row.doc.id,
        convId: row.doc.convId,
        content: row.doc.content,
        score: score,
      });
    }
    scored.sort(function (left, right) {
      if (right.score !== left.score) return right.score - left.score;
      return String(left.id).localeCompare(String(right.id));
    });
    return scored;
  }

  function fit(text, tokenBudget) {
    var raw = String(text || "");
    if (tokenBudget <= 0) return "";
    if (estimateTokens(raw) <= tokenBudget) return raw;
    var slice = Array.from(raw).slice(0, Math.max(0, tokenBudget * 4));
    var clipped = slice.join("").replace(/\s+\S*$/, "").trim();
    return clipped ? clipped + "…" : "";
  }

  function messageTokens(messages) {
    var total = 0;
    for (var i = 0; i < messages.length; i += 1) total += estimateTokens(messages[i].content);
    return total;
  }

  function assemblePrompt(options) {
    var opts = options || {};
    var reply = opts.replyTokens || 256;
    var safety = opts.safetyTokens == null ? 64 : opts.safetyTokens;
    var context = opts.contextTokens || 2048;
    var memoryCap = opts.memoryTokens == null ? 500 : opts.memoryTokens;
    var topK = opts.topK || 4;
    var budget = Math.max(48, context - reply - safety);
    var memoryOn = !!opts.memoryOn;
    var system = String(opts.system || "");
    var current = String(opts.currentUser || "");

    function pack(summaryText, recalledRows, recentRows) {
      var block = "";
      if (summaryText) block += "Conversation summary:\n" + summaryText + "\n\n";
      if (recalledRows.length) {
        block += "Recalled notes (keyword match, not a semantic search):\n";
        for (var i = 0; i < recalledRows.length; i += 1) {
          block += (i + 1) + ". " + recalledRows[i].content + "\n";
        }
      }
      var systemOut = system;
      if (block.trim()) systemOut = system + "\n\n" + block.trim();
      var messages = [{ role: "system", content: fit(systemOut, budget) }];
      for (var r = 0; r < recentRows.length; r += 1) {
        messages.push({
          role: recentRows[r].role === "assistant" ? "assistant" : "user",
          content: recentRows[r].content,
        });
      }
      messages.push({ role: "user", content: current });
      return messages;
    }

    if (!memoryOn) {
      var sysOnly = fit(system, Math.max(24, budget - 24));
      var curOnly = fit(current, Math.max(1, budget - estimateTokens(sysOnly)));
      var plain = [
        { role: "system", content: sysOnly },
        { role: "user", content: curOnly },
      ];
      return {
        messages: plain,
        recalled: [],
        summaryUsed: "",
        recentCount: 0,
        promptTokens: messageTokens(plain),
        budget: budget,
      };
    }

    var recent = opts.recent || [];
    var recentPacked = [];
    var recentIds = {};
    var recentRoom = Math.max(0, budget - Math.min(memoryCap, Math.floor(budget / 2)) - Math.min(estimateTokens(system), Math.floor(budget * 0.35)) - Math.min(estimateTokens(current), 120));
    for (var i = recent.length - 1; i >= 0; i -= 1) {
      var msg = recent[i];
      var cost = estimateTokens(msg.content);
      if (!cost) continue;
      if (cost > recentRoom) {
        var clipped = fit(msg.content, recentRoom);
        if (!estimateTokens(clipped)) break;
        recentPacked.unshift({ id: msg.id, role: msg.role, content: clipped });
        recentIds[msg.id] = 1;
        break;
      }
      recentPacked.unshift({ id: msg.id, role: msg.role, content: msg.content });
      recentIds[msg.id] = 1;
      recentRoom -= cost;
    }

    var corpus = [];
    var source = opts.corpus || [];
    for (var c = 0; c < source.length; c += 1) {
      if (!source[c] || recentIds[source[c].id]) continue;
      corpus.push(source[c]);
    }
    var ranked = rank(opts.query || "", corpus);
    var recalled = [];
    for (var k = 0; k < ranked.length && recalled.length < topK; k += 1) {
      if (ranked[k].score <= 0) continue;
      recalled.push(ranked[k]);
    }

    var summary = String(opts.summary || "");
    var messages = pack(summary, recalled, recentPacked);
    var guard = 0;
    while (messageTokens(messages) > budget && guard < 12) {
      guard += 1;
      if (recalled.length) {
        recalled.pop();
      } else if (recentPacked.length) {
        recentPacked.shift();
      } else if (summary) {
        summary = fit(summary, Math.max(0, estimateTokens(summary) - 20));
      } else {
        break;
      }
      messages = pack(summary, recalled, recentPacked);
    }
    if (messageTokens(messages) > budget) {
      var last = messages[messages.length - 1];
      var others = messageTokens(messages.slice(0, -1));
      last.content = fit(last.content, Math.max(1, budget - others));
    }
    return {
      messages: messages,
      recalled: recalled,
      summaryUsed: summary,
      recentCount: recentPacked.length,
      promptTokens: messageTokens(messages),
      budget: budget,
    };
  }

  function openDb() {
    return new Promise(function (resolve, reject) {
      if (typeof indexedDB === "undefined") {
        reject(new Error("IndexedDB is not available"));
        return;
      }
      var request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains("conversations")) {
          db.createObjectStore("conversations", { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains("messages")) {
          var messages = db.createObjectStore("messages", { keyPath: "id" });
          messages.createIndex("convId", "convId", { unique: false });
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function transaction(db, mode) {
    return db.transaction(["conversations", "messages"], mode);
  }

  function reqToPromise(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function loadAll(db) {
    var tx = transaction(db, "readonly");
    return Promise.all([
      reqToPromise(tx.objectStore("conversations").getAll()),
      reqToPromise(tx.objectStore("messages").getAll()),
    ]).then(function (rows) {
      var conversations = rows[0] || [];
      var messages = rows[1] || [];
      var byConv = {};
      messages.sort(function (a, b) { return (a.createdAt || 0) - (b.createdAt || 0); });
      for (var i = 0; i < messages.length; i += 1) {
        var convId = messages[i].convId;
        if (!byConv[convId]) byConv[convId] = [];
        byConv[convId].push(messages[i]);
      }
      conversations.sort(function (a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); });
      for (var c = 0; c < conversations.length; c += 1) {
        conversations[c].messages = byConv[conversations[c].id] || [];
      }
      return conversations;
    });
  }

  function replaceConversation(db, conv) {
    var tx = transaction(db, "readwrite");
    var conversations = tx.objectStore("conversations");
    var messages = tx.objectStore("messages");
    var record = {};
    var key;
    for (key in conv) {
      if (Object.prototype.hasOwnProperty.call(conv, key) && key !== "messages") record[key] = conv[key];
    }
    conversations.put(record);
    var index = messages.index("convId");
    var existing = index.getAllKeys(conv.id);
    return reqToPromise(existing).then(function (keys) {
      for (var i = 0; i < (keys || []).length; i += 1) messages.delete(keys[i]);
      var list = conv.messages || [];
      for (var m = 0; m < list.length; m += 1) {
        var item = list[m];
        messages.put({
          id: item.id,
          convId: conv.id,
          kind: item.kind,
          content: item.content,
          createdAt: item.createdAt || 0,
          meta: item.meta || "",
          trace: item.trace || "",
          forceTrace: !!item.forceTrace,
          recalled: item.recalled || [],
        });
      }
      return new Promise(function (resolve, reject) {
        tx.oncomplete = function () { resolve(); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error || new Error("aborted")); };
      });
    });
  }

  function deleteConversation(db, id) {
    var tx = transaction(db, "readwrite");
    tx.objectStore("conversations").delete(id);
    var index = tx.objectStore("messages").index("convId");
    return reqToPromise(index.getAllKeys(id)).then(function (keys) {
      var store = tx.objectStore("messages");
      for (var i = 0; i < (keys || []).length; i += 1) store.delete(keys[i]);
      return new Promise(function (resolve, reject) {
        tx.oncomplete = function () { resolve(); };
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  return {
    STOPWORDS: STOPWORDS,
    SYNONYMS: SYNONYMS,
    DB_NAME: DB_NAME,
    estimateTokens: estimateTokens,
    tokenize: tokenize,
    rank: rank,
    fit: fit,
    assemblePrompt: assemblePrompt,
    openDb: openDb,
    loadAll: loadAll,
    replaceConversation: replaceConversation,
    deleteConversation: deleteConversation,
  };
});
