/* Handoff checks for the browser harness. Same decisions as handoff_checker.py. */

export const HANDOFF_READY = "ready";
export const HANDOFF_NEEDS_FIXES = "needs-fixes";
export const HANDOFF_BLOCKED = "blocked";

const FIX = "fix";
const BLOCK = "block";
const DEFAULT_MAX_BYTES = 16 * 1024;
const HARD_CAP_BYTES = 2 * 1024 * 1024;

const REQUIRED_FIELDS = [
  ["goal", "Say what the next owner should finish."],
  ["acceptance_criteria", "Add at least one concrete check for done."],
  ["owner", "Name who owns the next step."],
  ["constraints", "List the limits the next owner must not cross."],
  ["budget_left", "Say what is left to spend (tokens, dollars, or tool calls)."],
];

const ALIASES = {
  goal: ["goal"],
  acceptance_criteria: ["acceptance_criteria", "acceptance", "done_when"],
  owner: ["owner", "next_owner", "assignee"],
  constraints: ["constraints", "constraint"],
  budget_left: ["budget_left", "budget", "remaining_budget"],
};

const PATH_FIELDS = ["artifacts", "files", "paths", "inputs", "outputs", "attachments"];
const PROSE_FIELDS = ["notes", "context", "body", "summary"];
const STANDARD_FIELDS = {
  goal: true,
  acceptance_criteria: true,
  acceptance: true,
  done_when: true,
  owner: true,
  next_owner: true,
  assignee: true,
  constraints: true,
  constraint: true,
  budget_left: true,
  budget: true,
  remaining_budget: true,
  questions: true,
  open_questions: true,
  artifacts: true,
  files: true,
  paths: true,
  inputs: true,
  outputs: true,
  attachments: true,
  notes: true,
  context: true,
  body: true,
  summary: true,
  title: true,
  from: true,
  source: true,
};

const VAGUE_TERMS = [
  "as needed",
  "as necessary",
  "as appropriate",
  "where appropriate",
  "if needed",
  "if necessary",
  "when possible",
  "and so on",
  "and others",
  "or similar",
  "good enough",
  "should work",
  "make it work",
  "properly",
  "appropriately",
  "correctly",
  "somehow",
  "whatever",
  "reasonable",
  "adequate",
  "etc",
];

const BLANK = {
  "": true,
  tbd: true,
  todo: true,
  fixme: true,
  "?": true,
  "???": true,
  unknown: true,
  "n/a": true,
  na: true,
  null: true,
  "-": true,
};
const OWNER_EMPTY = Object.assign({ none: true, nobody: true, unassigned: true, anyone: true }, BLANK);
const NON_ANSWERS = Object.assign({ none: true, no: true, unresolved: true }, BLANK);
const EXHAUSTED_WORDS = {
  "0": true,
  "0.0": true,
  "0.00": true,
  $0: true,
  "$0.00": true,
  "$0.0": true,
  none: true,
  exhausted: true,
  "no budget": true,
  over: true,
  "over budget": true,
  spent: true,
};
const EXHAUSTED_RE = /(?:\$?0+(?:\.0+)?|- ?\$?\d+(?:\.\d+)?|\$-\d+(?:\.\d+)?)(?:\s*(?:tokens?|usd|dollars?))?/;
const KEY_RE = /^[A-Za-z_][\w-]*$/;
const TODO_RE = /\b(TODO|TBD|FIXME)\b/gi;
const EXTS = "py|md|json|txt|html|css|js|yml|yaml|toml|csv|ini|cfg|xml|rst";
const BARE_PATH = new RegExp(
  "(?<![\\w./:])((?:[\\w.-]+/)+[\\w.-]+\\.[A-Za-z][A-Za-z0-9]{0,7}|(?<![\\w./:])[\\w.-]+\\.(?:" + EXTS + "))(?![\\w./])",
  "g"
);
const BACKTICK = /`([^`\n]+)`/g;
const SCHEMA_TYPES = ["string", "number", "integer", "boolean", "array", "object"];
const SCHEMA_KEYS = {
  required: true,
  properties: true,
  additionalProperties: true,
  description: true,
  title: true,
  $schema: true,
  $id: true,
};
const PROP_KEYS = { type: true, minLength: true, minItems: true, enum: true, description: true };

const MARKDOWN_TEMPLATE = "---\n" +
  "goal: \"\"\n" +
  "owner: \"\"\n" +
  "budget_left: \"\"\n" +
  "constraints: []\n" +
  "acceptance_criteria: []\n" +
  "questions: []\n" +
  "artifacts: []\n" +
  "---\n" +
  "\n" +
  "Write the handoff the next agent will receive.\n" +
  "\n" +
  "Put the goal, the owner of the next step, the budget left, the constraints,\n" +
  "and the acceptance criteria in the front matter above. Each criterion should\n" +
  "be a check someone could mark pass or fail.\n" +
  "\n" +
  "Leave questions empty once every question has an answer. List local files\n" +
  "under artifacts.\n";

const JSON_TEMPLATE = {
  goal: "",
  owner: "",
  budget_left: "",
  constraints: [],
  acceptance_criteria: [],
  questions: [],
  artifacts: [],
};

export class HandoffError extends Error {
  constructor(message) {
    super(message);
    this.name = "HandoffError";
  }
}

function isPlain(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function utf8Size(text) {
  return new TextEncoder().encode(String(text)).length;
}

function grouped(value) {
  return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function quote(text, limit) {
  const cap = limit == null ? 80 : limit;
  let flat = String(text == null ? "" : text).split(/\s+/).join(" ").replace(/"/g, "'").trim();
  if (flat.length > cap) flat = flat.slice(0, cap - 3).replace(/\s+$/, "") + "...";
  return '"' + flat + '"';
}

function problem(code, severity, message, field) {
  const body = { code: code, severity: severity, message: message };
  if (field) body.field = field;
  return body;
}

function missing(field, kind, hint) {
  const verb = kind === "missing" ? "is missing" : "is empty";
  return problem("missing-field", FIX, field + " " + verb + ". " + hint, field);
}

function isBlank(value) {
  if (value == null) return true;
  if (typeof value === "string") return !!BLANK[value.trim().toLowerCase()];
  if (Array.isArray(value) || isPlain(value)) return Object.keys(value).length === 0 || value.length === 0;
  return false;
}

function fieldValue(fields, canonical) {
  const names = ALIASES[canonical];
  for (let i = 0; i < names.length; i += 1) {
    if (Object.prototype.hasOwnProperty.call(fields, names[i])) return [names[i], fields[names[i]], true];
  }
  return [canonical, null, false];
}

function asMaxBytes(value) {
  if (typeof value === "boolean") throw new HandoffError("max_bytes must be an integer");
  let number = value;
  if (typeof number === "number" && !Number.isInteger(number)) {
    throw new HandoffError("max_bytes must be an integer");
  }
  if (typeof number !== "number" || !Number.isInteger(number)) throw new HandoffError("max_bytes must be an integer");
  if (number < 1 || number > HARD_CAP_BYTES) {
    throw new HandoffError("max_bytes must be from 1 to " + grouped(HARD_CAP_BYTES));
  }
  return number;
}

function summary(status, problems) {
  const count = problems.length;
  const noun = count === 1 ? "problem" : "problems";
  if (status === HANDOFF_READY) return "ready to hand off";
  if (status === HANDOFF_BLOCKED) return "blocked: " + count + " " + noun;
  return "needs fixes: " + count + " " + noun;
}

function decide(info) {
  const problems = info.problems;
  const status = problems.some(function (item) { return item.severity === BLOCK; })
    ? HANDOFF_BLOCKED
    : (problems.length ? HANDOFF_NEEDS_FIXES : HANDOFF_READY);
  return {
    status: status,
    ok_to_hand_off: status === HANDOFF_READY,
    summary: summary(status, problems),
    problems: problems,
    bytes: info.size,
    max_bytes: info.limit,
    root: info.root,
    format: info.format,
    schema: info.schemaLabel,
    paths: { found: info.found.slice(), missing: info.missing.slice() },
  };
}

function tooLarge(size, limit) {
  return problem(
    "too-large",
    BLOCK,
    "Payload is " + grouped(size) + " bytes. The cap is " + grouped(limit) + ". Move long text into files and cite the paths.",
    "payload"
  );
}

function checkTextField(canonical, value, hint) {
  if (typeof value !== "string") {
    return [problem("bad-shape", FIX, canonical + " must be text.", canonical)];
  }
  if (isBlank(value) || (canonical === "owner" && OWNER_EMPTY[value.trim().toLowerCase()])) {
    return [missing(canonical, "empty", hint)];
  }
  return [];
}

function unquotedColon(field, index, item) {
  if (!isPlain(item)) return null;
  const keys = Object.keys(item);
  if (keys.length !== 1) return null;
  const key = keys[0];
  const value = item[key];
  if (typeof key !== "string" || typeof value === "boolean" || (typeof value !== "string" && typeof value !== "number")) {
    return null;
  }
  const shown = (key + ": " + value).trim();
  return field + " item " + index + " has an unquoted colon, so it was read as a map (" + quote(shown) + "). Quote the whole item so the colon stays in the text.";
}

function nonemptyListProblems(problems, real, field, hint) {
  if (real !== 0) return problems;
  const specific = problems.filter(function (item) {
    return item.message.indexOf("unquoted colon") >= 0 || item.message.indexOf("is not text") >= 0;
  });
  if (specific.length) return specific;
  return [missing(field, "empty", hint)];
}

function checkCriteria(value, hint) {
  if (typeof value === "string") {
    if (isBlank(value)) return [missing("acceptance_criteria", "empty", hint)];
    return [];
  }
  if (!Array.isArray(value)) {
    return [problem("bad-shape", FIX, "acceptance_criteria must be text or a list of text.", "acceptance_criteria")];
  }
  if (!value.length) return [missing("acceptance_criteria", "empty", hint)];
  const problems = [];
  let real = 0;
  for (let i = 0; i < value.length; i += 1) {
    const item = value[i];
    const index = i + 1;
    if (typeof item === "string" && !isBlank(item)) {
      real += 1;
      continue;
    }
    const colon = unquotedColon("acceptance_criteria", index, item);
    if (colon) {
      problems.push(problem("bad-shape", FIX, colon, "acceptance_criteria"));
      continue;
    }
    if (item == null || (typeof item === "string" && isBlank(item))) {
      problems.push(problem(
        "bad-shape",
        FIX,
        "acceptance_criteria item " + index + " is empty. Remove it or write a concrete check.",
        "acceptance_criteria"
      ));
    } else {
      problems.push(problem("bad-shape", FIX, "acceptance_criteria item " + index + " is not text.", "acceptance_criteria"));
    }
  }
  return nonemptyListProblems(problems, real, "acceptance_criteria", hint);
}

function checkConstraints(value, hint) {
  if (typeof value === "string") {
    if (isBlank(value)) return [missing("constraints", "empty", hint)];
    return [];
  }
  if (!Array.isArray(value)) {
    return [problem("bad-shape", FIX, "constraints must be text or a list of text.", "constraints")];
  }
  if (!value.length) return [missing("constraints", "empty", hint)];
  const problems = [];
  let real = 0;
  for (let i = 0; i < value.length; i += 1) {
    const item = value[i];
    const index = i + 1;
    if (typeof item === "string" && !isBlank(item)) {
      real += 1;
      continue;
    }
    const colon = unquotedColon("constraints", index, item);
    if (colon) {
      problems.push(problem("bad-shape", FIX, colon, "constraints"));
      continue;
    }
    if (typeof item === "string" || item == null) {
      problems.push(problem(
        "bad-shape",
        FIX,
        "constraints item " + index + " is empty. Remove it or write the limit.",
        "constraints"
      ));
    } else {
      problems.push(problem("bad-shape", FIX, "constraints item " + index + " is not text.", "constraints"));
    }
  }
  return nonemptyListProblems(problems, real, "constraints", hint);
}

function requiredProblems(fields) {
  const problems = [];
  for (let i = 0; i < REQUIRED_FIELDS.length; i += 1) {
    const canonical = REQUIRED_FIELDS[i][0];
    const hint = REQUIRED_FIELDS[i][1];
    const found = fieldValue(fields, canonical);
    if (!found[2]) {
      problems.push(missing(canonical, "missing", hint));
      continue;
    }
    if (canonical === "acceptance_criteria") {
      checkCriteria(found[1], hint).forEach(function (item) { problems.push(item); });
      continue;
    }
    if (canonical === "constraints") {
      checkConstraints(found[1], hint).forEach(function (item) { problems.push(item); });
      continue;
    }
    if (canonical === "goal" || canonical === "owner") {
      checkTextField(canonical, found[1], hint).forEach(function (item) { problems.push(item); });
      continue;
    }
    if (isBlank(found[1])) problems.push(missing(canonical, "empty", hint));
  }
  return problems;
}

function criteriaTexts(fields) {
  const found = fieldValue(fields, "acceptance_criteria");
  if (!found[2]) return [];
  if (typeof found[1] === "string") return isBlank(found[1]) ? [] : [found[1]];
  if (Array.isArray(found[1])) {
    return found[1].filter(function (item) { return typeof item === "string" && !isBlank(item); });
  }
  return [];
}

function findVague(text) {
  const lowered = String(text).toLowerCase();
  let bestAt = null;
  let bestTerm = null;
  for (let i = 0; i < VAGUE_TERMS.length; i += 1) {
    const term = VAGUE_TERMS[i];
    const pattern = term === "etc"
      ? /\betc\.?(?!\w)/
      : new RegExp("\\b" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b");
    const match = pattern.exec(lowered);
    if (!match) continue;
    const longer = bestTerm != null && term.length > bestTerm.length;
    if (bestAt == null || match.index < bestAt || (match.index === bestAt && longer)) {
      bestAt = match.index;
      bestTerm = term;
    }
  }
  return bestTerm;
}

function vagueProblems(fields) {
  const problems = [];
  criteriaTexts(fields).forEach(function (item) {
    const term = findVague(item);
    if (!term) return;
    let follow = "Replace it with a check someone could mark pass or fail.";
    if (term === "etc") follow = "List each item instead of stopping at \"etc\".";
    else if (term === "as needed") follow = "Name the case you mean instead of \"as needed\".";
    problems.push(problem(
      "vague-criterion",
      FIX,
      "Acceptance criterion " + quote(item) + " contains \"" + term + "\". " + follow,
      "acceptance_criteria"
    ));
  });
  return problems;
}

function isUrl(value) {
  const text = String(value);
  if (text.indexOf("://") >= 0) return true;
  return text.toLowerCase().indexOf("mailto:") === 0;
}

function looksLikePath(value) {
  if (!value || /\s/.test(value)) return false;
  if (isUrl(value)) return false;
  if (value.indexOf("/") >= 0 || value.indexOf("\\") >= 0 || value.charAt(0) === ".") return true;
  return new RegExp("^[\\w.-]+\\.(?:" + EXTS + ")$").test(value);
}

function usableCitation(value) {
  if (!value || value === "." || value === ".." || value === "/" || value === "./") return false;
  if (value.length > 240 || isUrl(value)) return false;
  return looksLikePath(value);
}

function citeKey(value) {
  let text = String(value).trim().replace(/^`+|`+$/g, "").replace(/\\/g, "/");
  while (text.indexOf("./") === 0) text = text.slice(2);
  return text;
}

function explicitPaths(fields) {
  const citations = [];
  const problems = [];
  PATH_FIELDS.forEach(function (field) {
    if (!Object.prototype.hasOwnProperty.call(fields, field)) return;
    const value = fields[field];
    if (value == null || value === "" || (Array.isArray(value) && value.length === 0)) return;
    let entries;
    if (typeof value === "string") entries = [value];
    else if (Array.isArray(value)) entries = value.slice();
    else {
      problems.push(problem("bad-shape", FIX, field + " must be a list of paths.", field));
      return;
    }
    entries.forEach(function (item, index) {
      const number = index + 1;
      if (typeof item !== "string") {
        problems.push(problem("bad-shape", FIX, field + " item " + number + " is not text.", field));
        return;
      }
      const text = item.trim();
      if (isBlank(text)) {
        problems.push(problem("not-a-path", FIX, field + " item " + number + " is empty. Remove it or cite a path.", field));
        return;
      }
      if (isUrl(text)) return;
      if (!looksLikePath(text)) {
        problems.push(problem(
          "not-a-path",
          FIX,
          field + " item " + number + " (" + quote(text) + ") is not a local file path. Cite a path or remove it.",
          field
        ));
        return;
      }
      citations.push(text);
    });
  });
  return [citations, problems];
}

function regexPaths(text) {
  const found = [];
  const source = String(text || "");
  BARE_PATH.lastIndex = 0;
  let match = BARE_PATH.exec(source);
  while (match) {
    found.push(match[1]);
    match = BARE_PATH.exec(source);
  }
  BACKTICK.lastIndex = 0;
  match = BACKTICK.exec(source);
  while (match) {
    const span = match[1].trim();
    if (looksLikePath(span) && !isUrl(span)) found.push(span);
    match = BACKTICK.exec(source);
  }
  return found;
}

function dedupePaths(citations) {
  const ordered = [];
  const seen = {};
  citations.forEach(function (cited) {
    const text = String(cited).trim().replace(/^`+|`+$/g, "");
    if (!usableCitation(text)) return;
    const key = citeKey(text);
    if (seen[key]) return;
    seen[key] = true;
    ordered.push(text);
  });
  return ordered;
}

function pathExists(cited, files) {
  if (!files || !isPlain(files)) return false;
  const key = citeKey(cited);
  if (key.split("/").indexOf("..") >= 0) return false;
  return Object.prototype.hasOwnProperty.call(files, key);
}

function missingPathMessage(cited) {
  const shown = cited.indexOf(" ") < 0 ? cited : '"' + cited.replace(/"/g, "'") + '"';
  return shown + " does not exist. Create it, or remove the citation.";
}

function classifyPaths(citations, files) {
  const found = [];
  const missingPaths = [];
  citations.forEach(function (cited) {
    if (pathExists(cited, files)) found.push(cited);
    else missingPaths.push(cited);
  });
  return [found, missingPaths];
}

function normQuestion(text) {
  return String(text).toLowerCase().replace(/\?/g, " ").split(/\s+/).filter(Boolean).join(" ");
}

function bodyQuestions(body) {
  const found = [];
  String(body || "").split(/\r\n|\n|\r/).forEach(function (line) {
    let stripped = line.trim();
    if (!stripped || stripped.indexOf("?") < 0) return;
    if (stripped.charAt(0) === "#") stripped = stripped.replace(/^#+\s*/, "").trim();
    const without = stripped.replace(/\b(?:https?|mailto):\S+/gi, "");
    if (without.indexOf("?") < 0) return;
    found.push(stripped);
  });
  return found;
}

function isResolved(item) {
  let answer = "";
  ["answer", "a", "resolution"].forEach(function (key) {
    if (answer) return;
    const value = item[key];
    if (typeof value === "string" && value.trim()) answer = value.trim();
  });
  if (answer && !NON_ANSWERS[answer.toLowerCase()]) return true;
  if (item.resolved === true && (!answer || !NON_ANSWERS[answer.toLowerCase()])) return true;
  return false;
}

function questionProblems(fields, body) {
  const problems = [];
  const pending = [];
  const answered = {};
  let raw = null;
  let fieldName = "questions";
  ["questions", "open_questions"].forEach(function (name) {
    if (raw == null && Object.prototype.hasOwnProperty.call(fields, name)) {
      raw = fields[name];
      fieldName = name;
    }
  });
  if (typeof raw === "string") {
    const lowered = raw.trim().toLowerCase();
    if (["none", "no", "no questions"].indexOf(lowered) < 0 && !isBlank(raw)) pending.push(raw.trim());
  } else if (Array.isArray(raw)) {
    raw.forEach(function (item) {
      if (typeof item === "string") {
        if (item.trim() && !isBlank(item)) pending.push(item.trim());
        return;
      }
      if (!isPlain(item)) {
        problems.push(problem("bad-shape", FIX, fieldName + " entries must be text or objects with a question.", fieldName));
        return;
      }
      let question = "";
      ["question", "q", "text"].forEach(function (key) {
        if (question) return;
        const value = item[key];
        if (typeof value === "string" && value.trim()) question = value.trim();
      });
      if (!question) {
        problems.push(problem("bad-shape", FIX, fieldName + " has an entry with no question text.", fieldName));
        return;
      }
      if (isResolved(item)) answered[normQuestion(question)] = true;
      else pending.push(question);
    });
  } else if (raw != null && !isBlank(raw)) {
    problems.push(problem("bad-shape", FIX, fieldName + " must be a list of questions.", fieldName));
  }
  const known = {};
  pending.forEach(function (item) { known[normQuestion(item)] = true; });
  Object.keys(answered).forEach(function (key) { known[key] = true; });
  bodyQuestions(body).forEach(function (line) {
    if (!known[normQuestion(line)]) pending.push(line);
  });
  const seen = {};
  pending.forEach(function (question) {
    const key = normQuestion(question);
    if (!key || seen[key] || answered[key]) return;
    seen[key] = true;
    problems.push(problem(
      "unresolved-question",
      FIX,
      "Unresolved question: " + quote(question) + " Answer it, or remove it.",
      raw != null ? fieldName : "body"
    ));
  });
  return problems;
}

function walkStrings(value) {
  if (typeof value === "string") return [value];
  const found = [];
  if (Array.isArray(value)) {
    value.forEach(function (item) { walkStrings(item).forEach(function (text) { found.push(text); }); });
    return found;
  }
  if (isPlain(value)) {
    Object.keys(value).forEach(function (key) {
      walkStrings(value[key]).forEach(function (text) { found.push(text); });
    });
  }
  return found;
}

function prose(fields, body) {
  const parts = [];
  if (body) parts.push(body);
  PROSE_FIELDS.forEach(function (name) {
    const value = fields[name];
    if (typeof value === "string" && parts.indexOf(value) < 0) parts.push(value);
  });
  return parts.join("\n");
}

function todoSnippet(text, index, length) {
  const lineStart = index <= 0 ? 0 : text.lastIndexOf("\n", index - 1) + 1;
  let lineEnd = text.indexOf("\n", index + length);
  if (lineEnd < 0) lineEnd = text.length;
  const raw = text.slice(lineStart, lineEnd);
  const flat = raw.trim().split(/\s+/).filter(Boolean).join(" ");
  if (flat.length <= 120) return flat;
  const relStart = index - lineStart;
  const relEnd = relStart + length;
  const pad = 40;
  const start = Math.max(0, relStart - pad);
  const end = Math.min(raw.length, relEnd + pad);
  let chunk = raw.slice(start, end).trim().split(/\s+/).filter(Boolean).join(" ");
  if (start > 0) chunk = "..." + chunk;
  if (end < raw.length) chunk = chunk + "...";
  if (chunk.length > 120) chunk = chunk.slice(0, 117).replace(/\s+$/, "") + "...";
  return chunk;
}

function todoProblems(fields, body) {
  const problems = [];
  const sources = [];
  if (body) sources.push(["body", body]);
  Object.keys(fields).forEach(function (key) {
    if (key === "body") return;
    walkStrings(fields[key]).forEach(function (text) {
      if (text) sources.push([key, text]);
    });
  });
  sources.forEach(function (pair) {
    const seen = {};
    const text = pair[1];
    TODO_RE.lastIndex = 0;
    let match = TODO_RE.exec(text);
    while (match) {
      const snippet = todoSnippet(text, match.index, match[0].length);
      if (!seen[snippet.toLowerCase()]) {
        seen[snippet.toLowerCase()] = true;
        const label = match[1].toUpperCase();
        problems.push(problem("todo-left", FIX, label + " left in: \"" + snippet + "\" Finish it or delete it.", pair[0]));
      }
      match = TODO_RE.exec(text);
    }
  });
  return problems;
}

function budgetLeaf(value) {
  if (value == null || value === "" || (Array.isArray(value) && value.length === 0) || (isPlain(value) && Object.keys(value).length === 0)) {
    return "skip";
  }
  if (typeof value === "boolean") return "bad";
  if (typeof value === "number") return value <= 0 ? "exhausted" : "ok";
  if (typeof value === "string") {
    const text = value.trim().toLowerCase().split(/\s+/).join(" ");
    if (!text) return "skip";
    if (EXHAUSTED_WORDS[text] || new RegExp("^(?:" + EXHAUSTED_RE.source + ")$").test(text)) {
      return "exhausted";
    }
    return "ok";
  }
  return "bad";
}

function formatNumber(value) {
  if (typeof value === "number" && Number.isInteger(value)) return String(value);
  return String(value);
}

function budgetState(value) {
  if (isPlain(value)) {
    const keys = Object.keys(value);
    if (!keys.length) return ["bad", ""];
    let sawAmount = false;
    let sawPositive = false;
    for (let i = 0; i < keys.length; i += 1) {
      const leaf = budgetLeaf(value[keys[i]]);
      if (leaf === "bad") return ["bad", ""];
      if (leaf === "skip") continue;
      sawAmount = true;
      if (leaf === "ok") sawPositive = true;
    }
    if (!sawAmount) return ["bad", ""];
    if (!sawPositive) return ["exhausted", JSON.stringify(value)];
    return ["ok", ""];
  }
  const leaf = budgetLeaf(value);
  if (leaf === "ok") return ["ok", ""];
  if (leaf === "skip") return ["bad", ""];
  if (leaf === "exhausted") {
    const shown = typeof value === "string" ? value.trim() : formatNumber(value);
    return ["exhausted", shown];
  }
  return ["bad", ""];
}

function budgetProblem(fields) {
  const found = fieldValue(fields, "budget_left");
  if (!found[2] || isBlank(found[1])) return null;
  const state = budgetState(found[1]);
  if (state[0] === "ok") return null;
  if (state[0] === "bad") {
    return problem(
      "bad-shape",
      FIX,
      found[0] + " must be a number, a short string such as \"4000 tokens\", or an object of amounts.",
      found[0]
    );
  }
  return problem(
    "budget-exhausted",
    BLOCK,
    "budget_left is exhausted (" + state[1] + "). The next owner has nothing to spend. Raise it, or do not hand this off.",
    found[0]
  );
}

function typeOk(value, expected) {
  if (expected === "string") return typeof value === "string";
  if (expected === "boolean") return typeof value === "boolean";
  if (expected === "integer") return typeof value === "number" && Number.isInteger(value);
  if (expected === "number") return typeof value === "number" && Number.isFinite(value);
  if (expected === "array") return Array.isArray(value);
  if (expected === "object") return isPlain(value);
  return true;
}

function typeName(value) {
  if (typeof value === "boolean") return "a boolean";
  if (typeof value === "number" && Number.isInteger(value)) return "an integer";
  if (typeof value === "number") return "a number";
  if (typeof value === "string") return "a string";
  if (Array.isArray(value)) return "an array";
  if (isPlain(value)) return "an object";
  if (value == null) return "null";
  return typeof value;
}

function schemaInt(value, label) {
  if (typeof value === "boolean" || typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new HandoffError(label + " must be an integer >= 0");
  }
}

function validateSchema(data, label) {
  if (!isPlain(data)) throw new HandoffError(label + " must be a JSON object");
  const unknown = Object.keys(data).filter(function (key) { return !SCHEMA_KEYS[key]; }).sort();
  if (unknown.length) {
    throw new HandoffError(label + " key '" + unknown[0] + "' is not supported. Use required, properties, and additionalProperties.");
  }
  const required = data.required == null ? [] : data.required;
  if (!Array.isArray(required) || required.some(function (item) { return typeof item !== "string" || !item.trim(); })) {
    throw new HandoffError(label + " required must be an array of field names");
  }
  const properties = data.properties == null ? {} : data.properties;
  if (!isPlain(properties)) throw new HandoffError(label + " properties must be an object");
  Object.keys(properties).forEach(function (name) {
    if (!name.trim()) throw new HandoffError(label + " properties must be named with strings");
    const spec = properties[name];
    if (!isPlain(spec)) throw new HandoffError(label + " properties." + name + " must be an object");
    const extra = Object.keys(spec).filter(function (key) { return !PROP_KEYS[key]; }).sort();
    if (extra.length) {
      throw new HandoffError(
        label + " properties." + name + "." + extra[0] + " is not supported. Use type, minLength, minItems, enum, or description."
      );
    }
    if (spec.type != null && SCHEMA_TYPES.indexOf(spec.type) < 0) {
      throw new HandoffError(label + " properties." + name + ".type must be one of: " + SCHEMA_TYPES.join(", "));
    }
    if (spec.minLength != null) schemaInt(spec.minLength, label + " properties." + name + ".minLength");
    if (spec.minItems != null) schemaInt(spec.minItems, label + " properties." + name + ".minItems");
    if (spec.enum != null && (!Array.isArray(spec.enum) || !spec.enum.length)) {
      throw new HandoffError(label + " properties." + name + ".enum must be a non-empty array");
    }
  });
  if (data.additionalProperties != null && typeof data.additionalProperties !== "boolean") {
    throw new HandoffError(label + " additionalProperties must be a boolean");
  }
}

function checkProperty(name, value, spec) {
  const problems = [];
  if (typeof spec.type === "string" && !typeOk(value, spec.type)) {
    problems.push(problem("schema", FIX, name + " must be a " + spec.type + ". The value is " + typeName(value) + ".", name));
    return problems;
  }
  if (spec.enum && spec.enum.indexOf(value) < 0) {
    problems.push(problem("schema", FIX, name + " must be one of: " + spec.enum.join(", ") + ".", name));
  }
  if (spec.minLength != null && typeof value === "string" && value.length < spec.minLength) {
    problems.push(problem("schema", FIX, name + " must be at least " + spec.minLength + " characters.", name));
  }
  if (spec.minItems != null && Array.isArray(value) && value.length < spec.minItems) {
    problems.push(problem("schema", FIX, name + " must have at least " + spec.minItems + " items.", name));
  }
  return problems;
}

function schemaProblems(fields, schema) {
  const problems = [];
  const builtin = {};
  REQUIRED_FIELDS.forEach(function (pair) { builtin[pair[0]] = true; });
  const aliasNames = {};
  Object.keys(ALIASES).forEach(function (key) {
    ALIASES[key].forEach(function (name) { aliasNames[name] = true; });
  });
  (schema.required || []).forEach(function (name) {
    if (builtin[name] || aliasNames[name]) return;
    if (!Object.prototype.hasOwnProperty.call(fields, name)) {
      problems.push(problem("schema", FIX, name + " is missing. The schema requires it.", name));
      return;
    }
    if (isBlank(fields[name])) problems.push(problem("schema", FIX, name + " is empty. The schema requires it.", name));
  });
  const properties = schema.properties || {};
  if (isPlain(properties)) {
    Object.keys(properties).forEach(function (name) {
      if (!Object.prototype.hasOwnProperty.call(fields, name) || isBlank(fields[name])) return;
      checkProperty(name, fields[name], properties[name]).forEach(function (item) { problems.push(item); });
    });
  }
  if (schema.additionalProperties === false) {
    Object.keys(fields).sort().forEach(function (name) {
      if (properties[name] || STANDARD_FIELDS[name]) return;
      problems.push(problem(
        "schema",
        FIX,
        "Unexpected field \"" + name + "\". The schema does not allow it. Remove it, or add it to properties.",
        name
      ));
    });
  }
  return problems;
}

function splitLines(text) {
  return String(text).split(/\r\n|\n|\r/);
}

function unquote(text) {
  const quoteChar = text.charAt(0);
  const inner = text.slice(1, -1);
  if (quoteChar === '"') return inner.replace(/\\"/g, '"').replace(/\\n/g, "\n").replace(/\\\\/g, "\\");
  return inner.replace(/\\'/g, "'").replace(/\\\\/g, "\\");
}

function scalar(text, line) {
  if (text === "") return "";
  if (text.charAt(0) === '"' || text.charAt(0) === "'") {
    if (text.length < 2 || text.charAt(text.length - 1) !== text.charAt(0)) {
      const where = line ? "line " + line : "value";
      throw new HandoffError("front matter " + where + " has an unclosed quote");
    }
    return unquote(text);
  }
  if (text.charAt(0) === "[" || text.charAt(0) === "{") {
    try {
      return JSON.parse(text);
    } catch (err) {
      const where = line ? "line " + line : "value";
      throw new HandoffError("front matter " + where + " looks like JSON but is not valid: " + (err.message || "invalid"));
    }
  }
  const lowered = text.toLowerCase();
  if (lowered === "true") return true;
  if (lowered === "false") return false;
  if (lowered === "null" || lowered === "~") return null;
  if (/^-?\d+$/.test(text)) return Number(text);
  if (/^-?\d+\.\d+$/.test(text)) return Number(text);
  return text;
}

function parseFrontMatter(block) {
  const lines = splitLines(block);
  const result = {};
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim() || line.trim().charAt(0) === "#") {
      index += 1;
      continue;
    }
    if (line.charAt(0) === " " || line.charAt(0) === "\t") {
      throw new HandoffError("front matter line " + (index + 1) + " is indented without a parent key");
    }
    const sep = line.indexOf(":");
    const key = (sep < 0 ? line : line.slice(0, sep)).trim();
    if (sep < 0 || !KEY_RE.test(key)) throw new HandoffError("front matter line " + (index + 1) + " is not a key: value pair");
    if (Object.prototype.hasOwnProperty.call(result, key)) throw new HandoffError("front matter key '" + key + "' is repeated");
    const rest = line.slice(sep + 1).trim();
    if (rest !== "") {
      result[key] = scalar(rest, index + 1);
      index += 1;
      continue;
    }
    index += 1;
    const child = [];
    while (index < lines.length && (!lines[index].trim() || lines[index].charAt(0) === " " || lines[index].charAt(0) === "\t")) {
      child.push(lines[index]);
      index += 1;
    }
    while (child.length && !child[0].trim()) child.shift();
    while (child.length && !child[child.length - 1].trim()) child.pop();
    result[key] = child.length ? parseIndented(child) : "";
  }
  return result;
}

function dedent(lines) {
  const indents = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.trim()) continue;
    if (line.charAt(0) === "\t") throw new HandoffError("front matter indentation must be spaces, not tabs");
    indents.push(line.length - line.trimStart().length);
  }
  if (!indents.length) return [];
  const cut = Math.min.apply(null, indents);
  if (cut < 1) throw new HandoffError("front matter block is not indented");
  return lines.map(function (line) {
    if (!line.trim()) return "";
    const width = line.length - line.trimStart().length;
    if (width < cut) throw new HandoffError("front matter indentation is inconsistent");
    return line.slice(cut);
  });
}

function parseList(lines) {
  const items = [];
  let mapLines = null;
  function flush() {
    if (mapLines != null) {
      items.push(parseFrontMatter(mapLines.join("\n")));
      mapLines = null;
    }
  }
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.trim() || line.trim().charAt(0) === "#") continue;
    if (line.indexOf("- ") === 0) {
      flush();
      const rest = line.slice(2).trim();
      const sep = rest.indexOf(":");
      const key = sep < 0 ? rest : rest.slice(0, sep).trim();
      if (sep >= 0 && KEY_RE.test(key) && rest.charAt(0) !== '"' && rest.charAt(0) !== "'") {
        mapLines = [key + ":" + rest.slice(sep + 1)];
        continue;
      }
      items.push(scalar(rest, 0));
      continue;
    }
    if (mapLines != null && (line.charAt(0) === " " || line.charAt(0) === "\t")) {
      mapLines.push(line.trim());
      continue;
    }
    throw new HandoffError("front matter list item is not valid");
  }
  flush();
  return items;
}

function parseIndented(lines) {
  const dedented = dedent(lines);
  const content = dedented.filter(function (line) { return line.trim() && line.trim().charAt(0) !== "#"; });
  if (!content.length) return "";
  if (content[0].indexOf("- ") === 0) return parseList(dedented);
  return parseFrontMatter(dedented.join("\n"));
}

function parseMarkdown(text) {
  const lines = splitLines(text);
  if (!lines.length || lines[0].trim() !== "---") throw new HandoffError("front matter must open with a line that is just ---");
  let close = -1;
  for (let i = 1; i < lines.length; i += 1) {
    if (lines[i].trim() === "---") {
      close = i;
      break;
    }
  }
  if (close < 0) throw new HandoffError("front matter is missing the closing ---");
  return {
    fields: parseFrontMatter(lines.slice(1, close).join("\n")),
    body: lines.slice(close + 1).join("\n"),
  };
}

export function parseHandoff(text) {
  if (typeof text !== "string") throw new HandoffError("handoff text must be a string");
  const raw = text.replace(/^\uFEFF/, "");
  const leading = raw.trimStart();
  if (!leading) throw new HandoffError("handoff is empty");
  if (leading.charAt(0) === "{") {
    let data;
    try {
      data = JSON.parse(leading);
    } catch (err) {
      throw new HandoffError("handoff JSON is not valid: " + (err.message || "invalid"));
    }
    if (!isPlain(data)) throw new HandoffError("handoff JSON must be an object");
    return { fields: data, body: typeof data.body === "string" ? data.body : "" };
  }
  if (leading.indexOf("---") === 0) return parseMarkdown(leading);
  throw new HandoffError("handoff must be a JSON object or Markdown that starts with --- front matter");
}

export function renderTemplate(fmt) {
  const name = typeof fmt === "string" ? fmt.trim().toLowerCase() : "";
  if (name === "md" || name === "markdown") return MARKDOWN_TEMPLATE;
  if (name === "json") return JSON.stringify(JSON_TEMPLATE, null, 2) + "\n";
  throw new HandoffError("format must be markdown or json");
}

function stringifyFields(fields, body) {
  return [body].concat(walkStrings(fields)).join("\n");
}

function isDocument(value) {
  const leading = String(value).replace(/^\uFEFF/, "").trimStart();
  return !leading || value.indexOf("\n") >= 0 || leading.charAt(0) === "{" || leading.indexOf("---") === 0;
}

function loadDocument(handoff) {
  if (isPlain(handoff)) {
    const raw = JSON.stringify(handoff);
    const size = utf8Size(raw);
    if (size > HARD_CAP_BYTES) return { fields: null, body: "", raw: null, size: size, format: "dict", unparsed: true };
    return {
      fields: handoff,
      body: typeof handoff.body === "string" ? handoff.body : "",
      raw: null,
      size: size,
      format: "dict",
      unparsed: false,
    };
  }
  if (typeof handoff !== "string") throw new HandoffError("handoff must be a dict, a path, or document text");
  if (!isDocument(handoff)) {
    throw new HandoffError("pass the handoff text. This page cannot read a file path (" + handoff.trim() + ").");
  }
  const size = utf8Size(handoff);
  if (size > HARD_CAP_BYTES) return { fields: null, body: "", raw: null, size: size, format: "unparsed", unparsed: true };
  const parsed = parseHandoff(handoff);
  const leading = handoff.replace(/^\uFEFF/, "").trimStart();
  return {
    fields: parsed.fields,
    body: parsed.body,
    raw: handoff,
    size: size,
    format: leading.charAt(0) === "{" ? "json" : "markdown",
    unparsed: false,
  };
}

export function checkHandoff(handoff, options) {
  const opts = options || {};
  const limit = opts.maxBytes == null ? DEFAULT_MAX_BYTES : asMaxBytes(opts.maxBytes);
  const root = opts.root || "browser";
  let schema = null;
  let schemaLabel = null;
  if (opts.schema != null) {
    if (!isPlain(opts.schema)) throw new HandoffError("schema must be a JSON object");
    validateSchema(opts.schema, "schema");
    schema = opts.schema;
    schemaLabel = "inline";
  }
  const loaded = loadDocument(handoff);
  if (loaded.unparsed || !loaded.fields) {
    return decide({
      problems: [tooLarge(loaded.size, limit)],
      size: loaded.size,
      limit: limit,
      root: root,
      format: loaded.format,
      schemaLabel: schemaLabel,
      found: [],
      missing: [],
    });
  }
  const fields = loaded.fields;
  const problems = [];
  requiredProblems(fields).forEach(function (item) { problems.push(item); });
  if (schema) schemaProblems(fields, schema).forEach(function (item) { problems.push(item); });
  vagueProblems(fields).forEach(function (item) { problems.push(item); });
  const explicit = explicitPaths(fields);
  explicit[1].forEach(function (item) { problems.push(item); });
  const scan = loaded.raw != null ? loaded.raw : stringifyFields(fields, loaded.body);
  const citations = explicit[0].concat(regexPaths(scan));
  const ordered = dedupePaths(citations);
  const classified = classifyPaths(ordered, opts.files);
  classified[1].forEach(function (cited) {
    problems.push(problem("missing-path", FIX, missingPathMessage(cited), "paths"));
  });
  questionProblems(fields, prose(fields, loaded.body)).forEach(function (item) { problems.push(item); });
  todoProblems(fields, loaded.body).forEach(function (item) { problems.push(item); });
  const budget = budgetProblem(fields);
  if (budget) problems.push(budget);
  if (loaded.size > limit) problems.push(tooLarge(loaded.size, limit));
  return decide({
    problems: problems,
    size: loaded.size,
    limit: limit,
    root: root,
    format: loaded.format,
    schemaLabel: schemaLabel,
    found: classified[0],
    missing: classified[1],
  });
}
