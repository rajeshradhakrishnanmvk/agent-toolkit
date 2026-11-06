"""Tests for the Codespaces chat application."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import chat_app  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class ChatAppTest(unittest.TestCase):
    def test_resolve_settings_prefers_env_and_fallback_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_BASE_URL": "https://example.com/v1/",
                "OPENAI_MODEL": "my-model",
                "OPENAI_API_KEY": "",
                "LLAMA_API_KEY": "llama-token",
            },
            clear=False,
        ):
            base_url, model, key = chat_app._resolve_settings()
        self.assertEqual(base_url, "https://example.com/v1")
        self.assertEqual(model, "my-model")
        self.assertEqual(key, "llama-token")

    def test_create_chat_completion_returns_text(self) -> None:
        fake = _FakeResponse({"choices": [{"message": {"content": "pong"}}]})
        with mock.patch("urllib.request.urlopen", return_value=fake) as patched:
            text = chat_app.create_chat_completion(
                base_url="https://example.com/v1",
                model="m",
                api_key="k",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=12,
                temperature=0.1,
            )
        self.assertEqual(text, "pong")
        request = patched.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/chat/completions")

    def test_main_requires_api_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "LLAMA_API_KEY": "",
                "OPENAI_BASE_URL": "https://example.com/v1",
                "OPENAI_MODEL": "model",
            },
            clear=False,
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                code = chat_app.main([])
        self.assertEqual(code, 2)
        self.assertIn("Missing API key", err.getvalue())

    def test_financial_consultant_workflow_records_budget_and_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".agent-toolkit"
            bot = chat_app.FinancialConsultantBots(
                base_url="https://example.com/v1",
                model="model",
                api_key="token",
                root=root,
                task_id="financial-chat-test",
                max_tokens_budget=5000,
                max_cost_budget=0.05,
                max_tool_calls_budget=40,
                max_tokens_reply=120,
                temperature=0.2,
                system_prompt="You are helpful.",
            )

            with mock.patch("chat_app.create_chat_completion", return_value="Use a monthly SIP and rebalance yearly."):
                reply, status = bot.reply("I can invest 10000 INR monthly for 10 years. Help me plan.")

            self.assertIn("SIP", reply)
            self.assertEqual(status["decision"], "continue")
            self.assertGreater(status["spent"]["tokens"], 0)
            self.assertEqual(status["spent"]["tool_calls"], 3)

            listed = bot.claims.list_claims(state="all", limit=20)
            self.assertEqual(listed["count"], 3)
            states = {row["state"] for row in listed["claims"]}
            self.assertEqual(states, {"done"})

    def test_financial_consultant_stops_when_budget_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".agent-toolkit"
            bot = chat_app.FinancialConsultantBots(
                base_url="https://example.com/v1",
                model="model",
                api_key="token",
                root=root,
                task_id="financial-chat-tight",
                max_tokens_budget=10,
                max_cost_budget=0.05,
                max_tool_calls_budget=10,
                max_tokens_reply=120,
                temperature=0.2,
                system_prompt="You are helpful.",
            )

            reply, status = bot.reply("Please build me a detailed retirement portfolio plan.")
            self.assertIn("human approval", reply)
            self.assertEqual(status["decision"], "stop-and-ask-human")

    def test_question_prompt_does_not_break_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".agent-toolkit"
            bot = chat_app.FinancialConsultantBots(
                base_url="https://example.com/v1",
                model="model",
                api_key="token",
                root=root,
                task_id="financial-chat-qmark",
                max_tokens_budget=6000,
                max_cost_budget=0.05,
                max_tool_calls_budget=60,
                max_tokens_reply=120,
                temperature=0.2,
                system_prompt="You are helpful.",
            )

            with mock.patch("chat_app.create_chat_completion", return_value="Tax treatment differs by jurisdiction; verify local rules."):
                reply, _status = bot.reply("how the tax different in Kochi and Mahi in kerala ?")

            self.assertNotIn("workflow-error", reply)

    def test_trace_contains_handoffs_and_claim_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".agent-toolkit"
            bot = chat_app.FinancialConsultantBots(
                base_url="https://example.com/v1",
                model="model",
                api_key="token",
                root=root,
                task_id="financial-chat-trace",
                max_tokens_budget=6000,
                max_cost_budget=0.05,
                max_tool_calls_budget=60,
                max_tokens_reply=120,
                temperature=0.2,
                system_prompt="You are helpful.",
            )

            with mock.patch("chat_app.create_chat_completion", return_value="Build emergency fund first."):
                bot.reply("I have 20000 monthly surplus. How should I split saving and investing?")

            self.assertIsNotNone(bot.last_trace)
            assert bot.last_trace is not None
            self.assertEqual(bot.last_trace.get("outcome"), "ok")
            self.assertEqual(len(bot.last_trace.get("handoffs", [])), 2)
            self.assertEqual(len(bot.last_trace.get("claims", [])), 6)

            rendered = chat_app._format_trace(bot.last_trace)
            self.assertIn("trace> handoffs", rendered)
            self.assertIn("intake->analysis", rendered)
            self.assertIn("analysis->advisor", rendered)
            self.assertIn("trace> claim lifecycle", rendered)


if __name__ == "__main__":
    unittest.main()