#!/usr/bin/env python3
"""Week 4 example. No network.

A tiny budget stops the next step. The question is stored, read back as
pending, then a human answer is recorded for that request id only. The
budget total stays at zero: an approval does not spend or resume the task.

The process that writes the answer is this example, not an identity check.
Run from the repo root:

    python3 demo_ask_human.py
"""

from __future__ import annotations

from pathlib import Path

from ask_human import ApprovalLedger
from budget_guard import STOP_AND_ASK, BudgetGuard

TASK = "fix-flaky-test"
ACTION = "spend the next step on fix-flaky-test"
ROOT = Path(".agent-toolkit")
BUDGET = ROOT / "demo-ask-budget.json"
LEDGER = ROOT / "demo-ask-approvals.json"


def _reset(path: Path) -> None:
    for candidate in (path, Path(str(path) + ".lock")):
        if candidate.exists():
            candidate.unlink()


def main() -> int:
    ROOT.mkdir(exist_ok=True)
    _reset(BUDGET)
    _reset(LEDGER)
    guard = BudgetGuard(BUDGET)
    guard.open_task(TASK, max_tokens=100)
    stopped = guard.check(TASK, tokens=1000)
    if stopped["decision"] != STOP_AND_ASK or not stopped.get("ask_human"):
        raise SystemExit("budget check did not stop")
    question = stopped["ask_human"]
    book = ApprovalLedger(LEDGER)
    filed = book.ask(TASK, question, action=ACTION)
    pending = book.status(TASK)
    answered = book.answer(
        TASK,
        filed["request_id"],
        "approved",
        question=question,
        action=ACTION,
        by="example",
    )
    inspected = book.status(TASK, request_id=filed["request_id"])
    spent = guard.status(TASK)["spent"]["tokens"]
    print(f"decision  {stopped['decision']}")
    print(f"request   {filed['request_id']}")
    print(f"status    {pending['status']}")
    print(f"answered  {answered['result']}")
    print(f"inspected {inspected['status']} {inspected['request_id']}")
    print(f"budget    {spent} tokens spent")
    print("approval stored the answer and did not raise the budget or run the next step")
    print("this file wrote the answer itself; the ledger does not check who answered")
    if pending["status"] != "pending" or answered["result"] != "approved":
        return 1
    if inspected["request_id"] != filed["request_id"] or inspected["status"] != "approved":
        return 1
    if inspected["question"] != question or inspected["action"] != ACTION:
        return 1
    if spent != 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
