#!/usr/bin/env python3
"""Week 1 screen-recording demo.

An agent loop that is allowed, warned, then stopped. Run from the repo root:

    python3 demo.py

Wall-clock is left unset on purpose so the recording has one plot: tokens.
The dollars and tool-call caps are real; this run just doesn't trip them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from budget_guard import CONTINUE, STOP_AND_ASK, WARN, BudgetGuard

TASK = "fix-flaky-test"
LEDGER = Path(".agent-toolkit") / "demo-ledger.json"

# (step name, tokens, tool calls, decision the recording is built around)
STEPS = (
    ("read_failures", 600, 1, CONTINUE),
    ("edit_test", 700, 1, CONTINUE),
    ("run_tests", 800, 1, WARN),
    ("rerun_tests", 500, 1, STOP_AND_ASK),
)

_WORDS = {CONTINUE: "continue", WARN: "warn", STOP_AND_ASK: "stop"}
_COLORS = {CONTINUE: "32", WARN: "33", STOP_AND_ASK: "31"}


def _paint(text: str, decision: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{_COLORS[decision]}m{text}\033[0m"


def main() -> int:
    if LEDGER.exists():
        LEDGER.unlink()
    guard = BudgetGuard(LEDGER)
    opened = guard.open_task(
        TASK,
        max_tokens=2500,
        max_cost_usd=0.02,
        max_tool_calls=4,
        warn_ratio=0.8,
    )
    limits = opened["limits"]
    warn_at = round(limits["warn_ratio"] * 100)

    print("agent-toolkit · week 1 · budget guard")
    print()
    print("An agent is fixing a flaky test. It checks the budget before each step.")
    print()
    print(f"task    {TASK}")
    print(
        f"limits  {limits['max_tokens']:,} tokens"
        f" · ${limits['max_cost_usd']:.2f}"
        f" · {limits['max_tool_calls']} tool calls"
    )
    print(f"warn    {warn_at:.0f}% of any limit")
    print()

    for index, (name, tokens, calls, expected) in enumerate(STEPS, start=1):
        decision = guard.check(TASK, tokens=tokens, tool_calls=calls)
        got = decision["decision"]
        if got != expected:
            raise SystemExit(f"step {index} returned {got}, expected {expected}")
        print(
            f"  {index}  {name:<16}+{tokens} tok  +{calls} call   "
            f"{_paint(_WORDS[got], got)}"
        )
        if got == STOP_AND_ASK:
            print(f"     {decision['reason']}")
            print(f"     step {index} was not recorded")
            break
        projected = decision["projected"]["tokens"]
        limit = decision["limits"]["max_tokens"]
        near = " - near the limit" if got == WARN else ""
        print(f"     {projected:,}/{limit:,} tokens ({projected / limit:.0%}){near}")
        recorded = guard.record(TASK, tokens=tokens, tool_calls=calls, note=name)
        if recorded["decision"] != got:
            raise SystemExit(
                f"step {index} record returned {recorded['decision']}, check said {got}"
            )

    final = guard.status(TASK)
    spent = final["spent"]
    caps = final["limits"]
    print()
    print(
        f"spent   {spent['tokens']:,}/{caps['max_tokens']:,} tokens"
        f" · ${spent['cost_usd']:.2f}/${caps['max_cost_usd']:.2f}"
        f" · {spent['tool_calls']}/{caps['max_tool_calls']} tool calls"
    )
    print(f"ledger  {LEDGER.as_posix()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
