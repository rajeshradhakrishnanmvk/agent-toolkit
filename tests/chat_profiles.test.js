"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var profiles = require("../docs/chat/profiles.js");
var memory = require("../docs/chat/memory.js");

function read(name) {
  return JSON.parse(fs.readFileSync(path.join(__dirname, "..", "docs", "chat", "profiles", name), "utf8"));
}

var globalProfile = read("global.json");
var soulA = {
  id: "alpha",
  kind: "soul",
  name: "Alpha",
  instructions: "SOUL_RULE",
  config: { temperature: 0.4, memory: { topK: 2 } },
};
var soulB = {
  id: "beta",
  kind: "soul",
  name: "Beta",
  instructions: "OTHER_SOUL",
  config: { temperature: 0.9 },
};
var session = {
  id: "proj",
  kind: "session",
  name: "Proj",
  instructions: "SESSION_RULE",
  config: { soulId: "beta", temperature: 0.1, goals: "Ship the notes.", memory: { scope: "all" } },
};
globalProfile.instructions = "GLOBAL_RULE";

var config = profiles.resolveConfig(globalProfile, soulB, session);
assert.strictEqual(config.temperature, 0.1, "session overrides soul and global");
assert.strictEqual(config.model, globalProfile.config.model, "unset fields stay global");
assert.strictEqual(config.memory.scope, "all", "session memory scope wins");
assert.strictEqual(config.memory.topK, globalProfile.config.memory.topK, "soul topK is not this soul");
assert.strictEqual(config.memory.recallTokens, 500);

var switched = profiles.resolveConfig(globalProfile, soulA, {
  id: "proj",
  kind: "session",
  instructions: "SESSION_RULE",
  config: { soulId: "alpha" },
});
assert.strictEqual(switched.temperature, 0.4, "soul overrides global when session does not");
assert.strictEqual(switched.memory.topK, 2);

var text = profiles.composeInstructions(globalProfile, soulB, session);
assert.ok(text.indexOf("GLOBAL_RULE") < text.indexOf("OTHER_SOUL"));
assert.ok(text.indexOf("OTHER_SOUL") < text.indexOf("SESSION_RULE"));
assert.ok(text.indexOf("Ship the notes.") > text.indexOf("SESSION_RULE"));
assert.strictEqual(profiles.validateProfile(session), "");
assert.ok(profiles.validateProfile({ id: "x", kind: "nope" }));

var financial = read("souls/financial-consultant.json");
var turn = profiles.buildTurn(financial.config, "How large is the fund?", 1);
assert.ok(turn.currentUser.indexOf("INTAKE:") !== -1);
assert.ok(turn.currentUser.indexOf("How large is the fund?") !== -1);
assert.ok(turn.localTexts[1].indexOf("emergency fund") !== -1);
assert.ok(turn.claims.some(function (line) { return line.indexOf("advisor-bot") !== -1; }));

var plain = profiles.buildTurn({}, "Hello", 1);
assert.strictEqual(plain.currentUser, "Hello");

var packed = memory.assemblePrompt({
  memoryOn: true,
  system: text + " " + "word ".repeat(2000),
  summary: "summary ".repeat(400),
  query: "fund",
  currentUser: turn.currentUser,
  recent: [{ id: "r1", role: "user", content: "recent ".repeat(800) }],
  corpus: [{ id: "c1", convId: "s", content: "Keep the emergency fund." }],
  replyTokens: 256,
  safetyTokens: 64,
  contextTokens: 2048,
  memoryTokens: 500,
  topK: 4,
  synonyms: config.memory.synonyms,
});
assert.ok(packed.promptTokens <= packed.budget, packed.promptTokens + " > " + packed.budget);
assert.ok(packed.promptTokens + 256 + 64 <= 2048);

console.log("chat profile tests ok");
