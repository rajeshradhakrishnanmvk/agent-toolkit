#!/usr/bin/env python3
"""Week 2 screen-recording demo.

A sloppy handoff fails with a punch list. The repaired one passes.
Run from anywhere:

    python3 demo_handoff.py

Cited paths resolve next to this file, so the repo layout is the root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from handoff_checker import NEEDS_FIXES, READY, HandoffChecker

ROOT = Path(__file__).resolve().parent

SLOPPY = """\
---
goal: Update the project page for week 2
owner: ""
budget_left: ""
constraints:
  - Do not rewrite the week 1 section.
acceptance_criteria:
  - update the page properly
  - add week 2, etc
questions:
  - Which tone should the page use?
artifacts:
  - docs/no-such-page.md
---

TODO: check the visitor counter before publishing.
"""

FIXED = """\
---
goal: List week 2 on the project page as a live tool.
owner: writer-agent
budget_left: 4000 tokens, 0.05 USD, 8 tool calls
constraints:
  - Do not rewrite the week 1 section.
acceptance_criteria:
  - The week 2 row on docs/index.html shows a Live badge.
  - The visit counter script in docs/index.html is byte-for-byte the same.
questions: []
artifacts:
  - docs/index.html
  - README.md
---

Match the tone of the week 1 section in README.md.
"""

# The recording is built around this order.
SLOPPY_CODES = (
    "missing-field",
    "missing-field",
    "vague-criterion",
    "vague-criterion",
    "missing-path",
    "unresolved-question",
    "todo-left",
)

_COLORS = {READY: "32", NEEDS_FIXES: "33"}


def _paint(text: str, status: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{_COLORS[status]}m{text}\033[0m"


def main() -> int:
    checker = HandoffChecker(root=ROOT)
    print("agent-toolkit · week 2 · handoff checker")
    print()
    print("A researcher is handing a docs change to a writer.")
    print("The first payload is sloppy. The checker names what to fix.")
    print("The second is the same handoff, repaired.")
    print()

    steps = (
        ("sloppy", SLOPPY, NEEDS_FIXES, SLOPPY_CODES),
        ("repaired", FIXED, READY, ()),
    )
    for index, (label, text, expected, codes) in enumerate(steps, start=1):
        result = checker.check(text)
        got = result["status"]
        got_codes = tuple(problem["code"] for problem in result["problems"])
        if got != expected or got_codes != tuple(codes):
            detail = "\n".join(problem["message"] for problem in result["problems"])
            raise SystemExit(
                f"step {index} returned {got} {list(got_codes)}, "
                f"expected {expected} {list(codes)}\n{detail}"
            )
        print(f"  {index}  {label:<10}  {_paint(got, got)}")
        if got == READY:
            found = result["paths"]["found"]
            if len(found) != 2:
                raise SystemExit(f"expected 2 cited paths, got {found}")
            print(f"     {len(found)} cited paths exist")
            continue
        for problem in result["problems"]:
            print(f"     {problem['message']}")

    print()
    print("result  the sloppy handoff was not sent. The repaired one is ready.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
