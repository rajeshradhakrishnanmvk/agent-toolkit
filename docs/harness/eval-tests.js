import { rankResults } from "./leaderboard/leaderboard.js";
import {
  EVAL_SPEC,
  canonicalJson,
  connectEval,
  hashEvalSet,
  previewScript,
  runEval,
  runEvalTask,
  scriptedProvider,
  taskById,
  toolFence,
} from "./eval.js";
import { validateResultDocument, validateResultsDirectory, validateSubmission } from "./validate-results.js";

function checkById(row, id) {
  const checks = row.checks || [];
  for (let i = 0; i < checks.length; i += 1) {
    if (checks[i].id === id) return checks[i];
  }
  return null;
}

function scoreView(file) {
  return JSON.stringify({
    scores: file.scores,
    tasks: (file.tasks || []).map(function (task) {
      return {
        id: task.id,
        category: task.category,
        passed: task.passed,
        total: task.total,
        value: task.value,
        checks: task.checks,
      };
    }),
  });
}

function fixedRun(extra) {
  return runEval(Object.assign({
    stub: true,
    gapMs: 0,
    preview: false,
    label: "Sample model",
    runDate: "2026-01-01T00:00:00.000Z",
  }, extra || {}));
}

export function registerEvalTests(helpers) {
  const test = helpers.test;
  const assert = helpers.assert;
  const equal = helpers.equal;
  const isNode = helpers.isNode;

  test("leaderboard ranks higher totals first", function () {
    const ranked = rankResults([
      { file: "b.json", data: { model: { label: "Bee" }, scores: { value: 0.5 } } },
      { file: "a.json", data: { model: { label: "Ada" }, scores: { value: 1 } } },
      { file: "c.json", data: { model: { label: "Ada" }, scores: { value: 1 } } },
      { file: "d.json", error: true },
    ]);
    equal(ranked.map(function (item) { return item.file; }).join(","), "a.json,c.json,b.json,d.json");
  });

  test("eval hash is stable and matches node crypto in Node", async function () {
    const first = await hashEvalSet();
    const second = await hashEvalSet();
    equal(first, second);
    assert(/^[0-9a-f]{64}$/.test(first));
    const again = canonicalJson(JSON.parse(canonicalJson(taskDocument())));
    equal(again, canonicalJson(taskDocument()));
    if (isNode()) {
      const crypto = await import("node:crypto");
      const hex = crypto.createHash("sha256").update(canonicalJson(taskDocument())).digest("hex");
      equal(first, hex);
    }
  });

  test("the same stubbed transcript always gives the same score", async function () {
    const once = await fixedRun();
    const twice = await fixedRun();
    equal(scoreView(once), scoreView(twice));
    equal(once.scores.passed, once.scores.total);
    equal(once.scores.value, 1);
    equal(once.evalSet.id, "tiny-tools");
    equal(once.evalSet.version, 1);
    equal(once.evalSet.hash, await hashEvalSet());
    equal(once.harnessVersion, "2");
    equal(once.model.label, "Sample model");
    equal(once.model.id, "stub-preview");
    equal(once.preview, false);
    equal(EVAL_SPEC.tasks.length, 10);
    once.tasks.forEach(function (task) {
      task.checks.forEach(function (check) {
        assert(check.ok, task.id + " " + check.id + " " + check.detail);
      });
      task.trace.forEach(function (event) {
        if (event.kind !== "model") return;
        assert(event.promptTokens <= 2048 - 256, task.id + " prompt " + event.promptTokens);
        equal(event.dropped, 0);
      });
    });
    const failLines = [toolFence("lookup", { key: "token" }), "pebble"];
    const failOnce = await runEvalTask(taskById("pick-clock"), { provider: scriptedProvider(failLines) });
    const failTwice = await runEvalTask(taskById("pick-clock"), { provider: scriptedProvider(failLines) });
    equal(JSON.stringify(failOnce.checks), JSON.stringify(failTwice.checks));
    equal(failOnce.value, failTwice.value);
  });

  test("tool-choice pass and fail against a stubbed provider", async function () {
    await expectCategory("tool-choice", "pick-clock", "tool-name", [
      toolFence("lookup", { key: "token" }),
      "pebble",
    ]);
  });

  test("arguments pass and fail against a stubbed provider", async function () {
    await expectCategory("arguments", "args-clock", "args-schema", [
      toolFence("clock", { zone: "UTC" }),
      "2026-01-01T00:00:00.000Z",
    ]);
  });

  test("no-tool pass and fail against a stubbed provider", async function () {
    await expectCategory("no-tool", "plain-sum", "no-tool", [
      toolFence("clock", {}) + "\n4",
      "4",
    ]);
  });

  test("multi-step pass and fail against a stubbed provider", async function () {
    await expectCategory("multi-step", "chain-echo", "second-text", [
      toolFence("lookup", { key: "token" }),
      toolFence("echo", { text: "nope" }),
      "nope",
    ]);
  });

  test("approval pass and fail against a stubbed provider", async function () {
    await expectCategory("approval", "deny-note", "final-no-success", [
      toolFence("note", { text: "hello" }),
      "The note was a success and I saved it.",
    ]);
  });

  test("budget pass and fail against a stubbed provider", async function () {
    await expectCategory("budget", "budget-clock", "stopped", [
      "I will not call a tool.",
    ]);
  });

  test("eval requests temperature 0 and a stub preview does not call the live provider", async function () {
    let temperature = null;
    let calls = 0;
    await runEvalTask(taskById("plain-sum"), {
      provider: async function (request) {
        temperature = request.temperature;
        calls += 1;
        return { content: "4", model: "unit-model" };
      },
    });
    equal(temperature, 0);
    equal(calls, 1);
    let live = 0;
    const file = await runEval({
      stub: true,
      gapMs: 0,
      preview: false,
      label: "Local label",
      provider: async function () {
        live += 1;
        throw new Error("live provider");
      },
    });
    equal(live, 0);
    equal(file.model.id, "stub-preview");
  });

  test("paced eval waits between model requests", async function () {
    const started = Date.now();
    const paced = await runEval({
      tasks: [taskById("plain-sum"), taskById("plain-sky")],
      gapMs: 80,
      preview: false,
      label: "Pace",
      provider: async function () { return { content: "4 blue", model: "paced-model" }; },
    });
    assert(Date.now() - started >= 70);
    equal(paced.model.id, "paced-model");
    equal(paced.model.label, "Pace");
  });

  test("export leaves out secrets, authorization, and endpoint urls", async function () {
    const secret = "sk-" + "abcdefghij";
    const endpoint = "http" + "s://models.example/v1";
    const file = await runEval({
      stub: true,
      gapMs: 0,
      preview: false,
      label: "Label " + secret + " Authorization: Bearer " + secret + " " + endpoint,
      runDate: "2026-01-01T00:00:00.000Z",
    });
    const raw = JSON.stringify(file);
    assert(raw.indexOf(secret) < 0);
    assert(raw.indexOf("models.example") < 0);
    assert(raw.toLowerCase().indexOf("authorization") < 0);
    assert(raw.toLowerCase().indexOf("bearer") < 0);
    assert(raw.indexOf("srv1536099") < 0);
    assert(!Object.prototype.hasOwnProperty.call(file, "apiKey"));
    assert(!Object.prototype.hasOwnProperty.call(file.model, "endpoint"));
  });

  test("stubbed eval connect does not read the api key", function () {
    const connection = connectEval({
      stub: true,
      readKey: function () { throw new Error("read key"); },
      readLabel: function () { throw new Error("read label"); },
      connect: function () { throw new Error("connect"); },
    });
    equal(connection.stub, true);
    equal(connection.label, "stub preview");
    let saw = false;
    const live = connectEval({
      stub: false,
      readKey: function () { saw = true; return "local-key"; },
      readLabel: function () { return "Board name"; },
      readModel: function () { return "Llama-3.2-3B-Instruct"; },
      connect: function () { return async function () { return { content: "ok" }; }; },
    });
    equal(saw, true);
    equal(live.label, "Board name");
    equal(typeof live.provider, "function");
  });

  test("validator accepts a good sample and rejects a tampered score, a bad hash, a missing index entry, and a key-like string", async function () {
    const good = await fixedRun();
    const accepted = await validateSubmission({
      index: { schemaVersion: 1, entries: [{ file: "sample.json" }] },
      files: { "sample.json": good },
    });
    assert(accepted.ok, accepted.errors.join("\n"));
    const tampered = JSON.parse(JSON.stringify(good));
    tampered.scores.value = 0;
    const tamperResult = await validateResultDocument(tampered);
    assert(!tamperResult.ok);
    assert(tamperResult.errors.join("\n").indexOf("tampered score") >= 0);
    const hashed = JSON.parse(JSON.stringify(good));
    hashed.evalSet.hash = "0".repeat(64);
    assert(hashed.evalSet.hash !== good.evalSet.hash);
    const hashResult = await validateResultDocument(hashed);
    assert(!hashResult.ok);
    assert(hashResult.errors.join("\n").indexOf("eval hash") >= 0);
    const missing = await validateSubmission({
      index: { schemaVersion: 1, entries: [] },
      files: { "sample.json": good },
    });
    assert(!missing.ok);
    assert(missing.errors.join("\n").indexOf("missing index entry for sample.json") >= 0);
    const leaked = JSON.parse(JSON.stringify(good));
    leaked.model.label = "sk-" + "abcdefghij";
    const leakResult = await validateResultDocument(leaked);
    assert(!leakResult.ok);
    assert(leakResult.errors.join("\n").indexOf("key-like string") >= 0);
    const preview = JSON.parse(JSON.stringify(good));
    preview.preview = true;
    const previewResult = await validateResultDocument(preview);
    assert(!previewResult.ok);
  });

  if (isNode()) {
    test("shipped results index has a pending Carrie row and no scored files", async function () {
      const fs = await import("node:fs/promises");
      const path = await import("node:path");
      const url = await import("node:url");
      const harnessDir = path.dirname(url.fileURLToPath(import.meta.url));
      const dir = path.join(harnessDir, "results");
      const names = (await fs.readdir(dir)).filter(function (name) { return name.endsWith(".json"); }).sort();
      equal(names.join(","), "index.json,result.schema.json");
      const report = await validateResultsDirectory(dir);
      assert(report.ok, report.errors.join("\n"));
      const index = JSON.parse(await fs.readFile(path.join(dir, "index.json"), "utf8"));
      equal(index.entries.length, 1);
      equal(index.entries[0].pending, true);
      equal(index.entries[0].label, "Carrie White");
      equal(index.entries[0].modelLabel, "Llama 3.2 3B via llama.cpp");
      assert(!index.entries[0].file);
      assert(index.entries[0].scores == null);
      assert(index.entries[0].note.indexOf("Pending live run") >= 0);
      assert(index.entries[0].note.indexOf("Rajesh") >= 0);
      const page = await fs.readFile(path.join(harnessDir, "index.html"), "utf8");
      assert(page.indexOf("Run eval") >= 0);
      assert(page.indexOf('id="run-eval"') >= 0);
      assert(page.indexOf("leaderboard/") >= 0);
      const board = await fs.readFile(path.join(harnessDir, "leaderboard", "index.html"), "utf8");
      assert(board.indexOf("leaderboard.js") >= 0);
      assert(board.toLowerCase().indexOf("src=\"http") < 0);
    });
  }

  async function expectCategory(category, id, failingCheck, lines) {
    const task = taskById(id);
    equal(task.category, category);
    const pass = await runEvalTask(task, { provider: scriptedProvider(previewScript(id)) });
    equal(pass.value, 1, id + " pass " + JSON.stringify(pass.checks));
    pass.checks.forEach(function (check) { assert(check.ok, id + " " + check.id); });
    let temperature = null;
    const scripted = scriptedProvider(lines);
    const fail = await runEvalTask(task, {
      provider: async function (request) {
        temperature = request.temperature;
        return scripted(request);
      },
    });
    equal(temperature, 0);
    const broken = checkById(fail, failingCheck);
    assert(broken, failingCheck);
    equal(broken.ok, false, id + " " + failingCheck);
    assert(fail.value < 1);
  }
}

function taskDocument() {
  return {
    id: EVAL_SPEC.id,
    version: EVAL_SPEC.version,
    temperature: EVAL_SPEC.temperature,
    contextTokens: EVAL_SPEC.contextTokens,
    replyTokens: EVAL_SPEC.replyTokens,
    maxIterations: EVAL_SPEC.maxIterations,
    instructions: EVAL_SPEC.instructions,
    tasks: EVAL_SPEC.tasks,
  };
}
