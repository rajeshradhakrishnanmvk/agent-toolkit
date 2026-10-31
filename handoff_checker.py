#!/usr/bin/env python3
"""Handoff checker for AI agents.

When one agent (or one step) hands work to the next, the payload often
forgets the goal, the acceptance criteria, a file it cites, an open
question, the constraints, the budget left, or who owns the next step.

check() reads a handoff — a JSON object, or Markdown with front matter —
and answers:

  ready         every check passed; send it
  needs-fixes   a punch list of specific edits; do not send it yet
  blocked       do not send it; the budget is exhausted or the payload
                is over the size cap. The punch list is still included.

The checker does not send the handoff and does not rewrite it. A harness
should refuse the handoff unless the status is ready.

Built-in required fields: goal, acceptance_criteria, owner, constraints,
budget_left. A JSON schema file can require more. It cannot drop these.

No third-party dependencies. Python 3.9+. No network calls.

  python3 handoff_checker.py template > handoff.md
  python3 handoff_checker.py check handoff.md
  python3 handoff_checker.py explain
  python3 handoff_checker.py serve
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

VERSION = "0.1.0"
READY = "ready"
NEEDS_FIXES = "needs-fixes"
BLOCKED = "blocked"

SEVERITY_FIX = "fix"
SEVERITY_BLOCK = "block"

DEFAULT_MAX_BYTES = 16 * 1024
HARD_CAP_BYTES = 2 * 1024 * 1024

# canonical name, hint appended to the problem message
REQUIRED_FIELDS = (
    ("goal", "Say what the next owner should finish."),
    ("acceptance_criteria", "Add at least one concrete check for done."),
    ("owner", "Name who owns the next step."),
    ("constraints", "List the limits the next owner must not cross."),
    ("budget_left", "Say what is left to spend (tokens, dollars, or tool calls)."),
)

_ALIASES = {
    "goal": ("goal",),
    "acceptance_criteria": ("acceptance_criteria", "acceptance", "done_when"),
    "owner": ("owner", "next_owner", "assignee"),
    "constraints": ("constraints", "constraint"),
    "budget_left": ("budget_left", "budget", "remaining_budget"),
}

_PATH_FIELDS = ("artifacts", "files", "paths", "inputs", "outputs", "attachments")
_PROSE_FIELDS = ("notes", "context", "body", "summary")

_STANDARD_FIELDS = {
    "goal",
    "acceptance_criteria",
    "acceptance",
    "done_when",
    "owner",
    "next_owner",
    "assignee",
    "constraints",
    "constraint",
    "budget_left",
    "budget",
    "remaining_budget",
    "questions",
    "open_questions",
    "artifacts",
    "files",
    "paths",
    "inputs",
    "outputs",
    "attachments",
    "notes",
    "context",
    "body",
    "summary",
    "title",
    "from",
    "source",
}

# Whole words and phrases, case-insensitive. "etc" also matches "etc."
VAGUE_TERMS = (
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
)

_BLANK = {
    "",
    "tbd",
    "todo",
    "fixme",
    "?",
    "???",
    "unknown",
    "n/a",
    "na",
    "null",
    "-",
}
_OWNER_EMPTY = _BLANK | {"none", "nobody", "unassigned", "anyone"}
_NON_ANSWERS = _BLANK | {"none", "no", "unresolved"}
_EXHAUSTED_WORDS = {
    "0",
    "0.0",
    "0.00",
    "$0",
    "$0.00",
    "$0.0",
    "none",
    "exhausted",
    "no budget",
    "over",
    "over budget",
    "spent",
}
_EXHAUSTED_RE = re.compile(
    r"\$?0+(?:\.0+)?(?:\s*(?:tokens?|usd|dollars?))?"
)

_KEY = re.compile(r"^[A-Za-z_][\w-]*$")
_TODO_RE = re.compile(r"\b(TODO|TBD|FIXME)\b", re.IGNORECASE)
_EXTS = "py|md|json|txt|html|css|js|yml|yaml|toml|csv|ini|cfg|xml|rst"
_BARE_PATH = re.compile(
    rf"(?<![\w./:])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{{0,7}}"
    rf"|(?<![\w./:])[\w.-]+\.(?:{_EXTS}))(?![\w./])"
)
_BACKTICK = re.compile(r"`([^`\n]+)`")

_SCHEMA_TYPES = ("string", "number", "integer", "boolean", "array", "object")
_SCHEMA_KEYS = {
    "required",
    "properties",
    "additionalProperties",
    "description",
    "title",
    "$schema",
    "$id",
}
_PROP_KEYS = {"type", "minLength", "minItems", "enum", "description"}

_MARKDOWN_TEMPLATE = """\
---
goal: ""
owner: ""
budget_left: ""
constraints: []
acceptance_criteria: []
questions: []
artifacts: []
---

Write the handoff the next agent will receive.

Put the goal, the owner of the next step, the budget left, the constraints,
and the acceptance criteria in the front matter above. Each criterion should
be a check someone could mark pass or fail.

Leave questions empty once every question has an answer. List local files
under artifacts.
"""

_JSON_TEMPLATE = {
    "goal": "",
    "owner": "",
    "budget_left": "",
    "constraints": [],
    "acceptance_criteria": [],
    "questions": [],
    "artifacts": [],
}


class HandoffError(Exception):
    """A handoff or a schema the caller can fix: bad input, or a missing file."""


class _Loaded(NamedTuple):
    fields: dict[str, Any] | None
    body: str
    raw: str | None
    size: int
    fmt: str
    unparsed: bool


class _Defaults(NamedTuple):
    root: str | None
    schema: str | None
    max_bytes: int | None


def parse_handoff(text: str) -> dict[str, Any]:
    """Parse handoff text into ``{"fields": ..., "body": ...}``.

    JSON objects and Markdown that opens with a ``---`` front-matter fence
    are accepted. The front matter is a small subset: scalars, lists, and
    one level of maps. Quote a list item that contains a colon.
    """
    if not isinstance(text, str):
        raise HandoffError("handoff text must be a string")
    raw = text.lstrip("\ufeff")
    leading = raw.lstrip()
    if not leading:
        raise HandoffError("handoff is empty")
    if leading.startswith("{"):
        try:
            data = json.loads(leading)
        except json.JSONDecodeError as exc:
            raise HandoffError(f"handoff JSON is not valid: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise HandoffError("handoff JSON must be an object")
        body = data.get("body") if isinstance(data.get("body"), str) else ""
        return {"fields": data, "body": body}
    if leading.startswith("---"):
        return _parse_markdown(leading)
    raise HandoffError(
        "handoff must be a JSON object or Markdown that starts with --- front matter"
    )


def render_template(fmt: str = "markdown") -> str:
    """Return a blank handoff. ``fmt`` is ``markdown`` (or ``md``) or ``json``."""
    name = fmt.strip().lower() if isinstance(fmt, str) else ""
    if name in {"md", "markdown"}:
        return _MARKDOWN_TEMPLATE
    if name == "json":
        return json.dumps(_JSON_TEMPLATE, indent=2) + "\n"
    raise HandoffError("format must be markdown or json")


def explain_rules() -> dict[str, Any]:
    """The checks, statuses, and schema subset, as data."""
    return {
        "tool": "handoff-checker",
        "version": VERSION,
        "statuses": [
            {
                "name": READY,
                "meaning": "Every check passed. The handoff can be sent.",
            },
            {
                "name": NEEDS_FIXES,
                "meaning": (
                    "The payload can be repaired. problems lists each fix. "
                    "Do not send it yet."
                ),
            },
            {
                "name": BLOCKED,
                "meaning": (
                    "Do not send it. The budget left is exhausted, or the "
                    "payload is over the size cap. Other problems are listed too."
                ),
            },
        ],
        "required_fields": [name for name, _hint in REQUIRED_FIELDS],
        "checks": [
            "Required fields are present and non-empty.",
            "Cited local file paths exist, resolved against root.",
            "Acceptance criteria do not use vague words.",
            "No TODO, TBD, or FIXME is left in the payload.",
            "Questions are answered, or there are none.",
            f"The payload is at most max_bytes (default {_grouped(DEFAULT_MAX_BYTES)}).",
        ],
        "vague_terms": list(VAGUE_TERMS),
        "max_bytes": DEFAULT_MAX_BYTES,
        "max_bytes_ceiling": HARD_CAP_BYTES,
        "schema": {
            "note": (
                "A JSON schema file extends the built-in required fields. "
                "It cannot remove them. Supported keys: required (array of "
                "names), properties (type, minLength, minItems, enum, "
                "description), and additionalProperties false."
            ),
            "types": list(_SCHEMA_TYPES),
        },
        "problem_codes": [
            {"code": "missing-field", "severity": SEVERITY_FIX},
            {"code": "bad-shape", "severity": SEVERITY_FIX},
            {"code": "schema", "severity": SEVERITY_FIX},
            {"code": "vague-criterion", "severity": SEVERITY_FIX},
            {"code": "not-a-path", "severity": SEVERITY_FIX},
            {"code": "missing-path", "severity": SEVERITY_FIX},
            {"code": "unresolved-question", "severity": SEVERITY_FIX},
            {"code": "todo-left", "severity": SEVERITY_FIX},
            {"code": "budget-exhausted", "severity": SEVERITY_BLOCK},
            {"code": "too-large", "severity": SEVERITY_BLOCK},
        ],
        "exit_codes": {READY: 0, NEEDS_FIXES: 3, BLOCKED: 4, "error": 1},
    }


class HandoffChecker:
    """Validate one handoff at a time.

    ``root`` is the directory cited paths are resolved against. It defaults
    to the working directory at check time. ``schema`` is a path or a dict
    and is added to the built-in required fields. ``max_bytes`` is the size
    cap (default 16 KiB, ceiling 2 MiB).
    """

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        schema: str | Path | dict[str, Any] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = None if root is None else Path(root)
        self.schema = schema
        self.max_bytes = _as_max_bytes(max_bytes)

    def check(
        self,
        handoff: str | Path | dict[str, Any],
        *,
        root: str | Path | None = None,
        schema: str | Path | dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Validate ``handoff`` and return the decision. Does not write.

        ``handoff`` may be a dict, a file path, or the document text (JSON
        or Markdown). A string is read as a file when it does not look like
        a document and that path exists.
        """
        base = self.root if root is None else Path(root)
        resolved_root = _clean_root(Path.cwd() if base is None else base)
        limit = self.max_bytes if max_bytes is None else _as_max_bytes(max_bytes)
        schema_value = self.schema if schema is None else schema
        schema_data, schema_label = _load_schema(schema_value)
        loaded = _load_handoff(handoff)
        if loaded.unparsed or loaded.fields is None:
            problems = [_too_large(loaded.size, limit)]
            return _decision(
                problems=problems,
                size=loaded.size,
                limit=limit,
                root=resolved_root,
                fmt=loaded.fmt,
                schema_label=schema_label,
                found=[],
                missing=[],
            )
        return _evaluate(
            loaded,
            root=resolved_root,
            schema=schema_data,
            schema_label=schema_label,
            limit=limit,
        )

    def template(self, fmt: str = "markdown") -> str:
        """Return a blank handoff in ``markdown`` or ``json``."""
        return render_template(fmt)

    def explain(self) -> dict[str, Any]:
        """Return the checks and statuses as data."""
        return explain_rules()


def _evaluate(
    loaded: _Loaded,
    *,
    root: Path,
    schema: dict[str, Any] | None,
    schema_label: str | None,
    limit: int,
) -> dict[str, Any]:
    fields = loaded.fields or {}
    body = loaded.body
    problems: list[dict[str, Any]] = []
    problems.extend(_required_problems(fields))
    if schema is not None:
        problems.extend(_schema_problems(fields, schema))
    problems.extend(_vague_problems(fields))
    citations, shape_problems = _explicit_paths(fields)
    scan = loaded.raw if loaded.raw is not None else _stringify(fields, body)
    for cited in _regex_paths(scan):
        citations.append(cited)
    ordered = _dedupe_paths(citations)
    found, missing = _classify_paths(ordered, root)
    problems.extend(shape_problems)
    for cited in missing:
        problems.append(
            _problem(
                "missing-path",
                SEVERITY_FIX,
                _missing_path_message(cited),
                field="paths",
            )
        )
    problems.extend(_question_problems(fields, _prose(fields, body)))
    problems.extend(_todo_problems(scan))
    budget = _budget_problem(fields)
    if budget is not None:
        problems.append(budget)
    if loaded.size > limit:
        problems.append(_too_large(loaded.size, limit))
    return _decision(
        problems=problems,
        size=loaded.size,
        limit=limit,
        root=root,
        fmt=loaded.fmt,
        schema_label=schema_label,
        found=found,
        missing=missing,
    )


def _decision(
    *,
    problems: list[dict[str, Any]],
    size: int,
    limit: int,
    root: Path,
    fmt: str,
    schema_label: str | None,
    found: list[str],
    missing: list[str],
) -> dict[str, Any]:
    status = BLOCKED if any(item["severity"] == SEVERITY_BLOCK for item in problems) else (
        NEEDS_FIXES if problems else READY
    )
    return {
        "status": status,
        "ok_to_hand_off": status == READY,
        "summary": _summary(status, problems),
        "problems": problems,
        "bytes": size,
        "max_bytes": limit,
        "root": str(root),
        "format": fmt,
        "schema": schema_label,
        "paths": {"found": list(found), "missing": list(missing)},
    }


def _summary(status: str, problems: list[dict[str, Any]]) -> str:
    count = len(problems)
    noun = "problem" if count == 1 else "problems"
    if status == READY:
        return "ready to hand off"
    if status == BLOCKED:
        return f"blocked: {count} {noun}"
    return f"needs fixes: {count} {noun}"


def _problem(code: str, severity: str, message: str, field: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if field:
        body["field"] = field
    return body


def _required_problems(fields: dict[str, Any]) -> list[dict[str, Any]]:
    problems = []
    hints = dict(REQUIRED_FIELDS)
    for canonical, hint in REQUIRED_FIELDS:
        _key, value, present = _field_value(fields, canonical)
        if not present:
            problems.append(_missing(canonical, "missing", hint))
            continue
        if canonical == "acceptance_criteria":
            problems.extend(_check_criteria(value, hint))
            continue
        if canonical == "constraints":
            problems.extend(_check_constraints(value, hint))
            continue
        if canonical in {"goal", "owner"}:
            problems.extend(_check_text_field(canonical, value, hint))
            continue
        if _is_blank(value):
            problems.append(_missing(canonical, "empty", hint))
    return problems


def _check_text_field(canonical: str, value: Any, hint: str) -> list[dict[str, Any]]:
    if not isinstance(value, str):
        return [
            _problem(
                "bad-shape",
                SEVERITY_FIX,
                f"{canonical} must be text.",
                field=canonical,
            )
        ]
    if _is_blank(value) or (
        canonical == "owner" and value.strip().lower() in _OWNER_EMPTY
    ):
        return [_missing(canonical, "empty", hint)]
    return []


def _check_criteria(value: Any, hint: str) -> list[dict[str, Any]]:
    if isinstance(value, str):
        if _is_blank(value):
            return [_missing("acceptance_criteria", "empty", hint)]
        return []
    if not isinstance(value, list):
        return [
            _problem(
                "bad-shape",
                SEVERITY_FIX,
                "acceptance_criteria must be text or a list of text.",
                field="acceptance_criteria",
            )
        ]
    if not value:
        return [_missing("acceptance_criteria", "empty", hint)]
    problems = []
    real = 0
    for index, item in enumerate(value, start=1):
        if isinstance(item, str) and not _is_blank(item):
            real += 1
            continue
        if item is None or (isinstance(item, str) and _is_blank(item)):
            problems.append(
                _problem(
                    "bad-shape",
                    SEVERITY_FIX,
                    (
                        f"acceptance_criteria item {index} is empty. "
                        "Remove it or write a concrete check."
                    ),
                    field="acceptance_criteria",
                )
            )
        else:
            problems.append(
                _problem(
                    "bad-shape",
                    SEVERITY_FIX,
                    f"acceptance_criteria item {index} is not text.",
                    field="acceptance_criteria",
                )
            )
    if real == 0:
        return [_missing("acceptance_criteria", "empty", hint)]
    return problems


def _check_constraints(value: Any, hint: str) -> list[dict[str, Any]]:
    if isinstance(value, str):
        if _is_blank(value):
            return [_missing("constraints", "empty", hint)]
        return []
    if not isinstance(value, list):
        return [
            _problem(
                "bad-shape",
                SEVERITY_FIX,
                "constraints must be text or a list of text.",
                field="constraints",
            )
        ]
    if not value:
        return [_missing("constraints", "empty", hint)]
    problems = []
    real = 0
    for index, item in enumerate(value, start=1):
        if isinstance(item, str) and not _is_blank(item):
            real += 1
            continue
        problems.append(
            _problem(
                "bad-shape",
                SEVERITY_FIX,
                f"constraints item {index} is empty. Remove it or write the limit.",
                field="constraints",
            )
        )
    if real == 0:
        return [_missing("constraints", "empty", hint)]
    return problems


def _missing(field: str, kind: str, hint: str) -> dict[str, Any]:
    verb = "is missing" if kind == "missing" else "is empty"
    return _problem(
        "missing-field",
        SEVERITY_FIX,
        f"{field} {verb}. {hint}",
        field=field,
    )


def _vague_problems(fields: dict[str, Any]) -> list[dict[str, Any]]:
    problems = []
    for item in _criteria_texts(fields):
        term = _find_vague(item)
        if term is None:
            continue
        if term == "etc":
            follow = 'List each item instead of stopping at "etc".'
        elif term == "as needed":
            follow = 'Name the case you mean instead of "as needed".'
        else:
            follow = "Replace it with a check someone could mark pass or fail."
        problems.append(
            _problem(
                "vague-criterion",
                SEVERITY_FIX,
                f'Acceptance criterion {_quote(item)} contains "{term}". {follow}',
                field="acceptance_criteria",
            )
        )
    return problems


def _criteria_texts(fields: dict[str, Any]) -> list[str]:
    _key, value, present = _field_value(fields, "acceptance_criteria")
    if not present:
        return []
    if isinstance(value, str):
        return [] if _is_blank(value) else [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and not _is_blank(item)]
    return []


def _find_vague(text: str) -> str | None:
    lowered = text.lower()
    best_at: int | None = None
    best_term: str | None = None
    for term in VAGUE_TERMS:
        if term == "etc":
            pattern = r"\betc\.?(?!\w)"
        else:
            pattern = rf"\b{re.escape(term)}\b"
        match = re.search(pattern, lowered)
        if match is None:
            continue
        longer = best_term is not None and len(term) > len(best_term)
        if best_at is None or match.start() < best_at or (match.start() == best_at and longer):
            best_at = match.start()
            best_term = term
    return best_term


def _explicit_paths(
    fields: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    citations: list[str] = []
    problems: list[dict[str, Any]] = []
    for field in _PATH_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, str):
            entries: list[Any] = [value]
        elif isinstance(value, list):
            entries = list(value)
        else:
            problems.append(
                _problem(
                    "bad-shape",
                    SEVERITY_FIX,
                    f"{field} must be a list of paths.",
                    field=field,
                )
            )
            continue
        for index, item in enumerate(entries, start=1):
            if not isinstance(item, str):
                problems.append(
                    _problem(
                        "bad-shape",
                        SEVERITY_FIX,
                        f"{field} item {index} is not text.",
                        field=field,
                    )
                )
                continue
            text = item.strip()
            if _is_blank(text):
                problems.append(
                    _problem(
                        "not-a-path",
                        SEVERITY_FIX,
                        f"{field} item {index} is empty. Remove it or cite a path.",
                        field=field,
                    )
                )
                continue
            if _is_url(text):
                continue
            if not _looks_like_path(text):
                problems.append(
                    _problem(
                        "not-a-path",
                        SEVERITY_FIX,
                        (
                            f"{field} item {index} ({_quote(text)}) is not a local file path. "
                            "Cite a path or remove it."
                        ),
                        field=field,
                    )
                )
                continue
            citations.append(text)
    return citations, problems


def _regex_paths(text: str) -> list[str]:
    found: list[str] = []
    for match in _BARE_PATH.finditer(text):
        found.append(match.group(1))
    for match in _BACKTICK.finditer(text):
        span = match.group(1).strip()
        if _looks_like_path(span) and not _is_url(span):
            found.append(span)
    return found


def _dedupe_paths(citations: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for cited in citations:
        text = cited.strip().strip("`")
        if not _usable_citation(text):
            continue
        key = _cite_key(text)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def _classify_paths(citations: list[str], root: Path) -> tuple[list[str], list[str]]:
    found: list[str] = []
    missing: list[str] = []
    for cited in citations:
        if _path_exists(cited, root):
            found.append(cited)
        else:
            missing.append(cited)
    return found, missing


def _question_problems(fields: dict[str, Any], body: str) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    pending: list[str] = []
    answered: set[str] = set()
    raw = None
    field_name = "questions"
    for name in ("questions", "open_questions"):
        if name in fields:
            raw = fields[name]
            field_name = name
            break
    if isinstance(raw, str):
        if raw.strip().lower() not in {"none", "no", "no questions"} and not _is_blank(raw):
            pending.append(raw.strip())
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                if item.strip() and not _is_blank(item):
                    pending.append(item.strip())
                continue
            if not isinstance(item, dict):
                problems.append(
                    _problem(
                        "bad-shape",
                        SEVERITY_FIX,
                        f"{field_name} entries must be text or objects with a question.",
                        field=field_name,
                    )
                )
                continue
            question = ""
            for key in ("question", "q", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    question = value.strip()
                    break
            if not question:
                problems.append(
                    _problem(
                        "bad-shape",
                        SEVERITY_FIX,
                        f"{field_name} has an entry with no question text.",
                        field=field_name,
                    )
                )
                continue
            if _is_resolved(item):
                answered.add(_norm_q(question))
            else:
                pending.append(question)
    elif raw is not None and not _is_blank(raw):
        problems.append(
            _problem(
                "bad-shape",
                SEVERITY_FIX,
                f"{field_name} must be a list of questions.",
                field=field_name,
            )
        )
    known = {_norm_q(item) for item in pending} | answered
    for line in _body_questions(body):
        if _norm_q(line) not in known:
            pending.append(line)
    seen: set[str] = set()
    for question in pending:
        key = _norm_q(question)
        if not key or key in seen or key in answered:
            continue
        seen.add(key)
        problems.append(
            _problem(
                "unresolved-question",
                SEVERITY_FIX,
                f"Unresolved question: {_quote(question)} Answer it, or remove it.",
                field=field_name if raw is not None else "body",
            )
        )
    return problems


def _body_questions(body: str) -> list[str]:
    found = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or "?" not in stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        without_urls = re.sub(r"\b(?:https?|mailto):\S+", "", stripped)
        if "?" not in without_urls:
            continue
        found.append(stripped)
    return found


def _is_resolved(item: dict[str, Any]) -> bool:
    answer = ""
    for key in ("answer", "a", "resolution"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            answer = value.strip()
            break
    if answer and answer.lower() not in _NON_ANSWERS:
        return True
    if item.get("resolved") is True and (not answer or answer.lower() not in _NON_ANSWERS):
        return True
    return False


def _todo_problems(text: str) -> list[dict[str, Any]]:
    problems = []
    seen: set[str] = set()
    for match in _TODO_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        snippet = " ".join(text[line_start:line_end].split())
        if len(snippet) > 120:
            snippet = snippet[:117].rstrip() + "..."
        if snippet.lower() in seen:
            continue
        seen.add(snippet.lower())
        label = match.group(1).upper()
        problems.append(
            _problem(
                "todo-left",
                SEVERITY_FIX,
                f'{label} left in: "{snippet}" Finish it or delete it.',
                field="body",
            )
        )
    return problems


def _budget_problem(fields: dict[str, Any]) -> dict[str, Any] | None:
    key, value, present = _field_value(fields, "budget_left")
    if not present or _is_blank(value):
        return None
    state, shown = _budget_state(value)
    if state == "ok":
        return None
    if state == "bad":
        return _problem(
            "bad-shape",
            SEVERITY_FIX,
            (
                f"{key} must be a number, a short string such as "
                '"4000 tokens", or an object of amounts.'
            ),
            field=key,
        )
    return _problem(
        "budget-exhausted",
        SEVERITY_BLOCK,
        (
            f"budget_left is exhausted ({shown}). The next owner has nothing "
            "to spend. Raise it, or do not hand this off."
        ),
        field=key,
    )


def _budget_state(value: Any) -> tuple[str, str]:
    if isinstance(value, dict):
        if not value:
            return "bad", ""
        saw_amount = False
        saw_positive = False
        for item in value.values():
            leaf = _budget_leaf(item)
            if leaf == "bad":
                return "bad", ""
            if leaf == "skip":
                continue
            saw_amount = True
            if leaf == "ok":
                saw_positive = True
        if not saw_amount:
            return "bad", ""
        if not saw_positive:
            return "exhausted", json.dumps(value, separators=(",", ":"))
        return "ok", ""
    leaf = _budget_leaf(value)
    if leaf == "ok" or leaf == "skip":
        return ("ok" if leaf == "ok" else "bad"), ""
    if leaf == "exhausted":
        shown = value.strip() if isinstance(value, str) else _format_number(value)
        return "exhausted", shown
    return "bad", ""


def _budget_leaf(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "skip"
    if isinstance(value, bool):
        return "bad"
    if isinstance(value, (int, float)):
        return "exhausted" if value <= 0 else "ok"
    if isinstance(value, str):
        text = " ".join(value.strip().lower().split())
        if not text:
            return "skip"
        if text in _EXHAUSTED_WORDS or _EXHAUSTED_RE.fullmatch(text):
            return "exhausted"
        return "ok"
    return "bad"


def _schema_problems(fields: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    problems = []
    builtin = {name for name, _hint in REQUIRED_FIELDS}
    alias_names = {alias for names in _ALIASES.values() for alias in names}
    for name in schema.get("required", []):
        if name in builtin or name in alias_names:
            continue
        if name not in fields:
            problems.append(
                _problem(
                    "schema",
                    SEVERITY_FIX,
                    f"{name} is missing. The schema requires it.",
                    field=name,
                )
            )
            continue
        if _is_blank(fields[name]):
            problems.append(
                _problem(
                    "schema",
                    SEVERITY_FIX,
                    f"{name} is empty. The schema requires it.",
                    field=name,
                )
            )
    properties = schema.get("properties") or {}
    if isinstance(properties, dict):
        for name, spec in properties.items():
            if name not in fields or _is_blank(fields[name]):
                continue
            problems.extend(_check_property(name, fields[name], spec))
    if schema.get("additionalProperties", True) is False:
        allowed = set(properties) | _STANDARD_FIELDS
        for name in sorted(fields):
            if name not in allowed:
                problems.append(
                    _problem(
                        "schema",
                        SEVERITY_FIX,
                        (
                            f'Unexpected field "{name}". The schema does not allow it. '
                            "Remove it, or add it to properties."
                        ),
                        field=name,
                    )
                )
    return problems


def _check_property(name: str, value: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
    problems = []
    expected = spec.get("type")
    if isinstance(expected, str) and not _type_ok(value, expected):
        problems.append(
            _problem(
                "schema",
                SEVERITY_FIX,
                (
                    f"{name} must be a {expected}. The value is "
                    f"{_type_name(value)}."
                ),
                field=name,
            )
        )
        return problems
    if "enum" in spec and value not in spec["enum"]:
        choices = ", ".join(str(item) for item in spec["enum"])
        problems.append(
            _problem(
                "schema",
                SEVERITY_FIX,
                f"{name} must be one of: {choices}.",
                field=name,
            )
        )
    if "minLength" in spec and isinstance(value, str) and len(value) < spec["minLength"]:
        problems.append(
            _problem(
                "schema",
                SEVERITY_FIX,
                f"{name} must be at least {spec['minLength']} characters.",
                field=name,
            )
        )
    if "minItems" in spec and isinstance(value, list) and len(value) < spec["minItems"]:
        problems.append(
            _problem(
                "schema",
                SEVERITY_FIX,
                f"{name} must have at least {spec['minItems']} items.",
                field=name,
            )
        )
    return problems


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return type(value) is bool
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in (int, float)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _type_name(value: Any) -> str:
    if type(value) is bool:
        return "a boolean"
    if type(value) is int:
        return "an integer"
    if isinstance(value, float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    if value is None:
        return "null"
    return type(value).__name__


def _load_schema(
    schema: str | Path | dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if schema is None:
        return None, None
    if isinstance(schema, dict):
        _validate_schema(schema, "schema")
        return schema, "inline"
    if isinstance(schema, (str, Path)):
        path = Path(schema)
        if not path.is_file():
            raise HandoffError(f"no schema file at {path}")
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise HandoffError(f"schema {path} is not UTF-8") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HandoffError(f"schema {path} is not valid JSON") from exc
        if not isinstance(data, dict):
            raise HandoffError(f"schema {path} must be a JSON object")
        _validate_schema(data, f"schema {path}")
        return data, str(path)
    raise HandoffError("schema must be a path or a JSON object")


def _validate_schema(data: dict[str, Any], label: str) -> None:
    unknown = sorted(set(data) - _SCHEMA_KEYS)
    if unknown:
        raise HandoffError(
            f"{label} key {unknown[0]!r} is not supported. "
            "Use required, properties, and additionalProperties."
        )
    required = data.get("required", [])
    if (
        not isinstance(required, list)
        or any(type(item) is not str or not item.strip() for item in required)
    ):
        raise HandoffError(f"{label} required must be an array of field names")
    properties = data.get("properties", {})
    if not isinstance(properties, dict):
        raise HandoffError(f"{label} properties must be an object")
    for name, spec in properties.items():
        if type(name) is not str or not name.strip():
            raise HandoffError(f"{label} properties must be named with strings")
        if not isinstance(spec, dict):
            raise HandoffError(f"{label} properties.{name} must be an object")
        extra = sorted(set(spec) - _PROP_KEYS)
        if extra:
            raise HandoffError(
                f"{label} properties.{name}.{extra[0]} is not supported. "
                "Use type, minLength, minItems, enum, or description."
            )
        if "type" in spec and spec["type"] not in _SCHEMA_TYPES:
            allowed = ", ".join(_SCHEMA_TYPES)
            raise HandoffError(f"{label} properties.{name}.type must be one of: {allowed}")
        if "minLength" in spec:
            _schema_int(spec["minLength"], f"{label} properties.{name}.minLength")
        if "minItems" in spec:
            _schema_int(spec["minItems"], f"{label} properties.{name}.minItems")
        if "enum" in spec:
            enum = spec["enum"]
            if not isinstance(enum, list) or not enum:
                raise HandoffError(f"{label} properties.{name}.enum must be a non-empty array")
    if "additionalProperties" in data and type(data["additionalProperties"]) is not bool:
        raise HandoffError(f"{label} additionalProperties must be a boolean")


def _schema_int(value: Any, label: str) -> None:
    if type(value) is not int or value < 0:
        raise HandoffError(f"{label} must be an integer >= 0")


def _load_handoff(handoff: str | Path | dict[str, Any]) -> _Loaded:
    if isinstance(handoff, dict):
        raw = json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))
        size = len(raw.encode("utf-8"))
        if size > HARD_CAP_BYTES:
            return _Loaded(None, "", None, size, "dict", True)
        body = handoff.get("body") if isinstance(handoff.get("body"), str) else ""
        return _Loaded(handoff, body, None, size, "dict", False)
    if isinstance(handoff, Path):
        return _load_file(handoff)
    if not isinstance(handoff, str):
        raise HandoffError("handoff must be a dict, a path, or document text")
    if _is_file_arg(handoff):
        return _load_file(Path(handoff))
    leading = handoff.lstrip("\ufeff").lstrip()
    looks_like_doc = (
        not leading
        or "\n" in handoff
        or leading.startswith("{")
        or leading.startswith("---")
    )
    if not looks_like_doc:
        raise HandoffError(f"no handoff file at {handoff}")
    size = len(handoff.encode("utf-8"))
    if size > HARD_CAP_BYTES:
        return _Loaded(None, "", None, size, "unparsed", True)
    parsed = parse_handoff(handoff)
    fmt = "json" if leading.startswith("{") else "markdown"
    return _Loaded(parsed["fields"], parsed["body"], handoff, size, fmt, False)


def _is_file_arg(value: str) -> bool:
    leading = value.lstrip("\ufeff").lstrip()
    if not leading or "\n" in value or leading.startswith("{") or leading.startswith("---"):
        return False
    return Path(value).is_file()


def _load_file(path: Path) -> _Loaded:
    if not path.is_file():
        raise HandoffError(f"no handoff file at {path}")
    size = path.stat().st_size
    if size > HARD_CAP_BYTES:
        return _Loaded(None, "", None, size, "unparsed", True)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise HandoffError(f"handoff {path} is not UTF-8") from exc
    size = len(text.encode("utf-8"))
    if size > HARD_CAP_BYTES:
        return _Loaded(None, "", None, size, "unparsed", True)
    parsed = parse_handoff(text)
    fmt = "json" if text.lstrip("\ufeff").lstrip().startswith("{") else "markdown"
    return _Loaded(parsed["fields"], parsed["body"], text, size, fmt, False)


def _parse_markdown(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise HandoffError("front matter must open with a line that is just ---")
    close = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            close = index
            break
    if close is None:
        raise HandoffError("front matter is missing the closing ---")
    fields = _parse_front_matter("\n".join(lines[1:close]))
    body = "\n".join(lines[close + 1 :])
    return {"fields": fields, "body": body}


def _parse_front_matter(block: str) -> dict[str, Any]:
    lines = block.splitlines()
    result: dict[str, Any] = {}
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line[0] in " \t":
            raise HandoffError(
                f"front matter line {index + 1} is indented without a parent key"
            )
        key, sep, rest = line.partition(":")
        key = key.strip()
        if not sep or _KEY.fullmatch(key) is None:
            raise HandoffError(f"front matter line {index + 1} is not a key: value pair")
        if key in result:
            raise HandoffError(f"front matter key {key!r} is repeated")
        rest = rest.strip()
        if rest != "":
            result[key] = _scalar(rest, index + 1)
            index += 1
            continue
        index += 1
        child: list[str] = []
        while index < total and (not lines[index].strip() or lines[index][0] in " \t"):
            child.append(lines[index])
            index += 1
        while child and not child[0].strip():
            child.pop(0)
        while child and not child[-1].strip():
            child.pop()
        result[key] = "" if not child else _parse_indented(child)
    return result


def _parse_indented(lines: list[str]) -> Any:
    dedented = _dedent(lines)
    content = [line for line in dedented if line.strip() and not line.lstrip().startswith("#")]
    if not content:
        return ""
    if content[0].startswith("- "):
        return _parse_list(dedented)
    return _parse_front_matter("\n".join(dedented))


def _dedent(lines: list[str]) -> list[str]:
    indents = []
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("\t"):
            raise HandoffError("front matter indentation must be spaces, not tabs")
        indents.append(len(line) - len(line.lstrip(" ")))
    if not indents:
        return []
    cut = min(indents)
    if cut < 1:
        raise HandoffError("front matter block is not indented")
    output = []
    for line in lines:
        if not line.strip():
            output.append("")
            continue
        width = len(line) - len(line.lstrip(" "))
        if width < cut:
            raise HandoffError("front matter indentation is inconsistent")
        output.append(line[cut:])
    return output


def _parse_list(lines: list[str]) -> list[Any]:
    items: list[Any] = []
    map_lines: list[str] | None = None

    def flush() -> None:
        nonlocal map_lines
        if map_lines is not None:
            items.append(_parse_front_matter("\n".join(map_lines)))
            map_lines = None

    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- "):
            flush()
            rest = line[2:].strip()
            key, sep, val = rest.partition(":")
            if sep and _KEY.fullmatch(key.strip()) is not None and not rest.startswith(('"', "'")):
                map_lines = [f"{key.strip()}:{val}"]
                continue
            items.append(_scalar(rest, 0))
            continue
        if map_lines is not None and line[0] in " \t":
            map_lines.append(line.strip())
            continue
        raise HandoffError("front matter list item is not valid")
    flush()
    return items


def _scalar(text: str, line: int) -> Any:
    if text == "":
        return ""
    if text[0] in {'"', "'"}:
        if len(text) < 2 or text[-1] != text[0]:
            where = f"line {line}" if line else "value"
            raise HandoffError(f"front matter {where} has an unclosed quote")
        return _unquote(text)
    if text[0] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            where = f"line {line}" if line else "value"
            raise HandoffError(
                f"front matter {where} looks like JSON but is not valid: {exc.msg}"
            ) from exc
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _unquote(text: str) -> str:
    quote = text[0]
    inner = text[1:-1]
    if quote == '"':
        return inner.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return inner.replace("\\'", "'").replace("\\\\", "\\")


def _field_value(fields: dict[str, Any], canonical: str) -> tuple[str, Any, bool]:
    for key in _ALIASES[canonical]:
        if key in fields:
            return key, fields[key], True
    return canonical, None, False


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _BLANK
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _is_url(value: str) -> bool:
    if "://" in value:
        return True
    return value.lower().startswith("mailto:")


def _looks_like_path(value: str) -> bool:
    if not value or any(ch.isspace() for ch in value):
        return False
    if _is_url(value):
        return False
    if "/" in value or "\\" in value or value.startswith("."):
        return True
    return re.fullmatch(rf"[\w.-]+\.(?:{_EXTS})", value) is not None


def _usable_citation(value: str) -> bool:
    if not value or value in {".", "..", "/", "./"}:
        return False
    if len(value) > 240 or _is_url(value):
        return False
    return _looks_like_path(value)


def _cite_key(value: str) -> str:
    text = value.strip().strip("`").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _path_exists(cited: str, root: Path) -> bool:
    try:
        candidate = Path(cited)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.exists()
    except (OSError, ValueError):
        return False


def _missing_path_message(cited: str) -> str:
    shown = cited if " " not in cited else '"' + cited.replace('"', "'") + '"'
    return f"{shown} does not exist. Create it, or remove the citation."


def _stringify(fields: dict[str, Any], body: str) -> str:
    return "\n".join([body, *_walk_strings(fields)])


def _prose(fields: dict[str, Any], body: str) -> str:
    parts = [body]
    for name in _PROSE_FIELDS:
        value = fields.get(name)
        if isinstance(value, str) and value not in parts:
            parts.append(value)
    return "\n".join(part for part in parts if part)


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(_walk_strings(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_walk_strings(item))
        return found
    return []


def _norm_q(text: str) -> str:
    return " ".join(text.lower().replace("?", " ").split())


def _too_large(size: int, limit: int) -> dict[str, Any]:
    return _problem(
        "too-large",
        SEVERITY_BLOCK,
        (
            f"Payload is {_grouped(size)} bytes. The cap is {_grouped(limit)}. "
            "Move long text into files and cite the paths."
        ),
        field="payload",
    )


def _format_number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _grouped(value: int) -> str:
    return f"{value:,}"


def _as_max_bytes(value: Any) -> int:
    if type(value) is bool:
        raise HandoffError("max_bytes must be an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise HandoffError("max_bytes must be an integer")
        value = int(value)
    if type(value) is not int:
        raise HandoffError("max_bytes must be an integer")
    if value < 1 or value > HARD_CAP_BYTES:
        raise HandoffError(f"max_bytes must be from 1 to {_grouped(HARD_CAP_BYTES)}")
    return value


def _clean_root(root: Path) -> Path:
    if not root.exists():
        raise HandoffError(f"root directory does not exist: {root}")
    if not root.is_dir():
        raise HandoffError(f"root is not a directory: {root}")
    return root


def _quote(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split()).replace('"', "'")
    if len(flat) > limit:
        flat = flat[: limit - 3].rstrip() + "..."
    return f'"{flat}"'


# --- CLI -------------------------------------------------------------------


def format_human(payload: dict[str, Any]) -> str:
    lines = [
        f"{'status':<8}  {payload['status']}",
        f"{'summary':<8}  {payload['summary']}",
    ]
    problems = payload["problems"]
    if not problems:
        lines.append(f"{'problems':<8}  none")
    else:
        lines.append(f"{'problems':<8}  {len(problems)}")
        for index, problem in enumerate(problems, start=1):
            lines.append(f"{'':<8}  {index}. {problem['message']}")
    return "\n".join(lines) + "\n"


def format_explain(rules: dict[str, Any]) -> str:
    lines = [f"handoff-checker {rules['version']}", ""]
    lines.append(f"{'status':<10}what check() returns")
    for item in rules["statuses"]:
        lines.append(f"{'':<10}{item['name']:<14}{item['meaning']}")
    lines.append("")
    lines.append(f"{'required':<10}{', '.join(rules['required_fields'])}")
    lines.append(f"{'checks':<10}{rules['checks'][0]}")
    for item in rules["checks"][1:]:
        lines.append(f"{'':<10}{item}")
    lines.append(f"{'vague':<10}{', '.join(rules['vague_terms'])}")
    lines.append(f"{'schema':<10}{rules['schema']['note']}")
    lines.append(
        f"{'exit':<10}0 ready, 3 needs-fixes, 4 blocked, 1 error"
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff-checker",
        description=(
            "Check a handoff before one agent sends work to the next. "
            "JSON, or Markdown with front matter."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes for check:\n"
            "  0  ready\n"
            "  3  needs-fixes\n"
            "  4  blocked\n"
            "  1  error\n"
            "\n"
            "needs-fixes means repair the punch list and check again.\n"
            "blocked means do not send the handoff.\n"
            "\n"
            f"Size cap default: {_grouped(DEFAULT_MAX_BYTES)} bytes "
            f"(ceiling {_grouped(HARD_CAP_BYTES)}).\n"
            "Cited paths resolve against --root, or the working directory."
        ),
    )
    parser.add_argument("--version", action="version", version=f"handoff-checker {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    check_cmd = sub.add_parser("check", help="validate a handoff file, or - for stdin")
    check_cmd.add_argument("path", help="handoff file, or - to read stdin")
    check_cmd.add_argument(
        "--root",
        default=None,
        help="directory cited paths are resolved against (default: working directory)",
    )
    check_cmd.add_argument(
        "--schema",
        default=None,
        help="JSON schema file of extra required fields and simple types",
    )
    check_cmd.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help=f"payload cap in bytes (default {DEFAULT_MAX_BYTES})",
    )
    check_cmd.add_argument("--json", action="store_true", help="print the decision as JSON")

    template_cmd = sub.add_parser("template", help="print a blank handoff")
    template_cmd.add_argument(
        "--format",
        default="markdown",
        choices=("markdown", "md", "json"),
        help="markdown (default) or json",
    )
    template_cmd.add_argument(
        "--output",
        default=None,
        help="also write the template to this path",
    )

    explain_cmd = sub.add_parser("explain", help="print the checks and the statuses")
    explain_cmd.add_argument("--json", action="store_true", help="print the rules as JSON")

    serve_cmd = sub.add_parser(
        "serve",
        help="MCP server on stdio (one JSON-RPC message per line)",
    )
    serve_cmd.add_argument("--root", default=None, help="default root for tool calls")
    serve_cmd.add_argument("--schema", default=None, help="default schema file for tool calls")
    serve_cmd.add_argument("--max-bytes", type=int, default=None, help="default size cap")
    return parser


def _render(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(format_human(payload))


def _exit_for(payload: dict[str, Any]) -> int:
    if payload["status"] == NEEDS_FIXES:
        return 3
    if payload["status"] == BLOCKED:
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "template":
            text = render_template(args.format)
            if args.output:
                destination = Path(args.output)
                if not destination.parent.exists():
                    raise HandoffError(f"directory does not exist: {destination.parent}")
                destination.write_text(text, encoding="utf-8")
            sys.stdout.write(text)
            return 0
        if args.command == "explain":
            rules = explain_rules()
            if args.json:
                sys.stdout.write(json.dumps(rules, indent=2) + "\n")
            else:
                sys.stdout.write(format_explain(rules))
            return 0
        if args.command == "serve":
            return serve(
                _Defaults(
                    root=args.root,
                    schema=args.schema,
                    max_bytes=None if args.max_bytes is None else _as_max_bytes(args.max_bytes),
                )
            )
        if args.command != "check":  # pragma: no cover - argparse requires a command
            parser.error(f"unknown command {args.command}")
            return 2
        source: str | Path
        if args.path == "-":
            source = sys.stdin.read(HARD_CAP_BYTES + 1)
        else:
            source = Path(args.path)
        checker = HandoffChecker(
            root=args.root,
            schema=args.schema,
            max_bytes=DEFAULT_MAX_BYTES if args.max_bytes is None else args.max_bytes,
        )
        payload = checker.check(source)
    except HandoffError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except BrokenPipeError:
        return 0
    _render(payload, as_json=args.json)
    return _exit_for(payload)


# --- MCP (stdio, newline-delimited JSON-RPC) --------------------------------

_MCP_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
_MCP_INSTRUCTIONS = (
    "Handoff checker. Call handoff_template for a blank payload, then "
    "handoff_check before one agent sends work to another. status ready "
    "means send it. needs-fixes means repair every problem and check again. "
    "blocked means do not send it: the budget is exhausted or the payload "
    "is over the size cap. Built-in required fields: goal, "
    "acceptance_criteria, owner, constraints, budget_left. A schema_path "
    "JSON file can require more fields. It cannot remove the built-in ones. "
    "Cited local paths are checked against root. handoff_explain lists the rules."
)


def _tool_defs() -> list[dict[str, Any]]:
    return [
        {
            "name": "handoff_check",
            "description": (
                "Validate a handoff before it is sent. Pass handoff (markdown or "
                "JSON text) or path (a file). Returns status ready, needs-fixes, "
                "or blocked, plus problems. Does not modify the handoff. Send it "
                "only when status is ready."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "handoff": {
                        "type": "string",
                        "description": "Markdown or JSON handoff text. Omit when path is set.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Read the handoff from this file. Omit when handoff is set.",
                    },
                    "root": {
                        "type": "string",
                        "description": "Directory cited paths resolve against.",
                    },
                    "schema_path": {
                        "type": "string",
                        "description": "JSON schema file of extra required fields.",
                    },
                    "schema": {
                        "type": "object",
                        "description": "Inline schema object. Omit when schema_path is set.",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "description": f"Size cap. Default {DEFAULT_MAX_BYTES}.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "handoff_template",
            "description": "Return a blank handoff the caller can fill in. format is markdown or json.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "md", "json"],
                        "description": "markdown (default) or json.",
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "handoff_explain",
            "description": "List statuses, required fields, vague terms, and the schema subset.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def _tool_result(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _reject_unknown(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise HandoffError(f"unknown field {unknown[0]!r}")


def _dispatch_tool(name: str, args: dict[str, Any], defaults: _Defaults) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise HandoffError("arguments must be an object")
    if name == "handoff_template":
        _reject_unknown(args, {"format"})
        fmt = args.get("format", "markdown")
        if not isinstance(fmt, str):
            raise HandoffError("format must be a string")
        return _tool_result(render_template(fmt))
    if name == "handoff_explain":
        _reject_unknown(args, set())
        return _tool_result(explain_rules())
    if name != "handoff_check":
        return _tool_error(f"unknown tool {name!r}")
    _reject_unknown(
        args,
        {"handoff", "path", "root", "schema_path", "schema", "max_bytes"},
    )
    has_text = "handoff" in args and args.get("handoff") not in (None, "")
    has_path = "path" in args and args.get("path") not in (None, "")
    if has_text and has_path:
        raise HandoffError("pass handoff or path, not both")
    if not has_text and not has_path:
        raise HandoffError("pass handoff text or a path")
    if has_text and not isinstance(args.get("handoff"), str):
        raise HandoffError("handoff must be a string")
    if has_path and not isinstance(args.get("path"), str):
        raise HandoffError("path must be a string")
    if "schema" in args and "schema_path" in args and args.get("schema") not in (None, "") and args.get(
        "schema_path"
    ) not in (None, ""):
        raise HandoffError("pass schema or schema_path, not both")
    root = args.get("root", defaults.root)
    if root is not None and not isinstance(root, str):
        raise HandoffError("root must be a string")
    schema: str | Path | dict[str, Any] | None
    if isinstance(args.get("schema"), dict):
        schema = args["schema"]
    elif isinstance(args.get("schema_path"), str) and args.get("schema_path"):
        schema = args["schema_path"]
    else:
        schema = defaults.schema
    if "max_bytes" in args and args.get("max_bytes") is not None:
        max_bytes: int | None = _as_max_bytes(args.get("max_bytes"))
    else:
        max_bytes = defaults.max_bytes
    checker = HandoffChecker(
        root=root,
        schema=schema,
        max_bytes=DEFAULT_MAX_BYTES if max_bytes is None else max_bytes,
    )
    source: str | Path = args["path"] if has_path else args["handoff"]
    return _tool_result(checker.check(source))


def handle_message(message: Any, defaults: _Defaults | None = None) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Notifications return None."""
    if defaults is None:
        defaults = _Defaults(None, None, None)
    if not isinstance(message, dict) or message.get("jsonrpc") not in (None, "2.0"):
        return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    if "method" not in message:
        return _rpc_error(message.get("id"), -32600, "invalid request")
    method = message.get("method")
    if not isinstance(method, str):
        return _rpc_error(message.get("id"), -32600, "invalid request")
    has_id = "id" in message
    req_id = message.get("id")
    params = message.get("params")
    if params is None:
        params = {}

    if method.startswith("notifications/") or method in {"initialized", "notifications/initialized"}:
        return None

    def result(body: dict[str, Any]) -> dict[str, Any] | None:
        if not has_id:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": body}

    if method in {"ping", "tools/list", "resources/list", "resources/templates/list", "prompts/list"}:
        if method == "ping":
            return result({})
        if method == "tools/list":
            return result({"tools": _tool_defs()})
        if method == "resources/list":
            return result({"resources": []})
        if method == "resources/templates/list":
            return result({"resourceTemplates": []})
        return result({"prompts": []})

    if not isinstance(params, dict):
        return _rpc_error(req_id if has_id else None, -32602, "invalid params")

    if method == "initialize":
        requested = params.get("protocolVersion", "2024-11-05")
        protocol = requested if requested in _MCP_PROTOCOLS else "2024-11-05"
        return result(
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "handoff-checker", "version": VERSION},
                "instructions": _MCP_INSTRUCTIONS,
            }
        )
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str):
            return result(_tool_error("tools/call requires a tool name"))
        try:
            body = _dispatch_tool(tool_name, arguments, defaults)
        except HandoffError as exc:
            body = _tool_error(str(exc))
        return result(body)
    if not has_id:
        return None
    return _rpc_error(req_id, -32601, f"method not found: {method}")


def serve(defaults: _Defaults | None = None) -> int:
    """Speak MCP over stdio. One JSON object per line. Logs never touch stdout."""
    if defaults is None:
        defaults = _Defaults(None, None, None)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = _rpc_error(None, -32700, "parse error")
        else:
            try:
                response = handle_message(message, defaults)
            except Exception as exc:  # noqa: BLE001 - keep the stdio loop alive
                req_id = message.get("id") if isinstance(message, dict) else None
                sys.stderr.write(f"handoff-checker: {exc}\n")
                response = _rpc_error(req_id, -32603, "internal error")
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
