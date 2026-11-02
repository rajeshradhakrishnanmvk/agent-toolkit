#!/usr/bin/env python3
"""Week 3 screen-recording demo.

Two agents, one ledger. Agent B is blocked, then takes over the expired claim.

Run from anywhere:

    python3 demo_work_claim.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from work_claim import WorkLedger

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / ".agent-toolkit" / "demo-claims.json"
ITEM = "fix-flaky-test"
TTL = 60

_COLORS = {
    "taken": "32",
    "taken-over": "32",
    "busy": "33",
    "expired": "33",
    "not-owner": "31",
    "done": "32",
}


def _paint(text: str, kind: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{_COLORS[kind]}m{text}\033[0m"


def _show(index: int, actor: str, action: str, result: str, detail: str = "") -> None:
    shown = _paint(result, result)
    pad = " " * (12 - len(result))
    line = f"  {index}  {actor:<8}{action:<6}{shown}{pad}"
    if detail:
        line += f"  {detail}"
    print(line.rstrip())


def main() -> int:
    if LEDGER.exists():
        LEDGER.unlink()
    start = datetime(2026, 11, 2, tzinfo=timezone.utc)
    now = [start]
    agent_a = WorkLedger(LEDGER, clock=lambda: now[0])
    agent_b = WorkLedger(LEDGER, clock=lambda: now[0])

    print("agent-toolkit · week 3 · work-claim ledger")
    print()
    print("Two agents share one task list. A claim is a lease.")
    print("Agent B is blocked while agent A holds it, then takes over the expired claim.")
    print()
    print(f"item    {ITEM}")
    print(f"lease   {TTL}s")
    print()

    taken = agent_a.take(ITEM, "agent-a", ttl_seconds=TTL)
    if taken["result"] != "taken" or taken["expires_in"] != TTL:
        raise SystemExit(f"step 1 returned {taken}")
    _show(1, "agent-a", "take", taken["result"])

    busy = agent_b.take(ITEM, "agent-b", ttl_seconds=TTL)
    if busy["result"] != "busy" or busy["owner"] != "agent-a" or busy["expires_in"] != TTL:
        raise SystemExit(f"step 2 returned {busy}")
    _show(2, "agent-b", "take", busy["result"], f"held by {busy['owner']}, {busy['expires_in']}s left")

    now[0] = start + timedelta(seconds=TTL)
    listed = agent_b.list_claims(state="stale")
    rows = listed["claims"]
    if len(rows) != 1 or rows[0]["state"] != "expired" or rows[0].get("takeable") is not True:
        raise SystemExit(f"step 3 returned {listed}")
    _show(3, "list", "stale", "expired", "takeable")

    over = agent_b.take(ITEM, "agent-b", ttl_seconds=TTL)
    if over["result"] != "taken-over" or over["previous_owner"] != "agent-a" or not over["ok"]:
        raise SystemExit(f"step 4 returned {over}")
    _show(4, "agent-b", "take", over["result"], f"was {over['previous_owner']}")

    denied = agent_a.renew(ITEM, "agent-a")
    if denied["result"] != "not-owner" or denied["owner"] != "agent-b":
        raise SystemExit(f"step 5 returned {denied}")
    _show(5, "agent-a", "renew", denied["result"], f"held by {denied['owner']}")

    finished = agent_b.done(ITEM, "agent-b")
    if finished["result"] != "done" or finished["state"] != "done":
        raise SystemExit(f"step 6 returned {finished}")
    _show(6, "agent-b", "done", finished["result"])

    if agent_a.list_claims()["count"] != 0:
        raise SystemExit("done item was still listed as open")

    print()
    print("result  agent-b was blocked, then took over the expired claim.")
    print(f"ledger  {LEDGER.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
