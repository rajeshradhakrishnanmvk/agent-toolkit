import { EVAL_SPEC, hashEvalSet } from "../eval.js";

const CATEGORIES = ["tool-choice", "arguments", "no-tool", "multi-step", "approval", "budget"];

export function rankResults(items) {
  return (items || []).slice().sort(function (a, b) {
    const left = scoreValue(a);
    const right = scoreValue(b);
    if (right !== left) return right - left;
    const byLabel = labelOf(a).localeCompare(labelOf(b));
    if (byLabel) return byLabel;
    return String(a.file || "").localeCompare(String(b.file || ""));
  });
}

function scoreValue(item) {
  const data = item && item.data ? item.data : item;
  const value = data && data.scores ? data.scores.value : undefined;
  return typeof value === "number" ? value : -1;
}

function labelOf(item) {
  const data = item && item.data ? item.data : item;
  if (data && data.model && data.model.label) return String(data.model.label);
  if (item && item.label) return String(item.label);
  return String(item && item.file || "");
}

function cell(text) {
  const item = document.createElement("td");
  item.textContent = text;
  return item;
}

function percent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return (Math.round(value * 1000) / 10).toFixed(1) + "%";
}

function categoryValue(scores, name) {
  const row = scores && scores.categories && scores.categories[name];
  return row ? percent(row.value) : "—";
}

function safeName(name) {
  return /^[A-Za-z0-9._-]+$/.test(name);
}

function dashRow(colspan, text) {
  const row = document.createElement("tr");
  const item = document.createElement("td");
  item.colSpan = colspan;
  item.textContent = text;
  row.appendChild(item);
  return row;
}

async function load() {
  const metaEl = document.getElementById("meta");
  const emptyEl = document.getElementById("empty");
  const scoredEl = document.getElementById("scored");
  const pendingEl = document.getElementById("pending");
  const hash = await hashEvalSet();
  metaEl.textContent = "Eval set " + EVAL_SPEC.id + " version " + EVAL_SPEC.version + ". Hash " + hash + ".";
  let index;
  try {
    const response = await fetch(new URL("../results/index.json", import.meta.url));
    if (!response.ok) throw new Error("index " + response.status);
    index = await response.json();
  } catch (err) {
    metaEl.textContent = "The results index did not load.";
    emptyEl.hidden = false;
    pendingEl.appendChild(dashRow(4, "No pending row."));
    return;
  }
  const entries = Array.isArray(index.entries) ? index.entries : [];
  const scored = [];
  entries.forEach(function (entry) {
    if (entry && entry.pending === true) renderPending(entry, pendingEl);
  });
  if (!pendingEl.children.length) pendingEl.appendChild(dashRow(4, "None."));
  for (let i = 0; i < entries.length; i += 1) {
    const entry = entries[i];
    if (!entry || entry.pending === true || typeof entry.file !== "string" || !safeName(entry.file)) continue;
    try {
      const response = await fetch(new URL("../results/" + entry.file, import.meta.url));
      if (!response.ok) throw new Error(String(response.status));
      const file = await response.json();
      scored.push({ file: entry.file, data: file });
    } catch (err) {
      scored.push({ file: entry.file, error: true });
    }
  }
  const ranked = rankResults(scored);
  if (!ranked.length) {
    emptyEl.hidden = false;
    document.getElementById("board").hidden = true;
    return;
  }
  emptyEl.hidden = true;
  ranked.forEach(function (item, index) {
    scoredEl.appendChild(renderScored(item, index + 1, hash));
  });
}

function renderPending(entry, pendingEl) {
  const row = document.createElement("tr");
  row.className = "pending";
  const who = document.createElement("td");
  const name = document.createElement("strong");
  name.textContent = entry.label || "Pending";
  who.appendChild(name);
  if (entry.modelLabel) {
    const model = document.createElement("div");
    model.className = "muted";
    model.textContent = entry.modelLabel;
    who.appendChild(model);
  }
  row.appendChild(who);
  row.appendChild(cell("—"));
  const status = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = "pending live run";
  status.appendChild(badge);
  row.appendChild(status);
  row.appendChild(cell(entry.note || ""));
  pendingEl.appendChild(row);
}

function renderScored(item, rank, hash) {
  const row = document.createElement("tr");
  if (!item.data) {
    row.appendChild(cell(String(rank)));
    row.appendChild(cell(item.file));
    row.appendChild(cell("—"));
    CATEGORIES.forEach(function () { row.appendChild(cell("—")); });
    row.appendChild(cell("—"));
    row.appendChild(fileLink(item.file, "unreadable"));
    return row;
  }
  const data = item.data;
  const model = data.model || {};
  row.appendChild(cell(String(rank)));
  const who = document.createElement("td");
  const name = document.createElement("strong");
  name.textContent = model.label || item.file;
  who.appendChild(name);
  if (model.id) {
    const id = document.createElement("div");
    id.className = "muted";
    id.textContent = model.id;
    who.appendChild(id);
  }
  if (!data.evalSet || data.evalSet.hash !== hash) {
    const warn = document.createElement("div");
    warn.className = "muted";
    warn.textContent = "Eval hash differs from this page.";
    who.appendChild(warn);
  }
  row.appendChild(who);
  row.appendChild(cell(percent(data.scores && data.scores.value)));
  CATEGORIES.forEach(function (name) {
    row.appendChild(cell(categoryValue(data.scores, name)));
  });
  const date = typeof data.runDate === "string" ? data.runDate.slice(0, 10) : "—";
  row.appendChild(cell(date));
  row.appendChild(fileLink(item.file, item.file));
  return row;
}

function fileLink(name, text) {
  const item = document.createElement("td");
  const link = document.createElement("a");
  link.href = "../results/" + name;
  link.textContent = text;
  item.appendChild(link);
  return item;
}

if (typeof document !== "undefined") load();
