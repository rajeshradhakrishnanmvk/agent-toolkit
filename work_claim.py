#!/usr/bin/env python3
"""Work-claim ledger for AI agents.

When several agents share a repo, two of them will pick up the same task.
take() claims an item for an owner until a lease (a TTL) expires. A second
take is busy while that lease is running. After expires_at, the claim is
stale and the next owner can take it over.

renew() extends a lease you hold. release() gives the item back. done()
marks it finished. list_claims() shows open claims, including expired ones
that are free to take.

The ledger is one JSON file. A lock file plus an atomic replace keeps two
processes from claiming the same item. The owner id is a label, not a secret.

No third-party dependencies. Python 3.9+. No network calls.

  python3 work_claim.py take ITEM --owner AGENT --ttl 600
  python3 work_claim.py list
  python3 work_claim.py renew ITEM --owner AGENT
  python3 work_claim.py release ITEM --owner AGENT
  python3 work_claim.py done ITEM --owner AGENT
  python3 work_claim.py explain
  python3 work_claim.py serve
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

VERSION = "0.1.0"
DEFAULT_TTL = 900
MAX_TTL = 24 * 60 * 60
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_NOTE_LEN = 200
MAX_DONE = 100
MAX_CLAIMS = 1000
MAX_LEDGER_BYTES = 8 * 1024 * 1024
DEFAULT_LEDGER_REL = Path(".agent-toolkit") / "work-claims.json"

_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,159}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STATES = {"open", "active", "expired", "stale", "done", "all"}

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


class WorkClaimError(Exception):
    """A claim the caller can fix: bad input, or a broken ledger."""


class _Defaults(NamedTuple):
    root: Path
    ledger: Path


def explain_rules() -> dict[str, Any]:
    """The results, states, and lease limits, as data."""
    return {
        "tool": "work-claim",
        "version": VERSION,
        "results": [
            {"name": "taken", "ok": True, "meaning": "You hold the item. The lease starts now."},
            {
                "name": "taken-over",
                "ok": True,
                "meaning": "You hold it now. previous_owner names who had it.",
            },
            {"name": "held", "ok": True, "meaning": "You already hold it. Renew to extend."},
            {"name": "busy", "ok": False, "meaning": "Someone else holds an unexpired lease."},
            {"name": "renewed", "ok": True, "meaning": "Your lease now starts from this moment."},
            {"name": "released", "ok": True, "meaning": "You gave the item up."},
            {"name": "done", "ok": True, "meaning": "You marked the item finished."},
            {"name": "not-owner", "ok": False, "meaning": "Another owner holds it."},
            {"name": "absent", "ok": False, "meaning": "No claim for that item."},
            {
                "name": "closed",
                "ok": False,
                "meaning": "It is done. Take it again with reopen.",
            },
        ],
        "states": ["active", "expired", "done"],
        "stale": "An expired claim is stale. claim_take hands it to the new owner.",
        "default_ttl_seconds": DEFAULT_TTL,
        "max_ttl_seconds": MAX_TTL,
        "exit_codes": {"ok": 0, "refused": 3, "error": 1},
    }


class WorkLedger:
    """Claims stored in one JSON file.

    The file is replaced atomically. A sibling ``<ledger>.lock`` file
    serializes readers and writers across processes. Pass ``clock``
    (a callable returning a timezone-aware datetime) to test expiry.
    ``max_done`` is how many finished items to keep. ``max_claims`` is
    the cap on rows in the file, finished ones included.
    """

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        max_done: int = MAX_DONE,
        max_claims: int = MAX_CLAIMS,
    ) -> None:
        self.path = Path(ledger_path)
        self._clock = clock or _utcnow
        if type(max_done) is not int or max_done < 1:
            raise WorkClaimError("max_done must be an integer >= 1")
        if type(max_claims) is not int or max_claims < 1:
            raise WorkClaimError("max_claims must be an integer >= 1")
        self.max_done = max_done
        self.max_claims = max_claims

    def take(
        self,
        item: str,
        owner: str,
        *,
        ttl_seconds: int | None = None,
        note: str = "",
        reopen: bool = False,
    ) -> dict[str, Any]:
        """Claim ``item`` for ``owner`` until the lease expires.

        Same owner and a live lease returns ``held`` and does not extend it.
        A different owner with a live lease returns ``busy`` and writes
        nothing. An expired claim is taken over. A finished item returns
        ``closed`` unless ``reopen`` is true.
        """
        item = _item_id(item)
        owner = _owner_id(owner)
        ttl = DEFAULT_TTL if ttl_seconds is None else _ttl(ttl_seconds)
        note = _clean_note(note)
        if type(reopen) is not bool:
            raise WorkClaimError("reopen must be a boolean")
        now = self._now()

        def mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            claims = data["claims"]
            record = claims.get(item)
            previous = None
            if record is not None:
                view = self._view(record, now)
                state = view["state"]
                same = record["owner"] == owner
                if state == "active" and same:
                    return _result("held", True, view), False
                if state == "active":
                    return _result("busy", False, view), False
                if state == "done" and not reopen:
                    return _result("closed", False, view), False
                if not same:
                    previous = record["owner"]
            elif len(claims) >= self.max_claims:
                raise WorkClaimError(
                    f"ledger is full ({self.max_claims} claims); release or finish one first"
                )
            fresh = _fresh(item, owner, ttl, note, now)
            claims[item] = fresh
            name = "taken-over" if previous else "taken"
            return _result(name, True, self._view(fresh, now), previous_owner=previous), True

        return self._transaction(mutate, create=True)

    def renew(
        self,
        item: str,
        owner: str,
        *,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Extend the lease, starting from now. Only the current owner can.

        An expired claim can still be renewed until someone else takes it.
        ``ttl_seconds`` defaults to the lease already stored on the claim.
        """
        item = _item_id(item)
        owner = _owner_id(owner)
        ttl = None if ttl_seconds is None else _ttl(ttl_seconds)
        now = self._now()
        if not self.path.exists():
            return _absent(item)

        def mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            record = data["claims"].get(item)
            if record is None:
                return _absent(item), False
            view = self._view(record, now)
            if view["state"] == "done":
                return _result("closed", False, view), False
            if record["owner"] != owner:
                return _result("not-owner", False, view), False
            used = _stored_ttl(record) if ttl is None else ttl
            record["status"] = "claimed"
            record["ttl_seconds"] = used
            record["expires_at"] = _iso(now + timedelta(seconds=used))
            record["renewed_at"] = _iso(now)
            return _result("renewed", True, self._view(record, now)), True

        return self._transaction(mutate, create=False)

    def release(self, item: str, owner: str) -> dict[str, Any]:
        """Drop a claim you hold, live or expired, so someone else can take it."""
        item = _item_id(item)
        owner = _owner_id(owner)
        now = self._now()
        if not self.path.exists():
            return _absent(item)

        def mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            record = data["claims"].get(item)
            if record is None:
                return _absent(item), False
            view = self._view(record, now)
            if view["state"] == "done":
                return _result("closed", False, view), False
            if record["owner"] != owner:
                return _result("not-owner", False, view), False
            del data["claims"][item]
            return {"result": "released", "ok": True, "item": item}, True

        return self._transaction(mutate, create=False)

    def done(self, item: str, owner: str, *, note: str = "") -> dict[str, Any]:
        """Mark an item you hold as finished. A later take needs ``reopen``.

        Calling done again as the same owner is a no-op success. The ledger
        keeps the last ``max_done`` finished items and drops the older ones.
        """
        item = _item_id(item)
        owner = _owner_id(owner)
        note = _clean_note(note)
        now = self._now()
        if not self.path.exists():
            return _absent(item)

        def mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            record = data["claims"].get(item)
            if record is None:
                return _absent(item), False
            view = self._view(record, now)
            if record["owner"] != owner:
                if view["state"] == "done":
                    return _result("closed", False, view), False
                return _result("not-owner", False, view), False
            if view["state"] == "done":
                return _result("done", True, view), False
            record["status"] = "done"
            record["done_at"] = _iso(now)
            record.pop("expires_at", None)
            if note:
                record["note"] = note
            _trim_done(data["claims"], self.max_done)
            return _result("done", True, self._view(record, now)), True

        return self._transaction(mutate, create=False)

    def list_claims(
        self,
        *,
        item: str | None = None,
        state: str = "open",
        owner: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """List claims. The default ``state`` is ``open`` (active and expired).

        ``stale`` is an alias for ``expired``. A direct ``item`` lookup
        returns that row even when it is done. A missing ledger is an empty
        list and is not created.
        """
        state = _clean_state(state)
        if owner is not None:
            owner = _owner_id(owner)
        if item is not None:
            item = _item_id(item)
        limit = _limit(limit)
        if not self.path.exists():
            return {"count": 0, "claims": []}
        now = self._now()

        def read(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            claims = data["claims"]
            if item is not None:
                record = claims.get(item)
                if record is None:
                    return {"count": 0, "claims": []}, False
                view = self._view(record, now)
                if owner is not None and view["owner"] != owner:
                    return {"count": 0, "claims": []}, False
                return {"count": 1, "claims": [view]}, False
            rows = []
            for key in sorted(claims):
                view = self._view(claims[key], now)
                if _matches(view, state, owner):
                    rows.append(view)
            shown = rows[:limit]
            body: dict[str, Any] = {"count": len(shown), "claims": shown}
            if len(rows) > len(shown):
                body["total"] = len(rows)
                body["truncated"] = True
            return body, False

        return self._transaction(read, create=False)

    def _now(self) -> datetime:
        current = self._clock()
        if not isinstance(current, datetime):
            raise WorkClaimError("clock must return a datetime")
        if current.tzinfo is None:
            raise WorkClaimError("clock must return a timezone-aware datetime")
        # Match the milliseconds we store, so a reload sees the same instant.
        current = current.astimezone(timezone.utc)
        return current.replace(microsecond=(current.microsecond // 1000) * 1000)

    def _view(self, record: Any, now: datetime) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise WorkClaimError(f"a claim in {self.path} is corrupt")
        item = record.get("item")
        owner = record.get("owner")
        status = record.get("status")
        if (
            not isinstance(item, str)
            or not isinstance(owner, str)
            or status not in {"claimed", "done"}
        ):
            raise WorkClaimError(f"a claim in {self.path} is corrupt")
        view: dict[str, Any] = {"item": item, "owner": owner}
        if status == "done":
            view["state"] = "done"
        else:
            expires = _parse_time(record.get("expires_at"), item)
            remaining = (expires - now).total_seconds()
            if remaining <= 0:
                view["state"] = "expired"
                view["expires_in"] = 0
                view["takeable"] = True
            else:
                view["state"] = "active"
                view["expires_in"] = max(1, int(math.ceil(remaining - 1e-9)))
        note = record.get("note")
        if isinstance(note, str) and note:
            view["note"] = note
        return view

    def _transaction(
        self,
        fn: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]],
        *,
        create: bool,
    ) -> dict[str, Any]:
        # A read of a missing ledger must not create directories or files.
        # Callers that treat "missing" as empty or absent check exists() first.
        if not create and not self.path.exists():
            raise WorkClaimError(f"no claim ledger at {self.path}")
        lock_path = Path(str(self.path) + ".lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise WorkClaimError(f"cannot use ledger {self.path}: {exc.strerror}") from exc
        acquired = False
        try:
            _acquire_lock(lock_fd, lock_path)
            acquired = True
            data = self._load(create=create)
            result, dirty = fn(data)
            if dirty:
                ordered = {"version": 1, "claims": data["claims"]}
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
            raise WorkClaimError(f"ledger path is a directory: {self.path}")
        if not self.path.exists():
            if not create:
                raise WorkClaimError(f"no claim ledger at {self.path}")
            return {"version": 1, "claims": {}}
        size = self.path.stat().st_size
        if size > MAX_LEDGER_BYTES:
            raise WorkClaimError(
                f"ledger {self.path} is {size} bytes; refusing to load past {MAX_LEDGER_BYTES}"
            )
        try:
            raw = self.path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise WorkClaimError(f"ledger {self.path} is not UTF-8") from exc
        if not raw.strip():
            data: Any = {"version": 1, "claims": {}}
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise WorkClaimError(f"ledger {self.path} is not valid JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("claims"), dict):
            raise WorkClaimError(
                f"ledger {self.path} is not a work-claim file (expected a claims object)"
            )
        return data


def resolve_ledger(root: Path, explicit: str | None) -> Path:
    """Pick the ledger file. Relative paths resolve against ``root``, not a later cwd.

    An explicit path may use ``..``. ``$WORK_CLAIM_LEDGER`` may not leave ``root``
    unless it is absolute. The default is ``.agent-toolkit/work-claims.json``.
    """
    if explicit is not None and str(explicit).strip() != "":
        return _place(root, str(explicit).strip(), escape=True)
    env = os.environ.get("WORK_CLAIM_LEDGER")
    if env is not None and env.strip() != "":
        return _place(root, env.strip(), escape=False)
    return _place(root, DEFAULT_LEDGER_REL.as_posix(), escape=False)


def cli_root(value: str | None) -> Path:
    """The directory relative ledger paths resolve against. Captured absolutely."""
    if value is None or value.strip() == "":
        return Path.cwd()
    return Path(os.path.abspath(value.strip()))


def _place(root: Path, value: str, *, escape: bool) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if escape:
        # root is absolute, so abspath does not consult a later cwd.
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
                raise WorkClaimError("path escapes the root")
            stack.pop()
            continue
        stack.append(part)
    if len(stack) < len(base_parts):
        raise WorkClaimError("path escapes the root")
    return Path(*stack)


def _fresh(item: str, owner: str, ttl: int, note: str, now: datetime) -> dict[str, Any]:
    record: dict[str, Any] = {
        "item": item,
        "owner": owner,
        "status": "claimed",
        "ttl_seconds": ttl,
        "claimed_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=ttl)),
    }
    if note:
        record["note"] = note
    return record


def _result(
    name: str,
    ok: bool,
    view: dict[str, Any],
    previous_owner: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"result": name, "ok": ok}
    body.update(view)
    if previous_owner:
        body["previous_owner"] = previous_owner
    return body


def _absent(item: str) -> dict[str, Any]:
    return {"result": "absent", "ok": False, "item": item}


def _matches(view: dict[str, Any], state: str, owner: str | None) -> bool:
    if owner is not None and view["owner"] != owner:
        return False
    got = view["state"]
    if state == "all":
        return True
    if state == "open":
        return got != "done"
    return got == state


def _trim_done(claims: dict[str, Any], max_done: int) -> None:
    done_keys = [
        key
        for key, value in claims.items()
        if isinstance(value, dict) and value.get("status") == "done"
    ]
    if len(done_keys) <= max_done:
        return
    done_keys.sort(key=lambda key: str(claims[key].get("done_at") or ""))
    extra = len(done_keys) - max_done
    for key in done_keys[:extra]:
        del claims[key]


def _stored_ttl(record: dict[str, Any]) -> int:
    value = record.get("ttl_seconds", DEFAULT_TTL)
    if type(value) is not int or value < 1 or value > MAX_TTL:
        item = record.get("item")
        raise WorkClaimError(f"claim {item!r} in the ledger has a bad ttl")
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(value: Any, item: str) -> datetime:
    if not isinstance(value, str):
        raise WorkClaimError(f"claim {item!r} in the ledger has a bad expires_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorkClaimError(f"claim {item!r} in the ledger has a bad expires_at") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _item_id(value: Any) -> str:
    if not isinstance(value, str) or _ITEM.fullmatch(value) is None:
        raise WorkClaimError(
            "item must be 1–160 characters: letters, digits, and . _ : / # - "
            "(not starting with a symbol)"
        )
    return value


def _owner_id(value: Any) -> str:
    if not isinstance(value, str) or _OWNER.fullmatch(value) is None:
        raise WorkClaimError(
            "owner must be 1–128 characters: letters, digits, and . _ : - "
            "(not starting with a symbol)"
        )
    return value


def _as_int(name: str, value: Any) -> int:
    if type(value) is bool:
        raise WorkClaimError(f"{name} must be an integer")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise WorkClaimError(f"{name} must be an integer")
        value = int(value)
    if type(value) is not int:
        raise WorkClaimError(f"{name} must be an integer")
    return value


def _ttl(value: Any) -> int:
    number = _as_int("ttl_seconds", value)
    if number < 1 or number > MAX_TTL:
        raise WorkClaimError(f"ttl_seconds must be from 1 to {MAX_TTL}")
    return number


def _limit(value: Any) -> int:
    number = _as_int("limit", value)
    if number < 1 or number > MAX_LIMIT:
        raise WorkClaimError(f"limit must be from 1 to {MAX_LIMIT}")
    return number


def _clean_state(value: Any) -> str:
    if value is None or value == "":
        return "open"
    if not isinstance(value, str):
        raise WorkClaimError("state must be a string")
    name = value.strip().lower()
    if name not in _STATES:
        raise WorkClaimError("state must be one of: open, active, expired, stale, done, all")
    if name == "stale":
        return "expired"
    return name


def _clean_note(note: Any) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise WorkClaimError("note must be a string")
    if any(ord(ch) < 32 for ch in note):
        raise WorkClaimError("note must not contain control characters")
    note = note.strip()
    if len(note) > MAX_NOTE_LEN:
        raise WorkClaimError(f"note must be at most {MAX_NOTE_LEN} characters")
    return note


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
                raise WorkClaimError(f"timed out waiting for ledger lock {lock_path}")
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
    if "claims" in payload and "result" not in payload:
        return _format_list(payload)
    lines = [f"{'result':<8}  {payload['result']}"]
    for key, label in (
        ("item", "item"),
        ("owner", "owner"),
        ("previous_owner", "previous"),
        ("state", "state"),
    ):
        if key in payload:
            lines.append(f"{label:<8}  {payload[key]}")
    if payload.get("takeable"):
        lines.append(f"{'expires':<8}  expired, takeable")
    elif "expires_in" in payload:
        lines.append(f"{'expires':<8}  {payload['expires_in']}s")
    if payload.get("note"):
        lines.append(f"{'note':<8}  {payload['note']}")
    return "\n".join(lines) + "\n"


def _format_list(payload: dict[str, Any]) -> str:
    lines = [f"{'count':<8}  {payload['count']}"]
    claims = payload["claims"]
    if not claims:
        lines.append(f"{'claims':<8}  none")
    for row in claims:
        if row["state"] == "active":
            extra = f"  {row['expires_in']}s"
        elif row.get("takeable"):
            extra = "  takeable"
        else:
            extra = ""
        lines.append(f"{'':<8}  {row['item']}  {row['owner']}  {row['state']}{extra}")
    if payload.get("truncated"):
        lines.append(f"{'total':<8}  {payload['total']}")
    return "\n".join(lines) + "\n"


def format_explain(rules: dict[str, Any]) -> str:
    lines = [f"work-claim {rules['version']}", ""]
    lines.append(f"{'result':<12}what take, renew, release, and done return")
    for item in rules["results"]:
        lines.append(f"{'':<12}{item['name']:<12}{item['meaning']}")
    lines.append("")
    lines.append(f"{'states':<12}{', '.join(rules['states'])}")
    lines.append(f"{'stale':<12}{rules['stale']}")
    lines.append(
        f"{'lease':<12}{rules['default_ttl_seconds']}s default, {rules['max_ttl_seconds']}s max"
    )
    lines.append(f"{'exit':<12}0 ok, 3 refused, 1 error")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="work-claim",
        description=(
            "Claim a piece of work so two agents don't do it twice. "
            "A claim is a lease with an owner and a TTL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes for take, renew, release, and done:\n"
            "  0  ok (taken, taken-over, held, renewed, released, done)\n"
            "  3  refused (busy, not-owner, absent, closed)\n"
            "  1  error\n"
            "\n"
            "list and explain exit 0. busy means do not start that item.\n"
            "An expired claim is stale and can be taken over.\n"
            "\n"
            f"Lease default: {DEFAULT_TTL} seconds. Maximum: {MAX_TTL} (24 hours).\n"
            "Ledger default: .agent-toolkit/work-claims.json under --root,\n"
            "or $WORK_CLAIM_LEDGER. Relative paths resolve against --root."
        ),
    )
    parser.add_argument("--version", action="version", version=f"work-claim {VERSION}")
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

    take_cmd = sub.add_parser("take", help="claim an item until the lease expires")
    add_place(take_cmd, json_flag=True)
    take_cmd.add_argument("item")
    take_cmd.add_argument("--owner", required=True, help="owner id (a label, not a secret)")
    take_cmd.add_argument(
        "--ttl",
        type=int,
        default=None,
        help=f"lease seconds (default {DEFAULT_TTL}, max {MAX_TTL})",
    )
    take_cmd.add_argument("--note", default="", help="short note stored on the claim")
    take_cmd.add_argument(
        "--reopen",
        action="store_true",
        help="claim an item that was already marked done",
    )

    renew_cmd = sub.add_parser("renew", help="extend a lease you hold")
    add_place(renew_cmd, json_flag=True)
    renew_cmd.add_argument("item")
    renew_cmd.add_argument("--owner", required=True)
    renew_cmd.add_argument("--ttl", type=int, default=None, help="new lease seconds")

    release_cmd = sub.add_parser("release", help="give an item back")
    add_place(release_cmd, json_flag=True)
    release_cmd.add_argument("item")
    release_cmd.add_argument("--owner", required=True)

    done_cmd = sub.add_parser("done", help="mark an item you hold as finished")
    add_place(done_cmd, json_flag=True)
    done_cmd.add_argument("item")
    done_cmd.add_argument("--owner", required=True)
    done_cmd.add_argument("--note", default="", help="short note stored on the claim")

    list_cmd = sub.add_parser("list", help="list claims (expired ones are takeable)")
    add_place(list_cmd, json_flag=True)
    list_cmd.add_argument("item", nargs="?", default=None, help="one item, if you name it")
    list_cmd.add_argument(
        "--state",
        default="open",
        choices=sorted(_STATES),
        help="open (default), active, expired, stale, done, or all",
    )
    list_cmd.add_argument("--owner", default=None, help="only this owner")
    list_cmd.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    explain_cmd = sub.add_parser("explain", help="print the results and the lease rules")
    explain_cmd.add_argument("--json", action="store_true", help="print the rules as JSON")

    serve_cmd = sub.add_parser(
        "serve",
        help="MCP server on stdio (one JSON-RPC message per line)",
    )
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
                sys.stdout.write(json.dumps(rules, indent=2) + "\n")
            else:
                sys.stdout.write(format_explain(rules))
            return 0
        root = cli_root(getattr(args, "root", None))
        ledger = resolve_ledger(root, getattr(args, "ledger", None))
        if args.command == "serve":
            return serve(_Defaults(root, ledger))
        book = WorkLedger(ledger)
        if args.command == "take":
            payload = book.take(
                args.item,
                args.owner,
                ttl_seconds=args.ttl,
                note=args.note,
                reopen=args.reopen,
            )
        elif args.command == "renew":
            payload = book.renew(args.item, args.owner, ttl_seconds=args.ttl)
        elif args.command == "release":
            payload = book.release(args.item, args.owner)
        elif args.command == "done":
            payload = book.done(args.item, args.owner, note=args.note)
        elif args.command == "list":
            payload = book.list_claims(
                item=args.item,
                state=args.state,
                owner=args.owner,
                limit=args.limit,
            )
        else:  # pragma: no cover - argparse requires a command
            parser.error(f"unknown command {args.command}")
            return 2
    except WorkClaimError as exc:
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
    "Work-claim ledger. claim_take an item with your owner id and a ttl_seconds "
    "lease before you work on it. busy means someone else holds it: do that item "
    "later, or pick another. When the lease expires the claim is stale and "
    "claim_take hands it to you. claim_renew extends your lease. claim_release "
    "gives it back. claim_done marks it finished. claim_list shows open claims. "
    "claim_explain lists the results. Relative ledger and root paths resolve "
    "against the server --root, not the process cwd."
)

_TOOL_NAMES = {
    "claim_take",
    "claim_renew",
    "claim_release",
    "claim_done",
    "claim_list",
    "claim_explain",
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
    item = {"type": "string", "description": "Work item id, such as fix-flaky-test or src/app.py."}
    owner = {"type": "string", "description": "Owner id. A label, not a secret."}
    ttl = {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_TTL,
        "description": f"Lease seconds. Default {DEFAULT_TTL}. Max {MAX_TTL}.",
    }
    note = {"type": "string", "description": f"Short note, at most {MAX_NOTE_LEN} characters."}
    return [
        {
            "name": "claim_take",
            "description": (
                "Claim an item until the lease expires. busy means someone else holds "
                "a live lease. An expired claim is taken over."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item": item,
                    "owner": owner,
                    "ttl_seconds": ttl,
                    "note": note,
                    "reopen": {
                        "type": "boolean",
                        "description": "Claim an item that is already done.",
                    },
                    **_place_props(),
                },
                "required": ["item", "owner"],
                "additionalProperties": False,
            },
        },
        {
            "name": "claim_renew",
            "description": "Extend a lease you hold, starting from now. Only the current owner can.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item": item,
                    "owner": owner,
                    "ttl_seconds": ttl,
                    **_place_props(),
                },
                "required": ["item", "owner"],
                "additionalProperties": False,
            },
        },
        {
            "name": "claim_release",
            "description": "Give up an item you hold so someone else can take it.",
            "inputSchema": {
                "type": "object",
                "properties": {"item": item, "owner": owner, **_place_props()},
                "required": ["item", "owner"],
                "additionalProperties": False,
            },
        },
        {
            "name": "claim_done",
            "description": "Mark an item you hold as finished. A later take needs reopen.",
            "inputSchema": {
                "type": "object",
                "properties": {"item": item, "owner": owner, "note": note, **_place_props()},
                "required": ["item", "owner"],
                "additionalProperties": False,
            },
        },
        {
            "name": "claim_list",
            "description": "List claims. Expired rows are stale and takeable. Done rows are omitted unless asked for.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item": item,
                    "state": {
                        "type": "string",
                        "enum": ["open", "active", "expired", "stale", "done", "all"],
                        "description": "Default open. stale is an alias for expired.",
                    },
                    "owner": owner,
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
            "name": "claim_explain",
            "description": "List results, states, and the default lease.",
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
    # A version we speak is echoed. Anything else, including a newer one,
    # gets the newest version this server speaks.
    if isinstance(requested, str) and requested in _MCP_PROTOCOLS:
        return requested
    return _MCP_NEWEST


def _reject_unknown(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise WorkClaimError(f"unknown field {unknown[0]!r}")


def _ledger_from_args(args: dict[str, Any], defaults: _Defaults) -> WorkLedger:
    root = defaults.root
    raw_root = args.get("root")
    if raw_root not in (None, ""):
        if not isinstance(raw_root, str):
            raise WorkClaimError("root must be a string")
        root = _place(defaults.root, raw_root.strip(), escape=False)
    raw_ledger = args.get("ledger")
    if raw_ledger not in (None, ""):
        if not isinstance(raw_ledger, str):
            raise WorkClaimError("ledger must be a string")
        path = _place(root, raw_ledger.strip(), escape=False)
    elif raw_root not in (None, ""):
        path = _place(root, DEFAULT_LEDGER_REL.as_posix(), escape=False)
    else:
        path = defaults.ledger
    return WorkLedger(path)


def _optional_ttl(args: dict[str, Any]) -> int | None:
    if "ttl_seconds" not in args or args["ttl_seconds"] is None:
        return None
    return _ttl(args["ttl_seconds"])


def _dispatch_tool(name: str, args: dict[str, Any], defaults: _Defaults) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise WorkClaimError("arguments must be an object")
    if name == "claim_explain":
        _reject_unknown(args, set())
        return _tool_result(explain_rules())
    if name == "claim_list":
        _reject_unknown(args, {"item", "state", "owner", "limit", "root", "ledger"})
        book = _ledger_from_args(args, defaults)
        item = args.get("item")
        if item is not None:
            item = _item_id(item)
        owner = args.get("owner")
        if owner is not None:
            owner = _owner_id(owner)
        limit = DEFAULT_LIMIT if "limit" not in args or args["limit"] is None else args["limit"]
        return _tool_result(
            book.list_claims(item=item, state=args.get("state", "open"), owner=owner, limit=limit)
        )
    if name == "claim_take":
        _reject_unknown(args, {"item", "owner", "ttl_seconds", "note", "reopen", "root", "ledger"})
        reopen = args.get("reopen", False)
        if type(reopen) is not bool:
            raise WorkClaimError("reopen must be a boolean")
        book = _ledger_from_args(args, defaults)
        return _tool_result(
            book.take(
                args.get("item"),  # type: ignore[arg-type]
                args.get("owner"),  # type: ignore[arg-type]
                ttl_seconds=_optional_ttl(args),
                note="" if args.get("note") is None else args.get("note"),
                reopen=reopen,
            )
        )
    if name == "claim_renew":
        _reject_unknown(args, {"item", "owner", "ttl_seconds", "root", "ledger"})
        book = _ledger_from_args(args, defaults)
        return _tool_result(
            book.renew(
                args.get("item"),  # type: ignore[arg-type]
                args.get("owner"),  # type: ignore[arg-type]
                ttl_seconds=_optional_ttl(args),
            )
        )
    if name == "claim_release":
        _reject_unknown(args, {"item", "owner", "root", "ledger"})
        book = _ledger_from_args(args, defaults)
        return _tool_result(book.release(args.get("item"), args.get("owner")))  # type: ignore[arg-type]
    if name == "claim_done":
        _reject_unknown(args, {"item", "owner", "note", "root", "ledger"})
        book = _ledger_from_args(args, defaults)
        return _tool_result(
            book.done(
                args.get("item"),  # type: ignore[arg-type]
                args.get("owner"),  # type: ignore[arg-type]
                note="" if args.get("note") is None else args.get("note"),
            )
        )
    raise WorkClaimError(f"unknown tool {name!r}")


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
                "serverInfo": {"name": "work-claim", "version": VERSION},
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
        except WorkClaimError as exc:
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
                sys.stderr.write(f"work-claim: {exc}\n")
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
