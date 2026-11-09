"use strict";

var assert = require("assert");
var memory = require("../docs/chat/memory.js");

function doc(id, content) {
  return { id: id, convId: "c1", content: content };
}

var tokens = memory.tokenize("The kids are retiring soon");
assert.ok(tokens.indexOf("child") !== -1, "synonym kid -> child");
assert.ok(tokens.indexOf("retire") !== -1, "stem retirement/retiring -> retire");
assert.ok(tokens.indexOf("the") === -1, "stopword dropped");

var ranked = memory.rank("emergency fund for kids", [
  doc("a", "We keep an emergency fund for each child."),
  doc("b", "Pizza night is on Friday."),
]);
assert.strictEqual(ranked[0].id, "a");
assert.ok(ranked[0].score > 0);
assert.strictEqual(ranked[1].score, 0);

var off = memory.assemblePrompt({
  memoryOn: false,
  system: "system rules",
  currentUser: "current question",
  summary: "old summary",
  recent: [{ id: "r1", role: "user", content: "secret earlier turn about boats" }],
  corpus: [doc("a", "emergency fund")],
  query: "emergency fund",
});
assert.strictEqual(off.messages.length, 2);
assert.ok(off.messages[1].content.indexOf("secret earlier") === -1);
assert.strictEqual(off.recalled.length, 0);

var filler = "savings goal ".repeat(400);
var on = memory.assemblePrompt({
  memoryOn: true,
  system: "system rules",
  summary: "They want an emergency fund. " + filler,
  query: "emergency fund",
  currentUser: "What should I do next?",
  recent: [
    { id: "r1", role: "user", content: filler },
    { id: "r2", role: "assistant", content: filler },
  ],
  corpus: [
    doc("a", "Keep six months in the emergency fund."),
    doc("b", "Pizza night is on Friday."),
    doc("r1", filler),
  ],
  replyTokens: 256,
  safetyTokens: 64,
  contextTokens: 2048,
  memoryTokens: 500,
  topK: 4,
});
assert.ok(on.promptTokens <= on.budget, "prompt " + on.promptTokens + " budget " + on.budget);
assert.ok(on.recalled.every(function (item) { return item.id !== "b"; }));
assert.ok(on.messages[0].content.indexOf("Conversation summary:") !== -1);
assert.ok(on.promptTokens + 256 + 64 <= 2048);

console.log("chat memory tests ok");
