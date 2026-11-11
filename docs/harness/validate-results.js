/* Check leaderboard submissions. Scores are recomputed from per-task checks. */

import {
  HARNESS_VERSION,
  RESULT_SCHEMA_VERSION,
  aggregateScores,
  hashEvalSet,
  tallyChecks,
  taskSetDocument,
} from "./eval.js";

function secretProblem(text) {
  const raw = String(text || "");
  if (/sk-[A-Za-z0-9]{8,}/.test(raw)) return "key-like string";
  if (/AKIA[0-9A-Z]{16}/.test(raw)) return "key-like string";
  if (/ghp_[A-Za-z0-9]{20,}/.test(raw)) return "key-like string";
  if (new RegExp("bearer" + "\\s+\\S+", "i").test(raw)) return "authorization header";
  if (/authorization/i.test(raw)) return "authorization header";
  if (new RegExp("https?" + ":\\/\\/", "i").test(raw)) return "endpoint url";
  if (raw.indexOf("srv1536099") >= 0) return "endpoint url";
  return "";
}

const FORBIDDEN_FIELDS = {
  apikey: true,
  api_key: true,
  authorization: true,
  endpoint: true,
  baseurl: true,
  base_url: true,
  url: true,
  secret: true,
};

function walkFields(value, errors, path) {
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    value.forEach(function (item, index) {
      walkFields(item, errors, path + "[" + index + "]");
    });
    return;
  }
  Object.keys(value).forEach(function (key) {
    if (FORBIDDEN_FIELDS[String(key).toLowerCase()]) errors.push("forbidden field " + path + "." + key);
    walkFields(value[key], errors, path + "." + key);
  });
}

function sameScore(stored, fresh) {
  return !!stored
    && stored.passed === fresh.passed
    && stored.total === fresh.total
    && stored.value === fresh.value;
}

export async function validateResultDocument(data) {
  const errors = [];
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { ok: false, errors: ["result must be an object"] };
  }
  const problem = secretProblem(JSON.stringify(data));
  if (problem) errors.push(problem);
  walkFields(data, errors, "result");
  if (data.schemaVersion !== RESULT_SCHEMA_VERSION) errors.push("schema version must be 1");
  if (data.harnessVersion !== HARNESS_VERSION) errors.push("harness version must be 2");
  if (data.preview === true) errors.push("stub preview cannot be submitted");
  if (typeof data.runDate !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(data.runDate)) {
    errors.push("run date must be an ISO timestamp");
  }
  const model = data.model || {};
  if (typeof model.label !== "string" || !model.label.trim()) errors.push("model label is required");
  if (typeof model.id !== "string") errors.push("model id must be a string");
  const spec = taskSetDocument();
  const expectedHash = await hashEvalSet();
  const evalSet = data.evalSet || {};
  if (evalSet.id !== spec.id || evalSet.version !== spec.version) errors.push("eval set must be tiny-tools v1");
  if (evalSet.hash !== expectedHash) errors.push("eval hash does not match tiny-tools v1");
  const tasks = Array.isArray(data.tasks) ? data.tasks : null;
  if (!tasks) {
    errors.push("tasks must be a list");
    return { ok: false, errors: errors };
  }
  const byId = {};
  tasks.forEach(function (task) {
    if (!task || typeof task.id !== "string") {
      errors.push("task is missing an id");
      return;
    }
    if (byId[task.id]) errors.push("duplicate task " + task.id);
    byId[task.id] = task;
  });
  if (tasks.length !== spec.tasks.length) errors.push("task list does not match tiny-tools v1");
  spec.tasks.forEach(function (specTask) {
    const task = byId[specTask.id];
    if (!task) {
      errors.push("missing task " + specTask.id);
      return;
    }
    if (task.category !== specTask.category) errors.push("category mismatch for " + specTask.id);
    if (!Array.isArray(task.checks)) {
      errors.push("checks missing for " + specTask.id);
      return;
    }
    if (!Array.isArray(task.trace)) errors.push("trace missing for " + specTask.id);
    if (task.checks.length !== specTask.checks.length) errors.push("check list mismatch for " + specTask.id);
    for (let i = 0; i < specTask.checks.length; i += 1) {
      const check = task.checks[i];
      if (!check || check.id !== specTask.checks[i].id || typeof check.ok !== "boolean") {
        errors.push("check mismatch for " + specTask.id);
        break;
      }
    }
    const fresh = tallyChecks(task.checks);
    if (!sameScore(task, fresh)) errors.push("tampered score");
  });
  const scores = aggregateScores(tasks);
  if (!sameScore(data.scores, scores)) errors.push("tampered score");
  const storedCategories = (data.scores && data.scores.categories) || {};
  const freshNames = Object.keys(scores.categories);
  const storedNames = Object.keys(storedCategories);
  if (freshNames.length !== storedNames.length) errors.push("tampered score");
  freshNames.forEach(function (name) {
    if (!sameScore(storedCategories[name], scores.categories[name])) errors.push("tampered score");
  });
  return { ok: errors.length === 0, errors: errors };
}

export async function validateSubmission(bundle) {
  const input = bundle || {};
  const errors = [];
  const index = input.index;
  const files = input.files || {};
  if (!index || index.schemaVersion !== RESULT_SCHEMA_VERSION || !Array.isArray(index.entries)) {
    return { ok: false, errors: ["index entries must be a list"] };
  }
  const indexProblem = secretProblem(JSON.stringify(index));
  if (indexProblem) errors.push("index has a " + indexProblem);
  const listed = {};
  for (let i = 0; i < index.entries.length; i += 1) {
    const entry = index.entries[i];
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      errors.push("index entry " + i + " is not an object");
      continue;
    }
    if (entry.pending === true) {
      if (entry.file) errors.push("pending entry must not point at a file");
      if (entry.scores != null || entry.checks != null || entry.value != null) {
        errors.push("pending entry must not include scores");
      }
      if (typeof entry.label !== "string" || !entry.label.trim()) errors.push("pending entry needs a label");
      continue;
    }
    if (typeof entry.file !== "string" || !entry.file || entry.file.indexOf("/") >= 0 || entry.file.indexOf("\\") >= 0 || entry.file.indexOf("..") >= 0) {
      errors.push("index entry needs a file");
      continue;
    }
    if (!Object.prototype.hasOwnProperty.call(files, entry.file)) {
      errors.push("missing file " + entry.file);
      continue;
    }
    if (listed[entry.file]) errors.push("duplicate index file " + entry.file);
    listed[entry.file] = true;
    const result = await validateResultDocument(files[entry.file]);
    result.errors.forEach(function (item) { errors.push(entry.file + ": " + item); });
  }
  Object.keys(files).forEach(function (name) {
    if (!listed[name]) errors.push("missing index entry for " + name);
  });
  return { ok: errors.length === 0, errors: errors };
}

const SKIPPED = { "index.json": true, "result.schema.json": true };

export async function validateResultsDirectory(dir) {
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const index = JSON.parse(await fs.readFile(path.join(dir, "index.json"), "utf8"));
  const names = await fs.readdir(dir);
  const files = {};
  for (let i = 0; i < names.length; i += 1) {
    const name = names[i];
    if (!name.endsWith(".json") || SKIPPED[name]) continue;
    files[name] = JSON.parse(await fs.readFile(path.join(dir, name), "utf8"));
  }
  return validateSubmission({ index: index, files: files });
}
