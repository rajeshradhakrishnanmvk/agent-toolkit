/* Takeoff state machine. Decisions are deterministic. A model may only phrase a phase. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.Airport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var PHASES = ["preflight", "clearance", "atc", "taxi", "takeoff", "after"];

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function createFlight(plan, checklist) {
    var source = plan || {};
    return {
      number: source.flight || "TK-100",
      origin: source.origin || "",
      destination: source.destination || "",
      cargo: source.cargo || "",
      weather: source.weather || "",
      status: "Scheduled",
      phase: "preflight",
      hold: null,
      checklist: (checklist || []).map(function (item) {
        return { id: item.id, label: item.label, phase: item.phase, done: false };
      }),
      log: [],
      runwaySince: null,
      calls: 0,
      departed: false,
      cancelled: false,
      request: "",
      budgetOverride: false,
    };
  }

  function tick(flight, id) {
    flight.checklist.forEach(function (item) {
      if (item.id === id) item.done = true;
    });
  }

  function radio(flight, role, text, now, speech) {
    flight.log.push({ t: now, role: role, text: text });
    speech.push({ role: role, text: text, kind: "assistant" });
  }

  function pack(flight, speech, model) {
    return { flight: flight, say: speech, model: model, hold: flight.hold, done: !!flight.departed };
  }

  function missingInfo(flight) {
    var missing = [];
    if (!flight.destination || !String(flight.destination).trim()) missing.push("destination");
    if (!flight.request || String(flight.request).trim().length < 12) missing.push("a fuller request");
    return missing;
  }

  function runwayOccupant(flights, self) {
    var list = flights || [];
    var found = null;
    for (var i = 0; i < list.length; i += 1) {
      var other = list[i];
      if (!other || other.number === self.number && other.id === self.id) continue;
      if (!other.runwaySince || other.departed || other.cancelled) continue;
      if (!found || other.runwaySince < found.runwaySince) found = other;
    }
    return found;
  }

  function atcDecision(flight, ctx) {
    if (!flight.destination || !String(flight.destination).trim()) {
      return { code: "HOLD", kind: "info", reason: "no destination on the flight plan" };
    }
    if (!flight.request || String(flight.request).trim().length < 12) {
      return { code: "HOLD", kind: "info", reason: "request is incomplete" };
    }
    var occupant = runwayOccupant(ctx.flights || [], flight);
    if (occupant && (!flight.runwaySince || occupant.runwaySince < flight.runwaySince)) {
      return {
        code: "HOLD",
        kind: "runway",
        reason: "runway " + (ctx.runway || "18") + " is held by " + (occupant.number || "another flight"),
      };
    }
    if (!flight.budgetOverride && ctx.budget && ctx.budget.ok === false) {
      return { code: "HOLD", kind: "approval", reason: ctx.budget.reason || "fuel is past the cap" };
    }
    if (flight.weather && /reroute/i.test(String(flight.weather))) {
      return { code: "REROUTE", kind: "reroute", reason: "weather requires a new destination" };
    }
    return { code: "CLEARED", kind: "cleared", reason: "" };
  }

  function modelRequest(flight, role, phase, ctx) {
    var plan = flight.number + " " + (flight.origin || "origin") + " to " + (flight.destination || "destination");
    var user = "Phase " + phase + " for " + plan + ". Request: " + (flight.request || "");
    if (flight.cargo) user += "\nCargo: " + flight.cargo;
    if (phase === "clearance") user += "\nSummarize the plan in two short sentences and request clearance.";
    if (phase === "atc") user += "\nThe checks passed. Phrase a short CLEARED radio call. Do not change the decision.";
    if (phase === "takeoff") user += "\nGive the plain-language answer the passenger asked for. Be concrete.";
    if (phase === "after") user += "\nOne short debrief of what was delivered. No new advice.";
    if (phase === "inflight") user += "\nContinue from the earlier answer. Stay concrete.";
    return {
      role: role,
      phase: phase,
      user: user,
      systemExtra: (ctx && ctx.radio) || "",
    };
  }

  function completePhase(flight) {
    if (flight.phase === "clearance") {
      tick(flight, "plan");
      flight.phase = "atc";
      return;
    }
    if (flight.phase === "atc") {
      flight.phase = "taxi";
      return;
    }
    if (flight.phase === "takeoff") {
      flight.status = "Cleared";
      flight.phase = "after";
      return;
    }
    if (flight.phase === "after") {
      flight.phase = "inflight";
      flight.status = "Departed";
      flight.departed = true;
      flight.runwaySince = null;
    }
  }

  function hold(flight, speech, role, kind, reason, now, text) {
    flight.status = "Holding";
    flight.hold = { kind: kind, reason: reason };
    radio(flight, role, text, now, speech);
    return pack(flight, speech, null);
  }

  function runUntilModel(flight, speech, ctx, now) {
    var guard = 0;
    while (guard < 8 && !flight.departed && !flight.cancelled && !flight.hold) {
      guard += 1;
      if (flight.phase === "preflight") {
        var missing = missingInfo(flight);
        if (missing.length) {
          return hold(
            flight,
            speech,
            "ground",
            "info",
            missing.join(", "),
            now,
            "HOLD " + flight.number + ". Need " + missing.join(" and ") + " before pushback."
          );
        }
        tick(flight, "request");
        tick(flight, "cargo");
        flight.status = "Boarding";
        radio(flight, "ground", "Ground. Request and cargo checked for " + flight.number + ".", now, speech);
        flight.phase = "clearance";
        continue;
      }
      if (flight.phase === "clearance") {
        flight.status = "Boarding";
        return pack(flight, speech, modelRequest(flight, "captain", "clearance", ctx));
      }
      if (flight.phase === "atc") {
        var decision = atcDecision(flight, ctx);
        if (decision.code === "REROUTE") {
          return hold(flight, speech, "atc", "reroute", decision.reason, now, "REROUTE " + flight.number + ". " + decision.reason + ".");
        }
        if (decision.code === "HOLD") {
          var line = decision.kind === "approval"
            ? (ctx.askTemplate || "I need human approval before continuing: {reason}").replace("{reason}", decision.reason)
            : "HOLD " + flight.number + ". " + decision.reason + ".";
          return hold(flight, speech, "atc", decision.kind, decision.reason, now, line);
        }
        if (!flight.runwaySince) flight.runwaySince = now;
        tick(flight, "fuel");
        tick(flight, "handoff");
        tick(flight, "runway");
        flight.status = "Taxiing";
        radio(flight, "atc", "Checks complete. Decision CLEARED for " + flight.number + ".", now, speech);
        return pack(flight, speech, modelRequest(flight, "atc", "atc", ctx));
      }
      if (flight.phase === "taxi") {
        tick(flight, "readback");
        flight.status = "Taxiing";
        radio(
          flight,
          "first-officer",
          flight.number + " read-back. Request, cargo, plan, fuel, handoff, runway, and line-up are ticked.",
          now,
          speech
        );
        flight.phase = "takeoff";
        continue;
      }
      if (flight.phase === "takeoff") {
        flight.status = "Cleared";
        return pack(flight, speech, modelRequest(flight, "captain", "takeoff", ctx));
      }
      if (flight.phase === "after") {
        return pack(flight, speech, modelRequest(flight, "cabin", "after", ctx));
      }
      break;
    }
    return pack(flight, speech, null);
  }

  function resolveHold(flight, text, ctx, now) {
    var speech = [];
    var value = String(text || "").trim();
    if (/^(abort|cancel)\b/i.test(value)) {
      return advance(flight, { type: "abort" }, ctx);
    }
    var kind = flight.hold && flight.hold.kind;
    flight.hold = null;
    if (kind === "info" || kind === "reroute") {
      if (value.length >= 12) flight.request = value;
      if (kind === "reroute") flight.destination = value;
      flight.phase = "preflight";
      flight.status = "Boarding";
      return runUntilModel(flight, speech, ctx, now);
    }
    if (kind === "approval") {
      if (!/^(approve|approved|yes|cleared)\b/i.test(value)) {
        flight.hold = { kind: "approval", reason: "still waiting for approval" };
        flight.status = "Holding";
        radio(flight, "atc", "HOLD " + flight.number + ". Say approve to continue, or abort.", now, speech);
        return pack(flight, speech, null);
      }
      flight.budgetOverride = true;
      flight.phase = "atc";
      flight.status = "Taxiing";
      return runUntilModel(flight, speech, ctx, now);
    }
    if (kind === "runway" || kind === "traffic") {
      flight.phase = kind === "traffic" ? flight.phase : "atc";
      return runUntilModel(flight, speech, ctx, now);
    }
    return runUntilModel(flight, speech, ctx, now);
  }

  function followUp(flight, text, ctx, now) {
    flight.request = text;
    flight.status = "Departed";
    return pack(flight, [], modelRequest(flight, "captain", "inflight", ctx));
  }

  function advance(flight, event, ctx) {
    var nextFlight = clone(flight);
    var speech = [];
    var now = (ctx && ctx.now) || 0;
    event = event || { type: "user", text: "" };
    ctx = ctx || {};
    if (!nextFlight.id && ctx.id) nextFlight.id = ctx.id;
    if (event.type === "abort") {
      nextFlight.status = "Cancelled";
      nextFlight.cancelled = true;
      nextFlight.hold = null;
      nextFlight.runwaySince = null;
      radio(nextFlight, "atc", "Flight " + nextFlight.number + " cancelled. Runway released.", now, speech);
      return pack(nextFlight, speech, null);
    }
    if (nextFlight.departed && event.type === "user") return followUp(nextFlight, event.text, ctx, now);
    if (nextFlight.hold && event.type === "user") return resolveHold(nextFlight, event.text, ctx, now);
    if (nextFlight.hold && event.type === "retry") {
      var retryPhase = nextFlight.phase;
      nextFlight.hold = null;
      nextFlight.phase = retryPhase;
      return runUntilModel(nextFlight, speech, ctx, now);
    }
    if (event.type === "model") {
      radio(nextFlight, event.role, event.text, now, speech);
      nextFlight.calls += 1;
      completePhase(nextFlight);
      if (nextFlight.departed) return pack(nextFlight, speech, null);
      return runUntilModel(nextFlight, speech, ctx, now);
    }
    if (event.type === "user") nextFlight.request = event.text || nextFlight.request;
    if (nextFlight.status === "Scheduled") nextFlight.status = "Boarding";
    return runUntilModel(nextFlight, speech, ctx, now);
  }

  function countTakeoffModels(plan, checklist, request) {
    var flight = createFlight(plan, checklist);
    flight.id = "self";
    var ctx = { budget: { ok: true }, flights: [flight], now: 1, runway: "18", id: "self" };
    var calls = 0;
    var step = advance(flight, { type: "user", text: request }, ctx);
    var guard = 0;
    while (step.model && guard < 6) {
      guard += 1;
      calls += 1;
      ctx.flights = [step.flight];
      ctx.now += 1;
      step = advance(step.flight, { type: "model", role: step.model.role, text: "Roger." }, ctx);
    }
    return { calls: calls, status: step.flight.status, departed: step.flight.departed };
  }

  return {
    PHASES: PHASES,
    createFlight: createFlight,
    advance: advance,
    atcDecision: atcDecision,
    countTakeoffModels: countTakeoffModels,
  };
});
