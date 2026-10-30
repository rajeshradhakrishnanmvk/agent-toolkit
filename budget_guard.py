#!/usr/bin/env python3
"""Budget guard for AI agents.

Declare a per-task budget (tokens, US dollars, tool calls, wall-clock).
Before a costly step, call check(). The answer is one of:

  continue
  warn                  near a limit (default: 80%), still ok to proceed
  stop-and-ask-human    the step would pass a limit, or one is already past

After an allowed step, call record() with the actual spend. Totals live in
a JSON ledger so they survive restarts.

check() does not spend. record() always writes what you pass, even if the
new total is over budget: the ledger is what happened, the decision is
advice. This process cannot force another program to stop. The harness
should refuse the next step when the decision is stop-and-ask-human.

Two checks do not stack. Each check is a hypothetical on committed spend
until you record.

Prices are not built in. Pass cost_usd from rates you looked up, or use
estimate_cost_usd() / the `cost` command with your own per-million rates.
Nothing in this file is a quote from a model provider.

No third-party dependencies. Python 3.9+.

  python3 budget_guard.py open TASK --max-tokens 2500 --max-tool-calls 4
  python3 budget_guard.py check TASK --tokens 600 --tool-calls 1
  python3 budget_guard.py record TASK --tokens 600 --tool-calls 1 --note read_file
  python3 budget_guard.py status TASK
  python3 budget_guard.py serve
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

VERSION = "0.1.0"
CONTINUE = "continue"
WARN = "warn"
STOP_AND_ASK = "stop-and-ask-human"

DEFAULT_WARN_RATIO = 0.8
DEFAULT_LEDGER_PATH = Path(".agent-toolkit") / "budget-ledger.json"
MAX_LEDGER_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 100
MAX_NOTE_LEN = 200

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DECISIONS = (CONTINUE, WARN, STOP_AND_ASK)
_RANK = {CONTINUE: 0, WARN: 1, STOP_AND_ASK: 2}

# name, limits key, kind ("int" | "cost" | "seconds")
_DIMENSIONS = (
    ("tokens", "max_tokens", "int"),
    ("cost_usd", "max_cost_usd", "cost"),
    ("tool_calls", "max_tool_calls", "int"),
    ("elapsed_seconds", "max_wall_clock_seconds", "seconds"),
)
_LABELS = {
    "tokens": "tokens",
    "cost_usd": "cost",
    "tool_calls": "tool calls",
    "elapsed_seconds": "wall-clock",
}

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


class BudgetError(Exception):
    """A budget the caller can fix: bad input, unknown task, or a broken ledger."""


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    usd_per_million_input: float,
    usd_per_million_output: float,
) -> float:
    """Convert token counts to USD using rates the caller supplies.

    This does not look up provider prices. Pass the numbers you verified.
    """
    in_tok = _as_int("input_tokens", input_tokens)
    out_tok = _as_int("output_tokens", output_tokens)
    in_rate = _as_rate("usd_per_million_input", usd_per_million_input)
    out_rate = _as_rate("usd_per_million_output", usd_per_million_output)
    return round((in_tok * in_rate + out_tok * out_rate) / 1_000_000, 8)


class BudgetGuard:
    """Per-task budgets stored in one JSON ledger.

    The ledger file is replaced atomically. A sibling ``<ledger>.lock`` file
    serializes readers and writers across threads and processes. Wall-clock
    is real time since the task was opened, including time the agent was
    idle, and it keeps moving across restarts.
    """

    def __init__(
        self,
        ledger_path: str | Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        max_events: int = MAX_EVENTS,
    ) -> None:
        self.path = Path(ledger_path) if ledger_path is not None else _default_ledger()
        self._clock = clock or _utcnow
        if type(max_events) is not int or max_events < 1:
            raise BudgetError("max_events must be an integer >= 1")
        self.max_events = max_events

    def open_task(
        self,
        task_id: str,
        *,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_tool_calls: int | None = None,
        max_wall_clock_seconds: float | None = None,
        warn_ratio: float = DEFAULT_WARN_RATIO,
        reset: bool = False,
    ) -> dict[str, Any]:
        """Declare a budget. Fails if the task exists unless reset=True.

        reset=True drops spend and starts the wall-clock over. At least one
        limit is required. Each limit must be greater than zero.
        """
        task_id = _task_id(task_id)
        limits = _clean_limits(
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
            max_tool_calls=max_tool_calls,
            max_wall_clock_seconds=max_wall_clock_seconds,
            warn_ratio=warn_ratio,
        )

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            tasks = data["tasks"]
            if task_id in tasks and not reset:
                raise BudgetError(
                    f"task {task_id!r} already has a budget; "
                    "pass reset=True to replace it (this clears spend)"
                )
            now = _iso(self._now())
            tasks[task_id] = {
                "task_id": task_id,
                "opened_at": now,
                "updated_at": now,
                "limits": limits,
                "spent": {"tokens": 0, "cost_usd": 0.0, "tool_calls": 0},
                "events": [],
                "events_dropped": 0,
            }
            return self._view(tasks[task_id], proposed=_zero_spend())

        return self._transaction(mutate, create=True, write=True)

    def check(
        self,
        task_id: str,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        tool_calls: int = 0,
    ) -> dict[str, Any]:
        """Ask whether a proposed spend may proceed. Does not write.

        proposed spend is added to the committed total only inside this
        answer. Call again with zeros to re-check the current total,
        including wall-clock.
        """
        task_id = _task_id(task_id)
        proposed = _clean_spend(tokens, cost_usd, tool_calls)

        def read(data: dict[str, Any]) -> dict[str, Any]:
            task = self._require(data, task_id)
            return self._view(task, proposed=proposed)

        return self._transaction(read, create=False, write=False)

    def record(
        self,
        task_id: str,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        tool_calls: int = 0,
        note: str = "",
    ) -> dict[str, Any]:
        """Add actual spend to the ledger, then report the new decision.

        This is accounting, not permission. Call check() first. A record
        past the limit is kept (it really happened) and comes back as
        stop-and-ask-human.
        """
        task_id = _task_id(task_id)
        spend = _clean_spend(tokens, cost_usd, tool_calls)
        note = _clean_note(note)

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = self._require(data, task_id)
            if spend["tokens"] or spend["cost_usd"] or spend["tool_calls"] or note:
                task["spent"] = {
                    "tokens": task["spent"]["tokens"] + spend["tokens"],
                    "cost_usd": round(task["spent"]["cost_usd"] + spend["cost_usd"], 8),
                    "tool_calls": task["spent"]["tool_calls"] + spend["tool_calls"],
                }
                task["events"].append(
                    {
                        "at": _iso(self._now()),
                        "tokens": spend["tokens"],
                        "cost_usd": spend["cost_usd"],
                        "tool_calls": spend["tool_calls"],
                        "note": note,
                    }
                )
                overflow = len(task["events"]) - self.max_events
                if overflow > 0:
                    del task["events"][:overflow]
                    task["events_dropped"] = int(task.get("events_dropped", 0)) + overflow
                task["updated_at"] = _iso(self._now())
            return self._view(task, proposed=_zero_spend())

        return self._transaction(mutate, create=False, write=True)

    def status(self, task_id: str) -> dict[str, Any]:
        """Committed spend, limits, and the decision for the current total."""
        task_id = _task_id(task_id)

        def read(data: dict[str, Any]) -> dict[str, Any]:
            return self._view(self._require(data, task_id), proposed=_zero_spend())

        return self._transaction(read, create=False, write=False)

    def _now(self) -> datetime:
        current = self._clock()
        if not isinstance(current, datetime):
            raise BudgetError("clock must return a datetime")
        if current.tzinfo is None:
            raise BudgetError("clock must return a timezone-aware datetime")
        return current.astimezone(timezone.utc)

    def _require(self, data: dict[str, Any], task_id: str) -> dict[str, Any]:
        try:
            task = data["tasks"][task_id]
        except KeyError as exc:
            known = ", ".join(sorted(data["tasks"])) or "(none)"
            raise BudgetError(f"unknown task {task_id!r}; open tasks: {known}") from exc
        if not isinstance(task, dict) or "spent" not in task or "limits" not in task:
            raise BudgetError(f"task {task_id!r} in {self.path} is corrupt")
        return task

    def _view(self, task: dict[str, Any], *, proposed: dict[str, float | int]) -> dict[str, Any]:
        limits = task["limits"]
        spent = task["spent"]
        opened = _parse_time(task["opened_at"])
        elapsed = max(0.0, (self._now() - opened).total_seconds())
        projected = {
            "tokens": int(spent["tokens"]) + int(proposed["tokens"]),
            "cost_usd": round(float(spent["cost_usd"]) + float(proposed["cost_usd"]), 8),
            "tool_calls": int(spent["tool_calls"]) + int(proposed["tool_calls"]),
            "elapsed_seconds": elapsed,
        }
        committed = {
            "tokens": int(spent["tokens"]),
            "cost_usd": float(spent["cost_usd"]),
            "tool_calls": int(spent["tool_calls"]),
            "elapsed_seconds": elapsed,
        }
        warn_ratio = float(limits["warn_ratio"])
        dimensions = []
        for name, limit_key, kind in _DIMENSIONS:
            limit = limits.get(limit_key)
            value = projected[name]
            already = committed[name]
            if limit is None:
                state = CONTINUE
                ratio = None
                remaining = None
            else:
                limit_n = float(limit) if kind != "int" else int(limit)
                # Decimal, not binary float: 0.04 / 0.05 is exactly 80%.
                exact = Decimal(str(value)) / Decimal(str(limit_n)) if limit_n else None
                ratio = float(exact) if exact is not None else None
                if _is_over(kind, value, limit_n):
                    state = STOP_AND_ASK
                elif exact is not None and exact >= Decimal(str(warn_ratio)):
                    state = WARN
                else:
                    state = CONTINUE
                remaining = (
                    round(limit_n - value, 8) if kind == "cost" else limit_n - value
                )
            dimensions.append(
                {
                    "name": name,
                    "label": _LABELS[name],
                    "limit": limit,
                    "spent": already,
                    "projected": value,
                    "remaining": remaining,
                    "ratio": None if ratio is None else round(ratio, 6),
                    "state": state,
                }
            )
        decision = max((d["state"] for d in dimensions), key=_RANK.__getitem__)
        reason = _reason(decision, dimensions, warn_ratio, proposed)
        ask = None
        if decision == STOP_AND_ASK:
            detail = reason.split(": ", 1)[1]
            ask = (
                f"Task {task['task_id']} is over budget: {detail}. "
                "Ask a human before the next costly step."
            )
        return {
            "task_id": task["task_id"],
            "decision": decision,
            "ok_to_proceed": decision != STOP_AND_ASK,
            "reason": reason,
            "ask_human": ask,
            "opened_at": task["opened_at"],
            "elapsed_seconds": round(elapsed, 3),
            "limits": {
                "max_tokens": limits.get("max_tokens"),
                "max_cost_usd": limits.get("max_cost_usd"),
                "max_tool_calls": limits.get("max_tool_calls"),
                "max_wall_clock_seconds": limits.get("max_wall_clock_seconds"),
                "warn_ratio": warn_ratio,
            },
            "spent": {
                "tokens": int(spent["tokens"]),
                "cost_usd": float(spent["cost_usd"]),
                "tool_calls": int(spent["tool_calls"]),
            },
            "proposed": {
                "tokens": int(proposed["tokens"]),
                "cost_usd": float(proposed["cost_usd"]),
                "tool_calls": int(proposed["tool_calls"]),
            },
            "projected": projected,
            "dimensions": dimensions,
            "event_count": len(task.get("events", [])),
            "events_dropped": int(task.get("events_dropped", 0)),
            "ledger": str(self.path),
        }

    def _transaction(
        self,
        fn: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        create: bool,
        write: bool,
    ) -> dict[str, Any]:
        # A read of a missing ledger must not create directories or files.
        if not create and not self.path.exists():
            raise BudgetError(f"no budget ledger at {self.path}; open a task first")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(self.path) + ".lock")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        acquired = False
        try:
            _acquire_lock(lock_fd, lock_path)
            acquired = True
            data = self._load(create=create)
            result = fn(data)
            if write:
                if "version" not in data:
                    data["version"] = 1
                _atomic_write(self.path, json.dumps(data, indent=2) + "\n")
            return result
        finally:
            if acquired:
                _release_lock(lock_fd, lock_path)
            os.close(lock_fd)

    def _load(self, *, create: bool) -> dict[str, Any]:
        if not self.path.exists():
            if not create:
                raise BudgetError(
                    f"no budget ledger at {self.path}; open a task first"
                )
            return {"version": 1, "tasks": {}}
        size = self.path.stat().st_size
        if size > MAX_LEDGER_BYTES:
            raise BudgetError(
                f"ledger {self.path} is {size} bytes; refusing to load past {MAX_LEDGER_BYTES}"
            )
        try:
            raw = self.path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise BudgetError(f"ledger {self.path} is not UTF-8") from exc
        if not raw.strip():
            data: Any = {"version": 1, "tasks": {}}
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BudgetError(f"ledger {self.path} is not valid JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
            raise BudgetError(
                f"ledger {self.path} is not a budget-guard file (expected a tasks object)"
            )
        return data


def _default_ledger() -> Path:
    override = os.environ.get("BUDGET_GUARD_LEDGER")
    if override:
        return Path(override)
    return DEFAULT_LEDGER_PATH


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BudgetError(f"bad timestamp in ledger: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or _TASK_ID.fullmatch(value) is None:
        raise BudgetError(
            "task_id must be 1–128 characters: letters, digits, and . _ : - "
            "(not starting with - or a symbol)"
        )
    return value


def _as_int(name: str, value: Any) -> int:
    if type(value) is bool:
        raise BudgetError(f"{name} must be an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise BudgetError(f"{name} must be an integer")
        value = int(value)
    if type(value) is not int:
        raise BudgetError(f"{name} must be an integer")
    if value < 0:
        raise BudgetError(f"{name} must be >= 0")
    return value


def _as_rate(name: str, value: Any) -> float:
    number = _as_float(name, value)
    if number < 0:
        raise BudgetError(f"{name} must be >= 0")
    return number


def _as_float(name: str, value: Any) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise BudgetError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise BudgetError(f"{name} must be finite")
    return number


def _positive_limit(name: str, value: Any, *, kind: str) -> int | float:
    if kind == "int":
        number: int | float = _as_int(name, value)
    else:
        number = _as_float(name, value)
    if number <= 0:
        raise BudgetError(f"{name} must be > 0")
    if kind == "cost":
        return round(float(number), 8)
    return number


def _clean_limits(
    *,
    max_tokens: Any,
    max_cost_usd: Any,
    max_tool_calls: Any,
    max_wall_clock_seconds: Any,
    warn_ratio: Any,
) -> dict[str, Any]:
    cleaned = {
        "max_tokens": None if max_tokens is None else _positive_limit("max_tokens", max_tokens, kind="int"),
        "max_cost_usd": None
        if max_cost_usd is None
        else _positive_limit("max_cost_usd", max_cost_usd, kind="cost"),
        "max_tool_calls": None
        if max_tool_calls is None
        else _positive_limit("max_tool_calls", max_tool_calls, kind="int"),
        "max_wall_clock_seconds": None
        if max_wall_clock_seconds is None
        else _positive_limit("max_wall_clock_seconds", max_wall_clock_seconds, kind="seconds"),
    }
    if all(value is None for value in cleaned.values()):
        raise BudgetError(
            "set at least one limit: max_tokens, max_cost_usd, "
            "max_tool_calls, max_wall_clock_seconds"
        )
    if type(warn_ratio) is bool or not isinstance(warn_ratio, (int, float)):
        raise BudgetError("warn_ratio must be a number")
    ratio = float(warn_ratio)
    if not math.isfinite(ratio) or ratio <= 0 or ratio > 1:
        raise BudgetError("warn_ratio must be > 0 and <= 1")
    cleaned["warn_ratio"] = ratio
    return cleaned


def _clean_spend(tokens: Any, cost_usd: Any, tool_calls: Any) -> dict[str, int | float]:
    return {
        "tokens": _as_int("tokens", tokens),
        "cost_usd": round(_as_rate("cost_usd", cost_usd), 8),
        "tool_calls": _as_int("tool_calls", tool_calls),
    }


def _zero_spend() -> dict[str, int | float]:
    return {"tokens": 0, "cost_usd": 0.0, "tool_calls": 0}


def _clean_note(note: Any) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise BudgetError("note must be a string")
    if any(ord(ch) < 32 for ch in note):
        raise BudgetError("note must not contain control characters")
    note = note.strip()
    if len(note) > MAX_NOTE_LEN:
        raise BudgetError(f"note must be at most {MAX_NOTE_LEN} characters")
    return note


def _is_over(kind: str, projected: float, limit: float) -> bool:
    if kind == "cost":
        # Same decimal reading as the warn line. Binary floats make
        # 0.04 and 0.05 look uneven.
        return Decimal(str(projected)) > Decimal(str(limit))
    if kind == "seconds":
        return projected - limit > 1e-3
    return projected > limit


def _reason(
    decision: str,
    dimensions: list[dict[str, Any]],
    warn_ratio: float,
    proposed: dict[str, Any],
) -> str:
    if decision == CONTINUE:
        return "continue: within budget"
    interesting = [d for d in dimensions if d["state"] == decision]
    parts = []
    for dim in interesting:
        label = dim["label"]
        limit_txt = _fmt_dim(dim["name"], dim["limit"])
        projected_txt = _fmt_dim(dim["name"], dim["projected"])
        if decision == STOP_AND_ASK:
            already_over = _is_over(_kind(dim["name"]), dim["spent"], dim["limit"])
            if already_over or dim["name"] == "elapsed_seconds":
                if already_over and dim["name"] in {"tokens", "tool_calls"}:
                    verb = "are already"
                elif already_over:
                    verb = "is already"
                else:
                    verb = "is"
                amount = _fmt_dim(
                    dim["name"], dim["spent"] if already_over else dim["projected"]
                )
                parts.append(f"{label} {verb} {amount}, over the limit of {limit_txt}")
            else:
                parts.append(
                    f"{label} would reach {projected_txt}, over the limit of {limit_txt}"
                )
        else:
            pct = _pct(dim["ratio"])
            hypothetical = _proposed_for(dim["name"], proposed) > 0
            verb = "would be at" if hypothetical else "at"
            parts.append(
                f"{label} {verb} {pct} of {limit_txt} (warn at {_pct(warn_ratio)})"
            )
    body = "; ".join(parts)
    if decision == STOP_AND_ASK:
        return f"stop and ask a human: {body}"
    return f"warn: {body}"


def _kind(name: str) -> str:
    for dim_name, _key, kind in _DIMENSIONS:
        if dim_name == name:
            return kind
    return "int"


def _proposed_for(name: str, proposed: dict[str, Any]) -> float:
    if name == "elapsed_seconds":
        return 0.0
    return float(proposed.get(name, 0) or 0)


def _fmt_dim(name: str, value: Any) -> str:
    if name == "cost_usd":
        return _money(float(value))
    if name == "elapsed_seconds":
        return _fmt_seconds(float(value))
    return _grouped(int(value))


def _grouped(value: int) -> str:
    # Always a comma. The ',' format is not locale-dependent (unlike 'n').
    return f"{value:,}"


def _fmt_seconds(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{round(value):.0f}s"
    return f"{value:.1f}s"


def _pct(ratio: float) -> str:
    percent = ratio * 100
    if abs(percent - round(percent)) < 0.05:
        return f"{round(percent):.0f}%"
    return f"{percent:.1f}%"


def _money(value: float) -> str:
    rounded = round(float(value), 4)
    sign = "-" if rounded < 0 else ""
    text = f"{abs(rounded):.4f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.split(".", 1)[1]) == 1:
        text += "0"
    return f"{sign}${text}"


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
    # No flock (Windows): a second exclusive file beside the flock target.
    sidecar = Path(str(lock_path) + ".win")
    deadline = time.monotonic() + 5
    while True:
        try:
            fd = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise BudgetError(f"timed out waiting for ledger lock {lock_path}")
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

_HUMAN_LABELS = {
    "tokens": "tokens",
    "cost_usd": "cost",
    "tool_calls": "calls",
    "elapsed_seconds": "elapsed",
}


def format_human(payload: dict[str, Any]) -> str:
    lines = [
        f"{'decision':<8}  {payload['decision']}",
        f"{'task':<8}  {payload['task_id']}",
        f"{'reason':<8}  {payload['reason']}",
    ]
    for dim in payload["dimensions"]:
        if dim["limit"] is None:
            continue
        lines.append(f"{_HUMAN_LABELS[dim['name']]:<8}  {_human_dim(dim)}")
    if payload.get("ask_human"):
        lines.append(f"{'ask':<8}  {payload['ask_human']}")
    return "\n".join(lines) + "\n"


def _human_dim(dim: dict[str, Any]) -> str:
    name = dim["name"]
    if name == "cost_usd":
        body = (
            f"{_money(dim['spent'])} spent, {_money(dim['projected'])} projected, "
            f"limit {_money(dim['limit'])}"
        )
    elif name == "elapsed_seconds":
        body = f"{_fmt_seconds(dim['projected'])} of {_fmt_seconds(dim['limit'])}"
    else:
        body = (
            f"{_grouped(int(dim['spent']))} spent, {_grouped(int(dim['projected']))} projected, "
            f"limit {_grouped(int(dim['limit']))}"
        )
    ratio = _pct(dim["ratio"]) if dim["ratio"] is not None else "n/a"
    return f"{body}  ({ratio})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="budget-guard",
        description=(
            "Per-task budget for an AI agent: tokens, USD, tool calls, and wall-clock. "
            "Prices are not built in; you pass cost and rates."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes for check, record, and status:\n"
            "  0  continue or warn\n"
            "  3  stop-and-ask-human\n"
            "  1  error\n"
            "\n"
            "Warn still proceeds. The guard does not kill your agent; stop when\n"
            "the decision is stop-and-ask-human.\n"
            "\n"
            "Ledger default: .agent-toolkit/budget-ledger.json or $BUDGET_GUARD_LEDGER."
        ),
    )
    parser.add_argument("--version", action="version", version=f"budget-guard {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_ledger(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument(
            "--ledger",
            default=None,
            help="JSON ledger path (default: .agent-toolkit/budget-ledger.json or $BUDGET_GUARD_LEDGER)",
        )
        cmd.add_argument("--json", action="store_true", help="print the decision as JSON")

    def add_spend(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--tokens", type=int, default=0, help="tokens (default 0)")
        cmd.add_argument("--cost-usd", type=float, default=0.0, help="US dollars (default 0)")
        cmd.add_argument("--tool-calls", type=int, default=0, help="tool calls (default 0)")

    open_cmd = sub.add_parser("open", help="declare a budget for a task")
    add_ledger(open_cmd)
    open_cmd.add_argument("task_id")
    open_cmd.add_argument("--max-tokens", type=int, default=None)
    open_cmd.add_argument("--max-cost-usd", type=float, default=None)
    open_cmd.add_argument("--max-tool-calls", type=int, default=None)
    open_cmd.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="wall-clock seconds since open, including idle time",
    )
    open_cmd.add_argument("--warn-ratio", type=float, default=DEFAULT_WARN_RATIO)
    open_cmd.add_argument(
        "--reset",
        action="store_true",
        help="replace an existing task and clear its spend",
    )

    check_cmd = sub.add_parser("check", help="ask before a step; does not record spend")
    add_ledger(check_cmd)
    check_cmd.add_argument("task_id")
    add_spend(check_cmd)

    record_cmd = sub.add_parser("record", help="add actual spend after an allowed step")
    add_ledger(record_cmd)
    record_cmd.add_argument("task_id")
    add_spend(record_cmd)
    record_cmd.add_argument("--note", default="", help="short label stored in the ledger")

    status_cmd = sub.add_parser("status", help="show committed spend and the current decision")
    add_ledger(status_cmd)
    status_cmd.add_argument("task_id")

    cost_cmd = sub.add_parser(
        "cost",
        help="convert tokens to USD with rates you supply (no built-in price list)",
    )
    cost_cmd.add_argument("--input-tokens", type=int, required=True)
    cost_cmd.add_argument("--output-tokens", type=int, required=True)
    cost_cmd.add_argument("--usd-per-million-input", type=float, required=True)
    cost_cmd.add_argument("--usd-per-million-output", type=float, required=True)
    cost_cmd.add_argument("--json", action="store_true")

    serve_cmd = sub.add_parser(
        "serve",
        help="MCP server on stdio (one JSON-RPC message per line)",
    )
    serve_cmd.add_argument("--ledger", default=None, help="default ledger for tool calls")
    return parser


def _render(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(format_human(payload))


def _exit_for(command: str, payload: dict[str, Any]) -> int:
    if command in {"check", "record", "status"} and payload["decision"] == STOP_AND_ASK:
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "cost":
            amount = estimate_cost_usd(
                args.input_tokens,
                args.output_tokens,
                args.usd_per_million_input,
                args.usd_per_million_output,
            )
            if args.json:
                body = {
                    "cost_usd": amount,
                    "input_tokens": args.input_tokens,
                    "output_tokens": args.output_tokens,
                    "usd_per_million_input": args.usd_per_million_input,
                    "usd_per_million_output": args.usd_per_million_output,
                    "note": "Computed from the rates you passed. This tool does not know provider prices.",
                }
                sys.stdout.write(json.dumps(body, indent=2) + "\n")
            else:
                sys.stdout.write(f"{amount}\n")
            return 0
        if args.command == "serve":
            return serve(_resolve_ledger(args.ledger))
        guard = BudgetGuard(_resolve_ledger(args.ledger))
        if args.command == "open":
            payload = guard.open_task(
                args.task_id,
                max_tokens=args.max_tokens,
                max_cost_usd=args.max_cost_usd,
                max_tool_calls=args.max_tool_calls,
                max_wall_clock_seconds=args.max_seconds,
                warn_ratio=args.warn_ratio,
                reset=args.reset,
            )
        elif args.command == "check":
            payload = guard.check(
                args.task_id,
                tokens=args.tokens,
                cost_usd=args.cost_usd,
                tool_calls=args.tool_calls,
            )
        elif args.command == "record":
            payload = guard.record(
                args.task_id,
                tokens=args.tokens,
                cost_usd=args.cost_usd,
                tool_calls=args.tool_calls,
                note=args.note,
            )
        elif args.command == "status":
            payload = guard.status(args.task_id)
        else:  # pragma: no cover - argparse requires a command
            parser.error(f"unknown command {args.command}")
            return 2
    except BudgetError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except BrokenPipeError:
        return 0
    _render(payload, as_json=args.json)
    return _exit_for(args.command, payload)


def _resolve_ledger(path: str | None) -> Path:
    if path:
        return Path(path)
    return _default_ledger()


# --- MCP (stdio, newline-delimited JSON-RPC) --------------------------------

_MCP_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
_MCP_INSTRUCTIONS = (
    "Budget guard for one task at a time. Call budget_open once with at least "
    "one limit (max_tokens, max_cost_usd, max_tool_calls, max_wall_clock_seconds). "
    "There is no built-in price list: pass cost_usd yourself. Before every costly "
    "step call budget_check with the spend you are about to incur. If decision is "
    "continue or warn, do the step, then budget_record the actual spend. warn means "
    "near the limit and still ok to proceed. If decision is stop-and-ask-human, do "
    "not take the step; show ask_human to the user. budget_check does not record "
    "spend. Two checks do not stack. budget_status reads the ledger."
)

_LIMIT_PROPS = {
    "max_tokens": {"type": "integer", "minimum": 1, "description": "Must be > 0."},
    "max_cost_usd": {"type": "number", "minimum": 0, "description": "US dollars. Must be > 0."},
    "max_tool_calls": {"type": "integer", "minimum": 1, "description": "Must be > 0."},
    "max_wall_clock_seconds": {
        "type": "number",
        "minimum": 0,
        "description": "Real seconds since budget_open, including idle time. Must be > 0.",
    },
    "warn_ratio": {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
        "description": "Warn when any limit reaches this fraction. Must be > 0 and <= 1. Default 0.8.",
    },
}


def _ledger_prop() -> dict[str, Any]:
    return {
        "type": "string",
        "description": "JSON ledger path. Defaults to the server --ledger path.",
    }


def _tool_defs() -> list[dict[str, Any]]:
    spend_props = {
        "tokens": {"type": "integer", "minimum": 0, "default": 0},
        "cost_usd": {"type": "number", "minimum": 0, "default": 0},
        "tool_calls": {"type": "integer", "minimum": 0, "default": 0},
        "ledger_path": _ledger_prop(),
    }
    return [
        {
            "name": "budget_open",
            "description": (
                "Declare the budget for a task before any work. At least one limit "
                "is required. Prices are not built in; pass cost_usd on later calls. "
                "reset=true replaces an existing task and clears its spend."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    **_LIMIT_PROPS,
                    "reset": {"type": "boolean", "default": False},
                    "ledger_path": _ledger_prop(),
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "budget_check",
            "description": (
                "Call before every costly step. Read-only: does not record spend. "
                "If decision is continue or warn, you may proceed, then budget_record "
                "the actual spend. If decision is stop-and-ask-human, do not take the "
                "step; show ask_human to the user."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    **spend_props,
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "budget_record",
            "description": (
                "Write actual spend after a step that budget_check allowed. "
                "This does not ask permission. Returns the decision for the new totals."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    **spend_props,
                    "note": {"type": "string", "description": "Short label, max 200 characters."},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "budget_status",
            "description": "Read committed spend, limits, and the current decision. Does not record anything.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "ledger_path": _ledger_prop(),
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    ]


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": False,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _guard_for(args: dict[str, Any], default_ledger: Path) -> BudgetGuard:
    raw = args.get("ledger_path")
    if raw is None or raw == "":
        return BudgetGuard(default_ledger)
    if not isinstance(raw, str):
        raise BudgetError("ledger_path must be a string")
    return BudgetGuard(raw)


def _reject_unknown(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise BudgetError(f"unknown field {unknown[0]!r}")


def _optional(args: dict[str, Any], key: str) -> Any:
    value = args.get(key)
    return None if value is None else value


def _dispatch_tool(name: str, args: dict[str, Any], default_ledger: Path) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise BudgetError("arguments must be an object")
    if name == "budget_open":
        _reject_unknown(
            args,
            {
                "task_id",
                "max_tokens",
                "max_cost_usd",
                "max_tool_calls",
                "max_wall_clock_seconds",
                "warn_ratio",
                "reset",
                "ledger_path",
            },
        )
        guard = _guard_for(args, default_ledger)
        warn = args.get("warn_ratio", DEFAULT_WARN_RATIO)
        reset = args.get("reset", False)
        if type(reset) is not bool:
            raise BudgetError("reset must be a boolean")
        payload = guard.open_task(
            args.get("task_id"),  # type: ignore[arg-type]
            max_tokens=_optional(args, "max_tokens"),
            max_cost_usd=_optional(args, "max_cost_usd"),
            max_tool_calls=_optional(args, "max_tool_calls"),
            max_wall_clock_seconds=_optional(args, "max_wall_clock_seconds"),
            warn_ratio=DEFAULT_WARN_RATIO if warn is None else warn,
            reset=reset,
        )
        return _tool_result(payload)
    if name == "budget_check":
        _reject_unknown(args, {"task_id", "tokens", "cost_usd", "tool_calls", "ledger_path"})
        guard = _guard_for(args, default_ledger)
        payload = guard.check(
            args.get("task_id"),  # type: ignore[arg-type]
            tokens=args.get("tokens", 0),
            cost_usd=args.get("cost_usd", 0.0),
            tool_calls=args.get("tool_calls", 0),
        )
        return _tool_result(payload)
    if name == "budget_record":
        _reject_unknown(
            args, {"task_id", "tokens", "cost_usd", "tool_calls", "note", "ledger_path"}
        )
        guard = _guard_for(args, default_ledger)
        payload = guard.record(
            args.get("task_id"),  # type: ignore[arg-type]
            tokens=args.get("tokens", 0),
            cost_usd=args.get("cost_usd", 0.0),
            tool_calls=args.get("tool_calls", 0),
            note=args.get("note", ""),
        )
        return _tool_result(payload)
    if name == "budget_status":
        _reject_unknown(args, {"task_id", "ledger_path"})
        guard = _guard_for(args, default_ledger)
        return _tool_result(guard.status(args.get("task_id")))  # type: ignore[arg-type]
    return _tool_error(f"unknown tool {name!r}")


def handle_message(message: Any, default_ledger: Path) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Notifications return None."""
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
                "serverInfo": {"name": "budget-guard", "version": VERSION},
                "instructions": _MCP_INSTRUCTIONS,
            }
        )
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return result(_tool_error("tools/call requires a tool name"))
        try:
            body = _dispatch_tool(name, arguments, default_ledger)
        except BudgetError as exc:
            body = _tool_error(str(exc))
        return result(body)
    if not has_id:
        return None
    return _rpc_error(req_id, -32601, f"method not found: {method}")


def serve(default_ledger: Path) -> int:
    """Speak MCP over stdio. One JSON object per line. Logs never touch stdout."""
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
                response = handle_message(message, default_ledger)
            except Exception as exc:  # noqa: BLE001 - keep the stdio loop alive
                req_id = message.get("id") if isinstance(message, dict) else None
                sys.stderr.write(f"budget-guard: {exc}\n")
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
