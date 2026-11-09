"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var airport = require("../docs/chat/airport.js");
var profiles = require("../docs/chat/profiles.js");
var memory = require("../docs/chat/memory.js");

var checklist = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "docs", "chat", "profiles", "global.json"), "utf8")
).config.airport.checklist;

var plan = {
  flight: "TK-104",
  origin: "ATK",
  destination: "Publish the weekly notes",
  cargo: "Draft outline",
  weather: "",
};

function ctx(flight, extra) {
  extra = extra || {};
  return {
    id: flight.id,
    now: extra.now || 1,
    runway: "18",
    budget: extra.budget || { ok: true, reason: "" },
    flights: extra.flights || [],
    askTemplate: "I need human approval before continuing: {reason}",
  };
}

var flight = airport.createFlight(plan, checklist);
flight.id = "self";
var held = airport.advance(flight, { type: "user", text: "hi" }, ctx(flight));
assert.strictEqual(held.flight.status, "Holding");
assert.strictEqual(held.flight.hold.kind, "info");
assert.strictEqual(held.model, null);
assert.strictEqual(held.flight.calls, 0);

var aborted = airport.advance(held.flight, { type: "abort" }, ctx(held.flight));
assert.strictEqual(aborted.flight.status, "Cancelled");
assert.strictEqual(aborted.flight.runwaySince, null);

var resumed = airport.advance(
  held.flight,
  { type: "user", text: "Write a clear launch note for the weekly project." },
  ctx(held.flight)
);
assert.strictEqual(resumed.model.phase, "clearance");
assert.strictEqual(resumed.model.role, "captain");

var counted = airport.countTakeoffModels(
  plan,
  checklist,
  "Write a clear launch note for the weekly project."
);
assert.strictEqual(counted.calls, 4);
assert.strictEqual(counted.status, "Departed");
assert.strictEqual(counted.departed, true);

var queued = airport.createFlight(plan, checklist);
queued.id = "queued";
var occupier = airport.createFlight(plan, checklist);
occupier.id = "other";
occupier.number = "TK-200";
occupier.runwaySince = 1;
occupier.phase = "taxi";
var queuedStep = airport.advance(
  queued,
  { type: "user", text: "Write a clear launch note for the weekly project." },
  ctx(queued, { flights: [occupier] })
);
queuedStep = airport.advance(
  queuedStep.flight,
  { type: "model", role: "captain", text: "Requesting clearance." },
  ctx(queuedStep.flight, { flights: [occupier], now: 2 })
);
assert.strictEqual(queuedStep.flight.status, "Holding");
assert.strictEqual(queuedStep.flight.hold.kind, "runway");
assert.ok(!queuedStep.model);

var fuel = airport.createFlight(plan, checklist);
fuel.id = "fuel";
var fuelStep = airport.advance(
  fuel,
  { type: "user", text: "Write a clear launch note for the weekly project." },
  ctx(fuel, { budget: { ok: false, reason: "tokens would reach 13000" } })
);
fuelStep = airport.advance(
  fuelStep.flight,
  { type: "model", role: "captain", text: "Requesting clearance." },
  ctx(fuelStep.flight, { budget: { ok: false, reason: "tokens would reach 13000" }, now: 3 })
);
assert.strictEqual(fuelStep.flight.hold.kind, "approval");
assert.ok(fuelStep.say.some(function (line) {
  return line.text.indexOf("I need human approval before continuing:") === 0;
}));

var globalProfile = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "docs", "chat", "profiles", "global.json"), "utf8")
);
var captain = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "docs", "chat", "profiles", "souls", "captain.json"), "utf8")
);
var session = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "docs", "chat", "profiles", "sessions", "example.json"), "utf8")
);
var system = profiles.composeInstructions(globalProfile, captain, session);
var packed = memory.assemblePrompt({
  memoryOn: true,
  system: system,
  summary: "",
  query: "launch note",
  currentUser: "Write the launch note.",
  recent: [],
  corpus: [],
  replyTokens: 256,
  safetyTokens: 64,
  contextTokens: 2048,
  memoryTokens: 500,
});
assert.ok(packed.promptTokens <= packed.budget);
assert.ok(system.indexOf("captain") !== -1 || system.indexOf("Captain") !== -1);

console.log("airport tests ok");
