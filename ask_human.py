#!/usr/bin/env python3
"""Local approval ledger for AI agents.

When a harness is told to stop and ask a human, this file stores the
question and the later answer. ask() files one pending request for a task.
answer() records approved or denied for that exact request. status() and
list_requests() read the ledger back.

A request id is fixed at ask time, together with the task, the question,
and the action. An answer applies only to that id. A later ask gets a new
id. An answer for the old id does not approve the new one, and a decided
request is not overwritten.

This is a local record, not a check of who the human is, and not a security
boundary. Any caller that can write the ledger, or call answer(), can store
an approved decision. Restrict the answer path to the human. Recording an
approval does not raise a budget, start a tool, or resume work.

No third-party dependencies. Python 3.9+. No network calls.

  python3 ask_human.py ask TASK --question "..." --action "..."
  python3 ask_human.py status TASK
  python3 ask_human.py answer TASK --request-id ID --decision approved \\
      --question "..." --action "..."
  python3 ask_human.py list
  python3 ask_human.py explain
  python3 ask_human.py serve
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

VERSION = "0.1.0"
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
NONE = "none"
DECISIONS = (APPROVED, DENIED)
STATUSES = (PENDING, APPROVED, DENIED, NONE)

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_REQUESTS = 1000
MAX_LEDGER_BYTES = 8 * 1024 * 1024
MAX_QUESTION = 1000
MAX_ACTION = 300
MAX_NOTE = 200
MAX_BY = 80
DEFAULT_LEDGER_REL = Path(".agent-toolkit") / "approvals.json"

_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST = re.compile(r"^req_[0-9a-f]{32}$")
_LIST_STATES = {"pending", "approved", "denied", "all"}
_TOP_KEYS = {"version", "requests", "pending"}
_ROW_KEYS = {
    "request_id",
    "task_id",
    "question",
    "action",
    "status",
    "asked_at",
    "note",
    "answered_at",
    "by",
    "answer_note",
}

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


class ApprovalError(Exception):
    """An approval the caller can fix: bad input, or a broken ledger."""


class _Defaults(NamedTuple):
    root: Path
    ledger: Path


def explain_rules() -> dict[str, Any]:
    """The statuses, results, and the trust limit, as data."""
    return {
        "tool": "ask-human",
        "version": VERSION,
        "statuses": list(STATUSES),
        "decisions": list(DECISIONS),
        "results": [
            {"name": "asked", "ok": True, "meaning": "A new pending request was stored."},
            {
                "name": "pending-exists",
                "ok": False,
                "meaning": "This task already has a pending request. Answer that id, or leave it.",
            },
            {"name": "approved", "ok": True, "meaning": "That request was recorded as approved."},
            {"name": "denied", "ok": True, "meaning": "That request was recorded as denied."},
            {
                "name": "already-decided",
                "ok": False,
                "meaning": "That request is already approved or denied. It was not changed.",
            },
            {
                "name": "mismatch",
                "ok": False,
                "meaning": "The task, question, or action does not match that request.",
            },
            {"name": "absent", "ok": False, "meaning": "No request with that id."},
        ],
        "trust": (
            "A local record of a decision. Not proof of who answered, and not a "
            "security boundary. A caller who can write the ledger or call answer "
            "can store approved. Restrict the answer path. Approval does not raise "
            "a budget or resume work."
        ),
        "exit_codes": {"ok": 0, "refused": 3, "error": 1},
    }


class ApprovalLedger:
    """Approval requests stored in one JSON file.

    The file is replaced atomically. A sibling ``<ledger>.lock`` file
    serializes readers and writers across processes. Pass ``clock``
    (a callable returning a timezone-aware datetime) to fix timestamps
    in tests. A missing file reads as no requests and is created on ask.
    """

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        max_requests: int = MAX_REQUESTS,
    ) -> None:
        self.path = Path(ledger_path)
        self._clock = clock or _utcnow
        if type(max_requests) is not int or max_requests < 1:
            raise ApprovalError("max_requests must be an integer >= 1")
        self.max_requests = max_requests

    def ask(
        self,
        task_id: str,
        question: str,
        *,
        action: str,
        note: str = "",
    ) -> dict[str, Any]:
        """File one pending request. Does not approve it and does not run work.

        A second ask for a task that already has a pending request returns
        ``pending-exists`` and writes nothing.
        """
        task_id = _task_id(task_id)
        question = _bounded("question", question, MAX_QUESTION, empty_ok=False)
        action = _bounded("action", action, MAX_ACTION, empty_ok=False)
        note = _bounded("note", note, MAX_NOTE, empty_ok=True)
        now = self._now()

        def mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            current = data["pending"].get(task_id)
            if current is not None:
                return _outcome("pending-exists", False, data["requests"][current]), False
            if len(data["requests"]) >= self.max_requests:
                raise ApprovalError(
                    f"ledger is full ({self.max_requests} requests); it was not changed"
                )
            request_id = _new_id(data["requests"])
            row = {
                "request_id": request_id,
                "task_id": task_id,
                "question": question,
                "action": action,
                "status": PENDING,
                "asked_at": _iso(now),
                "note": note,
                "answered_at": None,
                "by": "",
                "answer_note": "",
            }
            data["requests"][request_id] = row
            data["pending"][task_id] = request_id
            return _outcome("asked", True, row), True

        return self._transaction(mutate, create=True)

    def answer(
        self,
        task_id: str,
        request_id: str,
        decision: str,
        *,
        question: str,
        action: str,
        by: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Record approved or denied for one request id. Writes nothing on refusal.

        ``question`` and ``action`` must be the text stored at ask time.
        A decided request is left as it is. An id from an earlier request
        does not decide a later one.
        """
        task_id = _task_id(task_id)
        request_id = _request_id(request_id)
        decision = _decision(decision)
        question = _bounded("question", question, MAX_QUESTION, empty_ok=False)
        action = _bounded("action", action, MAX_ACTION, empty_ok=False)
        by = _bounded("by", by, MAX_BY, empty_ok=True)
        note = _bounded("note", note, MAX_NOTE, empty_ok=True)
        if not self.path.exists():
            return _absent(task_id, request_id)
        now = self._now()

        def mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            row = data["requests"].get(request_id)
            if row is None:
                return _absent(task_id, request_id), False
            if row["task_id"] != task_id or row["question"] != question or row["action"] != action:
                return _outcome("mismatch", False, row), False
            if row["status"] != PENDING:
                return _outcome("already-decided", False, row), False
            if data["pending"].get(task_id) != request_id:
                raise ApprovalError(f"ledger {self.path} has a broken pending index")
            row["status"] = decision
            row["answered_at"] = _iso(now)
            row["by"] = by
            row["answer_note"] = note
            del data["pending"][task_id]
            return _outcome(decision, True, row), True

        return self._transaction(mutate, create=False)

    def status(self, task_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        """Read one task, or one request id. A missing ledger is ``none``.

        With no ``request_id``, a pending request wins over older decisions.
        Otherwise the latest decided request for the task is returned.
        Passing ``request_id`` reads that row only. It must belong to the task.
        """
        task_id = _task_id(task_id)
        if request_id is not None:
            request_id = _request_id(request_id)
        if not self.path.exists():
            if request_id is not None:
                raise ApprovalError(f"no approval request {request_id}")
            return _none(task_id)

        def read(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            if request_id is not None:
                row = data["requests"].get(request_id)
                if row is None:
                    raise ApprovalError(f"no approval request {request_id}")
                if row["task_id"] != task_id:
                    raise ApprovalError(
                        f"request {request_id} is not for task {task_id!r}"
                    )
                return _view(row), False
            open_id = data["pending"].get(task_id)
            if open_id is not None:
                return _view(data["requests"][open_id]), False
            latest: dict[str, Any] | None = None
            for row in data["requests"].values():
                if row["task_id"] != task_id or row["status"] == PENDING:
                    continue
                if latest is None or _newer(row, latest):
                    latest = row
            if latest is None:
                return _none(task_id), False
            return _view(latest), False

        return self._transaction(read, create=False)

    def list_requests(
        self,
        *,
        task_id: str | None = None,
        state: str = "all",
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """List stored requests. A missing ledger is an empty list and is not created."""
        state = _list_state(state)
        if task_id is not None:
            task_id = _task_id(task_id)
        limit = _limit(limit)
        if not self.path.exists():
            return {"count": 0, "requests": []}

        def read(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            rows = []
            for key in sorted(data["requests"]):
                row = data["requests"][key]
                if task_id is not None and row["task_id"] != task_id:
                    continue
                if state != "all" and row["status"] != state:
                    continue
                rows.append(_view(row))
            rows.sort(key=lambda item: (item["asked_at"] or "", item["request_id"] or ""))
            shown = rows[:limit]
            body: dict[str, Any] = {"count": len(shown), "requests": shown}
            if len(rows) > len(shown):
                body["total"] = len(rows)
                body["truncated"] = True
            return body, False

        return self._transaction(read, create=False)

    def _now(self) -> datetime:
        current = self._clock()
        if not isinstance(current, datetime):
            raise ApprovalError("clock must return a datetime")
        if current.tzinfo is None:
            raise ApprovalError("clock must return a timezone-aware datetime")
        current = current.astimezone(timezone.utc)
        return current.replace(microsecond=(current.microsecond // 1000) * 1000)

    def _transaction(
        self,
        fn: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]],
        *,
        create: bool,
    ) -> dict[str, Any]:
        if not create and not self.path.exists():
            raise ApprovalError(f"no approval ledger at {self.path}")
        lock_path = Path(str(self.path) + ".lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise ApprovalError(f"cannot use ledger {self.path}: {exc.strerror}") from exc
        acquired = False
        try:
            _acquire_lock(lock_fd, lock_path)
            acquired = True
            data = self._load(create=create)
            result, dirty = fn(data)
            if dirty:
                ordered = {
                    "version": 1,
                    "requests": data["requests"],
                    "pending": data["pending"],
                }
                _validate_ledger(ordered, self.path)
                _atomic_write(
                    self.path,
                    json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
                )
            return result
        finally:
            if acquired:
                _release_lock(lock_fd, lock_path)
            os.close(lock_fd)

    def _load(self, *, create: bool) -> dict[str, Any]:
        if self.path.is_dir():
            raise ApprovalError(f"ledger path is a directory: {self.path}")
        if not self.path.exists():
            if not create:
                raise ApprovalError(f"no approval ledger at {self.path}")
            return {"version": 1, "requests": {}, "pending": {}}
        size = self.path.stat().st_size
        if size > MAX_LEDGER_BYTES:
            raise ApprovalError(
                f"ledger {self.path} is {size} bytes; refusing to load past {MAX_LEDGER_BYTES}"
            )
        try:
            raw = self.path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ApprovalError(f"ledger {self.path} is not UTF-8") from exc
        if not raw.strip():
            raise ApprovalError(f"ledger {self.path} is empty")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApprovalError(f"ledger {self.path} is not valid JSON") from exc
        _validate_ledger(data, self.path)
        return data


def resolve_ledger(root: Path, explicit: str | None) -> Path:
    """Pick the ledger file. Relative paths resolve against ``root``, not a later cwd.

    An explicit path may use ``..``. ``$ASK_HUMAN_LEDGER`` may not leave ``root``
    unless it is absolute. The default is ``.agent-toolkit/approvals.json``.
    """
    if explicit is not None and str(explicit).strip() != "":
        return _place(root, str(explicit).strip(), escape=True)
    env = os.environ.get("ASK_HUMAN_LEDGER")
    if env is not None and env.strip() != "":
        return _place(root, env.strip(), escape=False)
    return _place(root, DEFAULT_LEDGER_REL.as_posix(), escape=False)


def cli_root(value: str | None) -> Path:
    """The directory relative ledger paths resolve against. Captured absolutely."""
    if value is None or value.strip() == "":
        return Path.cwd()
    return Path(os.path.abspath(value.strip()))


def _validate_ledger(data: Any, path: Path) -> None:
    if not isinstance(data, dict) or set(data) != _TOP_KEYS:
        raise ApprovalError(
            f"ledger {path} is not an approval file (expected version, requests, and pending)"
        )
    if data.get("version") != 1:
        raise ApprovalError(f"ledger {path} has an unsupported version")
    requests = data.get("requests")
    pending = data.get("pending")
    if not isinstance(requests, dict) or not isinstance(pending, dict):
        raise ApprovalError(f"ledger {path} is not an approval file")
    seen_pending: dict[str, str] = {}
    for key, row in requests.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            raise ApprovalError(f"ledger {path} has a corrupt request")
        _validate_row(key, row, path)
        if row["status"] == PENDING:
            task_id = row["task_id"]
            if task_id in seen_pending:
                raise ApprovalError(f"ledger {path} has two pending requests for {task_id!r}")
            seen_pending[task_id] = key
    if set(pending) != set(seen_pending) or any(
        pending.get(task) != request_id for task, request_id in seen_pending.items()
    ):
        raise ApprovalError(f"ledger {path} pending index does not match the requests")
    for task, request_id in pending.items():
        if not isinstance(task, str) or not isinstance(request_id, str):
            raise ApprovalError(f"ledger {path} pending index is corrupt")


def _validate_row(key: str, row: dict[str, Any], path: Path) -> None:
    if set(row) != _ROW_KEYS or row.get("request_id") != key:
        raise ApprovalError(f"ledger {path} request {key!r} is corrupt")
    if _REQUEST.fullmatch(key) is None or _TASK.fullmatch(str(row.get("task_id", ""))) is None:
        raise ApprovalError(f"ledger {path} request {key!r} is corrupt")
    status = row.get("status")
    if status not in {PENDING, APPROVED, DENIED}:
        raise ApprovalError(f"ledger {path} request {key!r} has a bad status")
    for name, limit, empty_ok in (
        ("question", MAX_QUESTION, False),
        ("action", MAX_ACTION, False),
        ("note", MAX_NOTE, True),
        ("by", MAX_BY, True),
        ("answer_note", MAX_NOTE, True),
    ):
        value = row.get(name)
        if not isinstance(value, str) or (not empty_ok and not value.strip()):
            raise ApprovalError(f"ledger {path} request {key!r} has a bad {name}")
        if len(value) > limit or any(ord(ch) < 32 for ch in value):
            raise ApprovalError(f"ledger {path} request {key!r} has a bad {name}")
    _parse_time(row.get("asked_at"), key, path)
    answered = row.get("answered_at")
    if status == PENDING:
        if answered is not None or row["by"] != "" or row["answer_note"] != "":
            raise ApprovalError(f"ledger {path} request {key!r} is pending but already answered")
        return
    if not isinstance(answered, str):
        raise ApprovalError(f"ledger {path} request {key!r} is missing answered_at")
    _parse_time(answered, key, path)


def _outcome(result: str, ok: bool, row: dict[str, Any]) -> dict[str, Any]:
    body = _view(row)
    body["result"] = result
    body["ok"] = ok
    return body


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "request_id": row["request_id"],
        "status": row["status"],
        "question": row["question"],
        "action": row["action"],
        "asked_at": row["asked_at"],
        "answered_at": row["answered_at"],
        "by": row["by"],
        "note": row["note"],
        "answer_note": row["answer_note"],
    }


def _none(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "request_id": None,
        "status": NONE,
        "question": None,
        "action": None,
        "asked_at": None,
        "answered_at": None,
        "by": "",
        "note": "",
        "answer_note": "",
    }


def _absent(task_id: str, request_id: str) -> dict[str, Any]:
    body = _none(task_id)
    body["request_id"] = request_id
    body["result"] = "absent"
    body["ok"] = False
    body["status"] = NONE
    return body


def _newer(row: dict[str, Any], latest: dict[str, Any]) -> bool:
    left = (row["answered_at"], row["request_id"])
    right = (latest["answered_at"], latest["request_id"])
    return left > right


def _new_id(requests: dict[str, Any]) -> str:
    for _ in range(5):
        request_id = "req_" + secrets.token_hex(16)
        if request_id not in requests:
            return request_id
    raise ApprovalError("could not allocate a request id")


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or _TASK.fullmatch(value) is None:
        raise ApprovalError(
            "task_id must be 1–128 characters: letters, digits, and . _ : - "
            "(not starting with a symbol)"
        )
    return value


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or _REQUEST.fullmatch(value) is None:
        raise ApprovalError("request_id must look like req_ followed by 32 hex characters")
    return value


def _decision(value: Any) -> str:
    if not isinstance(value, str) or value not in DECISIONS:
        raise ApprovalError("decision must be approved or denied")
    return value


def _bounded(name: str, value: Any, limit: int, *, empty_ok: bool) -> str:
    if value is None and empty_ok:
        return ""
    if not isinstance(value, str):
        raise ApprovalError(f"{name} must be a string")
    if any(ord(ch) < 32 for ch in value):
        raise ApprovalError(f"{name} must not contain control characters")
    text = value.strip()
    if not text and not empty_ok:
        raise ApprovalError(f"{name} must not be empty")
    if len(text) > limit:
        raise ApprovalError(f"{name} must be at most {limit} characters")
    return text


def _list_state(value: Any) -> str:
    if value is None or value == "":
        return "all"
    if not isinstance(value, str):
        raise ApprovalError("state must be a string")
    name = value.strip().lower()
    if name not in _LIST_STATES:
        raise ApprovalError("state must be one of: pending, approved, denied, all")
    return name


def _limit(value: Any) -> int:
    if type(value) is bool or type(value) is not int:
        raise ApprovalError("limit must be an integer")
    if value < 1 or value > MAX_LIMIT:
        raise ApprovalError(f"limit must be from 1 to {MAX_LIMIT}")
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(value: Any, request_id: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise ApprovalError(f"ledger {path} request {request_id!r} has a bad timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApprovalError(
            f"ledger {path} request {request_id!r} has a bad timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ApprovalError(f"ledger {path} request {request_id!r} has a bad timestamp")
    return parsed.astimezone(timezone.utc)


def _place(root: Path, value: str, *, escape: bool) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if escape:
        return Path(os.path.abspath(os.path.join(str(root), value)))
    return _under_root(root, path)


def _under_root(root: Path, path: Path) -> Path:
    """Join ``path`` onto ``root`` and refuse to climb above ``root``."""
    base_parts = list(root.parts)
    stack = list(base_parts)
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if len(stack) <= len(base_parts):
                raise ApprovalError("path escapes the root")
            stack.pop()
            continue
        stack.append(part)
    if len(stack) < len(base_parts):
        raise ApprovalError("path escapes the root")
    return Path(*stack)


def _atomic_write(path: Path, text: str) -> None:
    tmp = Path(str(path) + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _acquire_lock(lock_fd: int, lock_path: Path) -> None:
    if fcntl is not None:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return
    sidecar = Path(str(lock_path) + ".win")
    deadline = time.monotonic() + 5
    while True:
        try:
            fd = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ApprovalError(f"timed out waiting for ledger lock {lock_path}")
            time.sleep(0.02)
            continue
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return


def _release_lock(lock_fd: int, lock_path: Path) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    sidecar = Path(str(lock_path) + ".win")
    try:
        sidecar.unlink()
    except OSError:
        pass


# --- CLI -------------------------------------------------------------------


def format_human(payload: dict[str, Any]) -> str:
    if "requests" in payload and "result" not in payload and "status" not in payload:
        return _format_list(payload)
    lines = []
    if "result" in payload:
        lines.append(f"{'result':<10}  {payload['result']}")
    if "status" in payload:
        lines.append(f"{'status':<10}  {payload['status']}")
    for key, label in (
        ("task_id", "task"),
        ("request_id", "request"),
        ("question", "question"),
        ("action", "action"),
        ("by", "by"),
        ("note", "note"),
        ("answer_note", "answer"),
    ):
        value = payload.get(key)
        if value:
            lines.append(f"{label:<10}  {value}")
    return "\n".join(lines) + "\n"


def _format_list(payload: dict[str, Any]) -> str:
    lines = [f"{'count':<10}  {payload['count']}"]
    rows = payload["requests"]
    if not rows:
        lines.append(f"{'requests':<10}  none")
    for row in rows:
        lines.append(
            f"{'':<10}  {row['request_id']}  {row['task_id']}  {row['status']}"
        )
    if payload.get("truncated"):
        lines.append(f"{'total':<10}  {payload['total']}")
    return "\n".join(lines) + "\n"


def format_explain(rules: dict[str, Any]) -> str:
    lines = [f"ask-human {rules['version']}", ""]
    lines.append(f"{'status':<16}{', '.join(rules['statuses'])}")
    lines.append(f"{'decision':<16}{', '.join(rules['decisions'])}")
    lines.append(f"{'result':<16}what ask and answer return")
    for item in rules["results"]:
        lines.append(f"{'':<16}{item['name']:<16}{item['meaning']}")
    lines.append("")
    lines.append(rules["trust"])
    lines.append("")
    lines.append(f"{'exit':<16}0 ok, 3 refused, 1 error")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ask-human",
        description=(
            "Record a human approval for one task. The ledger stores the "
            "question and the answer. It does not check who answered, and "
            "it does not resume work."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes for ask and answer:\n"
            "  0  ok (asked, approved, denied)\n"
            "  3  refused (pending-exists, already-decided, mismatch, absent)\n"
            "  1  error\n"
            "\n"
            "status, list, and explain exit 0. A pending request must be\n"
            "answered by its request id. A later ask is a different id.\n"
            "An approval does not raise a budget or run the next step.\n"
            "\n"
            "Ledger default: .agent-toolkit/approvals.json under --root,\n"
            "or $ASK_HUMAN_LEDGER. Relative paths resolve against --root."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ask-human {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_place(cmd: argparse.ArgumentParser, *, json_flag: bool) -> None:
        cmd.add_argument(
            "--root",
            default=None,
            help="directory a relative ledger path is resolved against (default: working directory)",
        )
        cmd.add_argument(
            "--ledger",
            default=None,
            help="JSON ledger path, absolute or relative to --root",
        )
        if json_flag:
            cmd.add_argument("--json", action="store_true", help="print the result as JSON")

    ask_cmd = sub.add_parser("ask", help="file a pending question for a task")
    add_place(ask_cmd, json_flag=True)
    ask_cmd.add_argument("task")
    ask_cmd.add_argument("--question", required=True, help="the question shown to the human")
    ask_cmd.add_argument("--action", required=True, help="the step this answer is allowed to cover")
    ask_cmd.add_argument("--note", default="", help="short note stored with the request")

    answer_cmd = sub.add_parser("answer", help="record approved or denied for one request id")
    add_place(answer_cmd, json_flag=True)
    answer_cmd.add_argument("task")
    answer_cmd.add_argument("--request-id", required=True, help="id returned by ask")
    answer_cmd.add_argument("--decision", required=True, choices=DECISIONS)
    answer_cmd.add_argument("--question", required=True, help="must match the stored question")
    answer_cmd.add_argument("--action", required=True, help="must match the stored action")
    answer_cmd.add_argument("--by", default="", help="label stored with the answer, not an identity check")
    answer_cmd.add_argument("--note", default="", help="short note stored with the answer")

    status_cmd = sub.add_parser("status", help="read the open request, or a specific id")
    add_place(status_cmd, json_flag=True)
    status_cmd.add_argument("task")
    status_cmd.add_argument("--request-id", default=None, help="read this request instead of the latest")

    list_cmd = sub.add_parser("list", help="list stored requests")
    add_place(list_cmd, json_flag=True)
    list_cmd.add_argument(
        "--state",
        default="all",
        choices=sorted(_LIST_STATES),
        help="all (default), pending, approved, or denied",
    )
    list_cmd.add_argument("--task", default=None, help="only this task")
    list_cmd.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    explain_cmd = sub.add_parser("explain", help="print statuses, results, and the trust limit")
    explain_cmd.add_argument("--json", action="store_true", help="print the rules as JSON")

    serve_cmd = sub.add_parser("serve", help="MCP server on stdio (one JSON-RPC message per line)")
    add_place(serve_cmd, json_flag=False)
    return parser


def _render(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(format_human(payload))


def _exit_for(payload: dict[str, Any]) -> int:
    if "ok" not in payload:
        return 0
    return 0 if payload["ok"] else 3


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "explain":
            rules = explain_rules()
            if args.json:
                sys.stdout.write(json.dumps(rules, indent=2, ensure_ascii=False) + "\n")
            else:
                sys.stdout.write(format_explain(rules))
            return 0
        root = cli_root(getattr(args, "root", None))
        ledger = resolve_ledger(root, getattr(args, "ledger", None))
        if args.command == "serve":
            return serve(_Defaults(root, ledger))
        book = ApprovalLedger(ledger)
        if args.command == "ask":
            payload = book.ask(args.task, args.question, action=args.action, note=args.note)
        elif args.command == "answer":
            payload = book.answer(
                args.task,
                args.request_id,
                args.decision,
                question=args.question,
                action=args.action,
                by=args.by,
                note=args.note,
            )
        elif args.command == "status":
            payload = book.status(args.task, request_id=args.request_id)
        elif args.command == "list":
            payload = book.list_requests(task_id=args.task, state=args.state, limit=args.limit)
        else:  # pragma: no cover - argparse requires a command
            parser.error(f"unknown command {args.command}")
            return 2
    except ApprovalError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except BrokenPipeError:
        return 0
    _render(payload, as_json=args.json)
    return _exit_for(payload)


# --- MCP (stdio, newline-delimited JSON-RPC) --------------------------------

_MCP_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
_MCP_NEWEST = _MCP_PROTOCOLS[-1]
_MCP_INSTRUCTIONS = (
    "Local approval ledger. approval_ask files one pending question for a "
    "task_id, with the action that question covers. approval_answer records "
    "approved or denied for that request_id only, and the question and action "
    "must match the stored text. A later ask is a new id. An old id does not "
    "approve it. approval_status reads the open request, or one id. "
    "approval_list lists stored requests. This is a local record, not proof "
    "of who answered. A caller who can use approval_answer can store approved. "
    "Restrict that tool. Approval does not raise a budget or resume work. "
    "Relative ledger and root paths resolve against the server --root, not "
    "the process cwd."
)
_TOOL_NAMES = {
    "approval_ask",
    "approval_answer",
    "approval_status",
    "approval_list",
    "approval_explain",
}


def _place_props() -> dict[str, Any]:
    return {
        "root": {
            "type": "string",
            "description": "Resolve a relative ledger against this directory. Relative to the server --root.",
        },
        "ledger": {
            "type": "string",
            "description": "Ledger JSON path. Relative paths resolve against root, not the process cwd.",
        },
    }


def _tool_defs() -> list[dict[str, Any]]:
    task = {"type": "string", "description": "Task id, such as fix-flaky-test."}
    question = {"type": "string", "description": "Question shown to the human. Must match on answer."}
    action = {"type": "string", "description": "Step this request covers. Must match on answer."}
    request = {"type": "string", "description": "Request id returned by approval_ask."}
    note = {"type": "string", "description": f"Short note, at most {MAX_NOTE} characters."}
    return [
        {
            "name": "approval_ask",
            "description": (
                "File one pending approval request. Does not approve it. "
                "A second pending ask for the same task is refused."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task,
                    "question": question,
                    "action": action,
                    "note": note,
                    **_place_props(),
                },
                "required": ["task_id", "question", "action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "approval_answer",
            "description": (
                "Record approved or denied for one request id. The question and "
                "action must match the ask. Does not resume work or raise a budget."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task,
                    "request_id": request,
                    "decision": {"type": "string", "enum": ["approved", "denied"]},
                    "question": question,
                    "action": action,
                    "by": {
                        "type": "string",
                        "description": "Label stored with the answer. Not an identity check.",
                    },
                    "note": note,
                    **_place_props(),
                },
                "required": ["task_id", "request_id", "decision", "question", "action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "approval_status",
            "description": (
                "Read the pending request for a task, or the latest decision, "
                "or one request id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task,
                    "request_id": request,
                    **_place_props(),
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "approval_list",
            "description": "List stored requests. Filter by task_id and status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task,
                    "state": {
                        "type": "string",
                        "enum": ["pending", "approved", "denied", "all"],
                        "description": "Default all.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "description": f"Max rows. Default {DEFAULT_LIMIT}.",
                    },
                    **_place_props(),
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "approval_explain",
            "description": "List statuses, results, and the trust limit of this ledger.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def _tool_result(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _protocol(requested: Any) -> str:
    if isinstance(requested, str) and requested in _MCP_PROTOCOLS:
        return requested
    return _MCP_NEWEST


def _reject_unknown(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ApprovalError(f"unknown field {unknown[0]!r}")


def _ledger_from_args(args: dict[str, Any], defaults: _Defaults) -> ApprovalLedger:
    root = defaults.root
    raw_root = args.get("root")
    if raw_root not in (None, ""):
        if not isinstance(raw_root, str):
            raise ApprovalError("root must be a string")
        root = _place(defaults.root, raw_root.strip(), escape=False)
    raw_ledger = args.get("ledger")
    if raw_ledger not in (None, ""):
        if not isinstance(raw_ledger, str):
            raise ApprovalError("ledger must be a string")
        path = _place(root, raw_ledger.strip(), escape=False)
    elif raw_root not in (None, ""):
        path = _place(root, DEFAULT_LEDGER_REL.as_posix(), escape=False)
    else:
        path = defaults.ledger
    return ApprovalLedger(path)


def _required_str(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise ApprovalError(f"{name} must be a string")
    return value


def _dispatch_tool(name: str, args: dict[str, Any], defaults: _Defaults) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ApprovalError("arguments must be an object")
    if name == "approval_explain":
        _reject_unknown(args, set())
        return _tool_result(explain_rules())
    if name == "approval_list":
        _reject_unknown(args, {"task_id", "state", "limit", "root", "ledger"})
        book = _ledger_from_args(args, defaults)
        task_id = args.get("task_id")
        if task_id is not None:
            task_id = _task_id(task_id)
        limit = DEFAULT_LIMIT if "limit" not in args or args["limit"] is None else args["limit"]
        return _tool_result(
            book.list_requests(task_id=task_id, state=args.get("state", "all"), limit=limit)
        )
    if name == "approval_ask":
        _reject_unknown(args, {"task_id", "question", "action", "note", "root", "ledger"})
        book = _ledger_from_args(args, defaults)
        return _tool_result(
            book.ask(
                _required_str(args, "task_id"),
                _required_str(args, "question"),
                action=_required_str(args, "action"),
                note="" if args.get("note") is None else args.get("note", ""),
            )
        )
    if name == "approval_answer":
        _reject_unknown(
            args,
            {"task_id", "request_id", "decision", "question", "action", "by", "note", "root", "ledger"},
        )
        book = _ledger_from_args(args, defaults)
        return _tool_result(
            book.answer(
                _required_str(args, "task_id"),
                _required_str(args, "request_id"),
                _required_str(args, "decision"),
                question=_required_str(args, "question"),
                action=_required_str(args, "action"),
                by="" if args.get("by") is None else args.get("by", ""),
                note="" if args.get("note") is None else args.get("note", ""),
            )
        )
    if name == "approval_status":
        _reject_unknown(args, {"task_id", "request_id", "root", "ledger"})
        book = _ledger_from_args(args, defaults)
        request_id = args.get("request_id")
        if request_id is not None:
            request_id = _request_id(request_id)
        return _tool_result(book.status(_required_str(args, "task_id"), request_id=request_id))
    raise ApprovalError(f"unknown tool {name!r}")


def handle_message(message: Any, defaults: _Defaults | None = None) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Notifications return None."""
    if defaults is None:
        root = Path.cwd()
        defaults = _Defaults(root, resolve_ledger(root, None))
    if not isinstance(message, dict) or message.get("jsonrpc") not in (None, "2.0"):
        return _rpc_error(
            message.get("id") if isinstance(message, dict) else None, -32600, "invalid request"
        )
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
        return result(
            {
                "protocolVersion": _protocol(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ask-human", "version": VERSION},
                "instructions": _MCP_INSTRUCTIONS,
            }
        )
    if method == "tools/call":
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            if not has_id:
                return None
            return _rpc_error(req_id, -32602, "tools/call requires a tool name")
        if tool_name not in _TOOL_NAMES:
            if not has_id:
                return None
            return _rpc_error(req_id, -32602, f"unknown tool {tool_name!r}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return result(_tool_error("arguments must be an object"))
        try:
            body = _dispatch_tool(tool_name, arguments, defaults)
        except ApprovalError as exc:
            body = _tool_error(str(exc))
        return result(body)
    if not has_id:
        return None
    return _rpc_error(req_id, -32601, f"method not found: {method}")


def serve(defaults: _Defaults | None = None) -> int:
    """Speak MCP over stdio. One JSON object per line. Logs never touch stdout."""
    if defaults is None:
        root = Path.cwd()
        defaults = _Defaults(root, resolve_ledger(root, None))
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
                sys.stderr.write(f"ask-human: {exc}\n")
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
