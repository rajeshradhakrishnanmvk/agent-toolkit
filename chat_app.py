#!/usr/bin/env python3
"""Financial consultant multi-bot chat client.

This script keeps the OpenAI-compatible endpoint behavior from README Codespaces
and orchestrates three local bots around each user request:

- Intake bot: captures user intent and constraints.
- Analysis bot: builds a practical financial action brief.
- Advisor bot: asks the LLM for the final response.

The workflow uses:
- budget_guard.BudgetGuard for per-chat cost controls.
- handoff_checker.HandoffChecker for handoff quality checks.
- work_claim.WorkLedger for stage ownership/lease tracking.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from budget_guard import BudgetError, BudgetGuard
from handoff_checker import HandoffChecker
from work_claim import WorkClaimError, WorkLedger

DEFAULT_BASE_URL = "https://chat.srv1536099.hstgr.cloud/v1"
DEFAULT_MODEL = "Llama-3.2-3B-Instruct"
DEFAULT_TASK_ID = "financial-consultant-chat"


def estimate_tokens(text: str) -> int:
    """Fast token estimate for budget checks, without external dependencies."""
    if not text:
        return 1
    return max(1, len(text) // 4)


def _safe_slug(text: str) -> str:
    keep = []
    for ch in text.lower():
        if ch.isalnum() or ch in {"-", "_", ":", "."}:
            keep.append(ch)
        elif ch.isspace():
            keep.append("-")
    slug = "".join(keep).strip("-")
    return slug[:80] or "query"


def _question_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and "?" in line:
            lines.append(line)
    return lines


def _resolved_questions_from_prose(text: str) -> list[dict[str, str | bool]]:
    resolved: list[dict[str, str | bool]] = []
    for question in _question_lines(text):
        resolved.append(
            {
                "question": question,
                "answer": "Captured from user prompt and handled by the advisor bot.",
                "resolved": True,
            }
        )
    return resolved


def _resolve_settings() -> tuple[str, str, str]:
    base_url = os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLAMA_API_KEY") or ""
    return base_url.rstrip("/"), model, api_key


def _http_json(url: str, payload: dict[str, Any], api_key: str, timeout: float = 60.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = raw
        try:
            parsed = json.loads(raw)
            detail = json.dumps(parsed, ensure_ascii=True)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error calling {url}: {exc.reason}") from exc


def create_chat_completion(
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> str:
    response = _http_json(
        f"{base_url}/chat/completions",
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        api_key,
    )
    try:
        return response["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError, TypeError) as exc:
        raise RuntimeError(f"Unexpected response shape: {json.dumps(response, ensure_ascii=True)}") from exc


class FinancialConsultantBots:
    """Coordinates intake, analysis, and advisor stages per user turn."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        root: Path,
        task_id: str,
        max_tokens_budget: int,
        max_cost_budget: float,
        max_tool_calls_budget: int,
        max_tokens_reply: int,
        temperature: float,
        system_prompt: str,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.root = root
        self.task_id = task_id
        self.max_tokens_reply = max_tokens_reply
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.turn_number = 0

        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = BudgetGuard(self.root / "budget-ledger.json")
        self.claims = WorkLedger(self.root / "work-claims.json")
        self.handoff = HandoffChecker(root=self.root)

        self._ensure_budget(max_tokens_budget, max_cost_budget, max_tool_calls_budget)

    def _ensure_budget(self, max_tokens: int, max_cost: float, max_tools: int) -> None:
        try:
            self.budget.open_task(
                self.task_id,
                max_tokens=max_tokens,
                max_cost_usd=max_cost,
                max_tool_calls=max_tools,
            )
        except BudgetError:
            # Existing task is acceptable for iterative chat sessions.
            self.budget.status(self.task_id)

    def _claim_stage(self, stage_item: str, owner: str, note: str) -> None:
        result = self.claims.take(stage_item, owner, ttl_seconds=900, note=note, reopen=True)
        if not result.get("ok"):
            raise RuntimeError(f"Cannot claim stage {stage_item}: {result.get('result')}")

    def _done_stage(self, stage_item: str, owner: str, note: str) -> None:
        result = self.claims.done(stage_item, owner, note=note)
        if not result.get("ok"):
            raise RuntimeError(f"Cannot close stage {stage_item}: {result.get('result')}")

    def _validate_handoff(self, payload: dict[str, Any]) -> None:
        checked = self.handoff.check(payload)
        if checked["status"] != "ready":
            raise RuntimeError(f"Handoff invalid: {checked['summary']}")

    def _make_analysis(self, intake_summary: str, user_text: str) -> str:
        tips = [
            "Protect emergency fund first (3-6 months expenses).",
            "Avoid concentration risk and keep diversification broad.",
            "Prefer low-cost, transparent products when options are similar.",
            "Match debt prepayment vs investing with your risk and horizon.",
        ]
        selected = tips[self.turn_number % len(tips)]
        return (
            f"User request: {user_text}\n"
            f"Intake summary: {intake_summary}\n"
            f"Planning note: {selected}"
        )

    def reply(self, user_text: str) -> tuple[str, dict[str, Any]]:
        self.turn_number += 1
        turn_id = f"turn-{self.turn_number:04d}-{_safe_slug(user_text)[:24]}"

        projected_tokens = estimate_tokens(user_text) + 220
        gate = self.budget.check(self.task_id, tokens=projected_tokens, tool_calls=1)
        if gate["decision"] == "stop-and-ask-human":
            reason = gate.get("reason", "budget exceeded")
            return (
                f"I need human approval before continuing: {reason}",
                {
                    "decision": gate["decision"],
                    "reason": reason,
                    "spent": gate["spent"],
                    "remaining": gate.get("remaining", {}),
                },
            )

        intake_item = f"{turn_id}:intake"
        analysis_item = f"{turn_id}:analysis"
        advisor_item = f"{turn_id}:advisor"

        try:
            self._claim_stage(intake_item, "intake-bot", "Gather financial context")
            intake_summary = (
                "Clarify goals, timeline, cash flow, debt obligations, and risk tolerance. "
                f"Current prompt: {user_text}"
            )
            self._validate_handoff(
                {
                    "goal": "Pass user financial context to analysis bot.",
                    "acceptance_criteria": [
                        "Includes user objective and constraints.",
                        "Mentions risk and timeline assumptions.",
                    ],
                    "owner": "analysis-bot",
                    "constraints": [
                        "Do not promise returns.",
                        "Keep recommendations educational, not legal/tax advice.",
                    ],
                    "budget_left": "positive",
                    "artifacts": [],
                    "questions": _resolved_questions_from_prose(intake_summary),
                    "notes": intake_summary,
                }
            )
            self._done_stage(intake_item, "intake-bot", "intake complete")
            self.budget.record(self.task_id, tokens=estimate_tokens(intake_summary), tool_calls=1)

            self._claim_stage(analysis_item, "analysis-bot", "Build practical action brief")
            analysis_summary = self._make_analysis(intake_summary, user_text)
            self._validate_handoff(
                {
                    "goal": "Pass analysis brief to advisor bot for final response.",
                    "acceptance_criteria": [
                        "Includes assumptions and risk framing.",
                        "Includes practical next steps.",
                    ],
                    "owner": "advisor-bot",
                    "constraints": [
                        "Keep language clear and concise.",
                        "Avoid unsafe guarantees.",
                    ],
                    "budget_left": "positive",
                    "artifacts": [],
                    "questions": _resolved_questions_from_prose(analysis_summary),
                    "notes": analysis_summary,
                }
            )
            self._done_stage(analysis_item, "analysis-bot", "analysis complete")
            self.budget.record(self.task_id, tokens=estimate_tokens(analysis_summary), tool_calls=1)

            self._claim_stage(advisor_item, "advisor-bot", "Generate final user answer")
            advisor_messages = [
                {
                    "role": "system",
                    "content": (
                        f"{self.system_prompt}\n"
                        "You are an educational financial consultant bot. "
                        "Provide practical steps, assumptions, and risks. "
                        "Do not claim guaranteed returns."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Use the internal brief to answer the user.\n\n"
                        f"INTAKE:\n{intake_summary}\n\n"
                        f"ANALYSIS:\n{analysis_summary}\n\n"
                        f"USER:\n{user_text}"
                    ),
                },
            ]
            final = create_chat_completion(
                base_url=self.base_url,
                model=self.model,
                api_key=self.api_key,
                messages=advisor_messages,
                max_tokens=self.max_tokens_reply,
                temperature=self.temperature,
            )
            self._done_stage(advisor_item, "advisor-bot", "advisor complete")
            self.budget.record(self.task_id, tokens=estimate_tokens(final), tool_calls=1)
            status = self.budget.status(self.task_id)
            return final, status
        except (BudgetError, WorkClaimError, RuntimeError) as exc:
            return f"workflow-error: {exc}", self.budget.status(self.task_id)


def _print_startup(base_url: str, model: str) -> None:
    print("Financial Consultant Bots")
    print(f"endpoint: {base_url}")
    print(f"model:    {model}")
    print("commands: /help, /model, /status, /quit")
    print()


def _handle_command(command: str, model: str) -> tuple[str, bool]:
    cmd = command.strip().lower()
    if cmd in {"/quit", "/exit"}:
        return model, True
    if cmd == "/help":
        print("/help   show commands")
        print("/model  show active model")
        print("/status show budget status")
        print("/quit   exit")
        return model, False
    if cmd == "/model":
        print(f"active model: {model}")
        return model, False
    return model, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive chat client for Codespaces endpoint")
    parser.add_argument("--model", default=None, help="Override OPENAI_MODEL")
    parser.add_argument("--max-tokens", type=int, default=256, help="Max tokens per assistant turn")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature")
    parser.add_argument("--system", default="You are a concise helpful assistant.", help="System prompt")
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID, help="Budget task id")
    parser.add_argument(
        "--state-root",
        default=".agent-toolkit",
        help="Directory that stores budget/work ledgers",
    )
    parser.add_argument("--budget-max-tokens", type=int, default=12000, help="Budget token cap")
    parser.add_argument("--budget-max-cost", type=float, default=0.05, help="Budget cost cap (USD)")
    parser.add_argument(
        "--budget-max-tool-calls",
        type=int,
        default=120,
        help="Budget tool-call cap",
    )
    args = parser.parse_args(argv)

    base_url, env_model, api_key = _resolve_settings()
    model = args.model or env_model

    if not api_key:
        print("Missing API key. Set OPENAI_API_KEY or LLAMA_API_KEY.", file=sys.stderr)
        return 2
    if args.max_tokens <= 0:
        print("--max-tokens must be > 0", file=sys.stderr)
        return 2

    if args.budget_max_tokens <= 0 or args.budget_max_tool_calls <= 0 or args.budget_max_cost <= 0:
        print("budget caps must be > 0", file=sys.stderr)
        return 2

    assistant = FinancialConsultantBots(
        base_url=base_url,
        model=model,
        api_key=api_key,
        root=Path(args.state_root),
        task_id=args.task_id,
        max_tokens_budget=args.budget_max_tokens,
        max_cost_budget=args.budget_max_cost,
        max_tool_calls_budget=args.budget_max_tool_calls,
        max_tokens_reply=args.max_tokens,
        temperature=args.temperature,
        system_prompt=args.system,
    )

    _print_startup(base_url, model)

    while True:
        try:
            user_text = input("you> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            return 130

        if not user_text:
            continue

        if user_text.startswith("/"):
            if user_text.strip().lower() == "/status":
                status = assistant.budget.status(assistant.task_id)
                print(
                    "budget> "
                    f"decision={status['decision']} "
                    f"tokens={status['spent']['tokens']}/{status['limits']['max_tokens']} "
                    f"cost=${status['spent']['cost_usd']:.4f}/{status['limits']['max_cost_usd']:.4f} "
                    f"tools={status['spent']['tool_calls']}/{status['limits']['max_tool_calls']}"
                )
                continue
            model, should_exit = _handle_command(user_text, model)
            assistant.model = model
            if should_exit:
                return 0
            continue

        reply, status = assistant.reply(user_text)
        print(f"assistant> {reply}")
        print(
            "budget> "
            f"{status['decision']} | "
            f"tokens {status['spent']['tokens']}/{status['limits']['max_tokens']} | "
            f"cost ${status['spent']['cost_usd']:.4f}/{status['limits']['max_cost_usd']:.4f} | "
            f"tools {status['spent']['tool_calls']}/{status['limits']['max_tool_calls']}"
        )


if __name__ == "__main__":
    sys.exit(main())