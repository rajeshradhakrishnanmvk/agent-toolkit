"""Tests for the week-1 budget guard, its CLI/MCP surface, and the project page."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from budget_guard import (  # noqa: E402
    CONTINUE,
    STOP_AND_ASK,
    WARN,
    BudgetError,
    BudgetGuard,
    estimate_cost_usd,
    handle_message,
    main,
)

COUNTER = """<script>
(function () {
  if (navigator.doNotTrack === '1' || !navigator.sendBeacon) return;
  var q = new URLSearchParams(location.search);
  var data = JSON.stringify({
    path: location.pathname,
    referrer: document.referrer || '',
    utm_source: q.get('utm_source') || '',
    utm_medium: q.get('utm_medium') || '',
    utm_campaign: q.get('utm_campaign') || '',
    width: (window.screen && screen.width) || window.innerWidth || 0
  });
  navigator.sendBeacon('https://stats.srv1536099.hstgr.cloud/hit', data);
})();
</script>"""


class GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ledger.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def guard(self, **kwargs: object) -> BudgetGuard:
        return BudgetGuard(self.path, **kwargs)  # type: ignore[arg-type]

    def test_open_requires_a_limit_and_a_sane_id(self) -> None:
        guard = self.guard()
        with self.assertRaises(BudgetError):
            guard.open_task("job")
        for bad in ("", "-rf", "../x", "has space", "a" * 129, "ok/no"):
            with self.assertRaises(BudgetError):
                guard.open_task(bad, max_tokens=10)
        self.assertFalse(self.path.exists())

    def test_check_does_not_spend_or_create_a_missing_ledger(self) -> None:
        missing = Path(self._tmp.name) / "nope" / "ledger.json"
        with self.assertRaises(BudgetError):
            BudgetGuard(missing).check("job", tokens=1)
        self.assertFalse(missing.parent.exists())

        guard = self.guard()
        guard.open_task("job", max_tokens=1000)
        before = self.path.read_text(encoding="utf-8")
        decision = guard.check("job", tokens=1001)
        self.assertEqual(decision["decision"], STOP_AND_ASK)
        self.assertFalse(decision["ok_to_proceed"])
        self.assertEqual(decision["spent"]["tokens"], 0)
        self.assertEqual(decision["projected"]["tokens"], 1001)
        self.assertIn("would reach", decision["reason"])
        self.assertIsNotNone(decision["ask_human"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_warn_and_stop_boundaries(self) -> None:
        guard = self.guard()
        guard.open_task("job", max_tokens=1000, warn_ratio=0.8)
        self.assertEqual(guard.check("job", tokens=799)["decision"], CONTINUE)
        warned = guard.check("job", tokens=800)
        self.assertEqual(warned["decision"], WARN)
        self.assertTrue(warned["ok_to_proceed"])
        self.assertIn("80%", warned["reason"])
        at_limit = guard.record("job", tokens=1000, note="fill")
        self.assertEqual(at_limit["decision"], WARN)
        self.assertEqual(at_limit["spent"]["tokens"], 1000)
        over = guard.check("job", tokens=1)
        self.assertEqual(over["decision"], STOP_AND_ASK)
        self.assertEqual(guard.status("job")["spent"]["tokens"], 1000)

    def test_warn_ratio_of_one_warns_only_at_the_ceiling(self) -> None:
        guard = self.guard()
        guard.open_task("job", max_tokens=10, warn_ratio=1)
        self.assertEqual(guard.check("job", tokens=9)["decision"], CONTINUE)
        self.assertEqual(guard.check("job", tokens=10)["decision"], WARN)
        self.assertEqual(guard.check("job", tokens=11)["decision"], STOP_AND_ASK)

    def test_tool_calls_can_stop_while_tokens_are_fine(self) -> None:
        guard = self.guard()
        guard.open_task("job", max_tokens=10_000, max_tool_calls=1)
        decision = guard.check("job", tokens=10, tool_calls=2)
        self.assertEqual(decision["decision"], STOP_AND_ASK)
        self.assertIn("tool calls", decision["reason"])
        self.assertNotIn("tokens would", decision["reason"])

    def test_record_persists_and_keeps_an_overage(self) -> None:
        self.guard().open_task("job", max_tokens=100, max_cost_usd=0.05)
        first = BudgetGuard(self.path)
        self.assertEqual(first.record("job", cost_usd=0.04)["decision"], WARN)
        second = BudgetGuard(self.path)
        recorded = second.record("job", cost_usd=0.02, note="extra")
        self.assertEqual(recorded["decision"], STOP_AND_ASK)
        self.assertEqual(recorded["spent"]["cost_usd"], 0.06)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["tasks"]["job"]["spent"]["cost_usd"], 0.06)
        self.assertEqual(raw["tasks"]["job"]["events"][-1]["note"], "extra")

    def test_money_rounds_and_does_not_drift(self) -> None:
        guard = self.guard()
        guard.open_task("job", max_cost_usd=1)
        for _ in range(3):
            guard.record("job", cost_usd=0.1)
        self.assertEqual(guard.status("job")["spent"]["cost_usd"], 0.3)

    def test_reset_clears_spend_and_restart_refuses_without_it(self) -> None:
        guard = self.guard()
        guard.open_task("job", max_tokens=50)
        guard.record("job", tokens=10, note="keep")
        with self.assertRaises(BudgetError):
            guard.open_task("job", max_tokens=50)
        self.assertEqual(guard.status("job")["spent"]["tokens"], 10)
        fresh = guard.open_task("job", max_tokens=50, reset=True)
        self.assertEqual(fresh["spent"]["tokens"], 0)
        self.assertEqual(fresh["event_count"], 0)
        self.assertEqual(fresh["decision"], CONTINUE)

    def test_wall_clock_includes_idle_time(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = [start]
        guard = self.guard(clock=lambda: now[0])
        guard.open_task("job", max_wall_clock_seconds=10, warn_ratio=0.8)
        now[0] = start + timedelta(seconds=8)
        self.assertEqual(guard.check("job")["decision"], WARN)
        now[0] = start + timedelta(seconds=10)
        self.assertEqual(guard.status("job")["decision"], WARN)
        now[0] = start + timedelta(seconds=11)
        stopped = guard.status("job")
        self.assertEqual(stopped["decision"], STOP_AND_ASK)
        self.assertIn("wall-clock", stopped["reason"])
        guard.open_task("job", max_wall_clock_seconds=10, reset=True)
        self.assertEqual(guard.status("job")["decision"], CONTINUE)
        self.assertLess(guard.status("job")["elapsed_seconds"], 1)

    def test_event_log_trims_but_totals_keep_the_dropped_spend(self) -> None:
        guard = self.guard(max_events=2)
        guard.open_task("job", max_tokens=100)
        for i in range(3):
            guard.record("job", tokens=1, note=f"n{i}")
        status = guard.status("job")
        self.assertEqual(status["spent"]["tokens"], 3)
        self.assertEqual(status["event_count"], 2)
        self.assertEqual(status["events_dropped"], 1)
        notes = [event["note"] for event in json.loads(self.path.read_text())["tasks"]["job"]["events"]]
        self.assertEqual(notes, ["n1", "n2"])

    def test_concurrent_records_sum(self) -> None:
        self.guard().open_task("job", max_tokens=100000)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                local = BudgetGuard(self.path)
                for _ in range(25):
                    local.record("job", tokens=1)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(BudgetGuard(self.path).status("job")["spent"]["tokens"], 200)

    def test_rejects_bad_spend_notes_and_corrupt_files(self) -> None:
        guard = self.guard()
        guard.open_task("job", max_tokens=10)
        with self.assertRaises(BudgetError):
            guard.record("job", tokens=-1)
        with self.assertRaises(BudgetError):
            guard.record("job", tokens=True)  # type: ignore[arg-type]
        with self.assertRaises(BudgetError):
            guard.record("job", note="line\nbreak")
        with self.assertRaises(BudgetError):
            guard.record("job", note="x" * 201)
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(BudgetError):
            BudgetGuard(self.path).status("job")

    def test_unknown_task_names_the_open_ones(self) -> None:
        guard = self.guard()
        guard.open_task("alpha", max_tokens=1)
        with self.assertRaises(BudgetError) as caught:
            guard.status("beta")
        self.assertIn("alpha", str(caught.exception))

    def test_env_ledger_path(self) -> None:
        with mock.patch.dict(os.environ, {"BUDGET_GUARD_LEDGER": str(self.path)}):
            guard = BudgetGuard()
        self.assertEqual(guard.path, self.path)

    def test_estimate_cost_uses_only_the_rates_you_pass(self) -> None:
        self.assertEqual(estimate_cost_usd(2000, 500, 3, 15), 0.0135)
        with self.assertRaises(BudgetError):
            estimate_cost_usd(1, 1, -1, 1)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ledger.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        command, rest = args[0], args[1:]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([command, "--ledger", str(self.path), *rest])
        return code, out.getvalue(), err.getvalue()

    def test_round_trip_and_stop_exit_code(self) -> None:
        code, out, err = self.run_cli("open", "job", "--max-tokens", "100")
        self.assertEqual(code, 0, err)
        self.assertIn("continue", out)
        code, out, err = self.run_cli("check", "job", "--tokens", "90", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], WARN)
        code, out, err = self.run_cli("record", "job", "--tokens", "90", "--note", "draft")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("check", "job", "--tokens", "20")
        self.assertEqual(code, 3, err)
        self.assertIn("stop-and-ask-human", out)
        self.assertIn("ask", out)
        status = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(status["tasks"]["job"]["spent"]["tokens"], 90)

    def test_cost_command_and_usage_errors(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                [
                    "cost",
                    "--input-tokens",
                    "2000",
                    "--output-tokens",
                    "500",
                    "--usd-per-million-input",
                    "3",
                    "--usd-per-million-output",
                    "15",
                    "--json",
                ]
            )
        self.assertEqual(code, 0, err.getvalue())
        body = json.loads(out.getvalue())
        self.assertEqual(body["cost_usd"], 0.0135)
        self.assertIn("does not know provider prices", body["note"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main([])
        self.assertEqual(ctx.exception.code, 2)


class McpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ledger.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handle_message_protocol_and_errors(self) -> None:
        init = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            self.path,
        )
        assert init is not None
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "budget-guard")
        fallback = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            },
            self.path,
        )
        assert fallback is not None
        self.assertEqual(fallback["result"]["protocolVersion"], "2024-11-05")
        self.assertIsNone(
            handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.path)
        )
        missing = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "budget_check", "arguments": {"task_id": "job", "tokens": 1}},
            },
            self.path,
        )
        assert missing is not None
        self.assertTrue(missing["result"]["isError"])
        unknown = handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"}, self.path)
        assert unknown is not None
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_stdio_session(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "budget_open",
                    "arguments": {"task_id": "job", "max_tokens": 100},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "budget_check",
                    "arguments": {"task_id": "job", "tokens": 90},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "budget_record",
                    "arguments": {"task_id": "job", "tokens": 90, "note": "draft"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "budget_status", "arguments": {"task_id": "job"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "budget_check",
                    "arguments": {"task_id": "job", "tokens": 20},
                },
            },
        ]
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "budget_guard.py"), "serve", "--ledger", str(self.path)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 7)
        bodies = [json.loads(line) for line in lines]
        self.assertEqual([body["id"] for body in bodies], [1, 2, 3, 4, 5, 6, 7])
        names = [tool["name"] for tool in bodies[1]["result"]["tools"]]
        self.assertEqual(
            names, ["budget_open", "budget_check", "budget_record", "budget_status"]
        )
        checked = json.loads(bodies[3]["result"]["content"][0]["text"])
        self.assertEqual(checked["decision"], WARN)
        self.assertEqual(checked["spent"]["tokens"], 0)
        recorded = json.loads(bodies[4]["result"]["content"][0]["text"])
        self.assertEqual(recorded["spent"]["tokens"], 90)
        stopped = json.loads(bodies[6]["result"]["content"][0]["text"])
        self.assertEqual(stopped["decision"], STOP_AND_ASK)
        self.assertFalse(stopped["ok_to_proceed"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["tasks"]["job"]["spent"]["tokens"], 90)


class DemoAndPageTest(unittest.TestCase):
    def test_demo_warns_then_stops_without_recording_the_last_step(self) -> None:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        ledger = ROOT / ".agent-toolkit" / "demo-ledger.json"
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "demo.py")],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("warn", proc.stdout)
            self.assertIn("near the limit", proc.stdout)
            self.assertIn(
                "stop and ask a human: tokens would reach 2,600, over the limit of 2,500",
                proc.stdout,
            )
            self.assertIn("step 4 was not recorded", proc.stdout)
            data = json.loads(ledger.read_text(encoding="utf-8"))
            task = data["tasks"]["fix-flaky-test"]
            self.assertEqual(task["spent"]["tokens"], 2100)
            self.assertEqual(task["spent"]["tool_calls"], 3)
            self.assertEqual(len(task["events"]), 3)

    def test_guard_source_does_not_phone_home(self) -> None:
        text = (ROOT / "budget_guard.py").read_text(encoding="utf-8")
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("urllib", text)
        self.assertNotIn("socket", text)

    def test_every_page_has_the_exact_counter(self) -> None:
        pages = sorted((ROOT / "docs").glob("**/*.html"))
        self.assertTrue(pages)
        for page in pages:
            html = page.read_text(encoding="utf-8")
            self.assertEqual(html.count("<script>"), 1, page)
            self.assertEqual(html.count("stats.srv1536099.hstgr.cloud/hit"), 1, page)
            self.assertIn(COUNTER, html)
            rest = html.split(COUNTER, 1)[1]
            self.assertRegex(rest, r"^\s*</body>\s*</html>\s*$")
            lowered = html.lower()
            self.assertNotIn("fonts.googleapis", lowered)
            self.assertNotIn("<script src", lowered)
            self.assertIn(
                "One small, single-file tool for AI agent builders, every week.", html
            )
            self.assertIn("Budget guard", html)
            self.assertIn("Handoff checker", html)
            self.assertIn("Work-claim ledger", html)
            self.assertGreaterEqual(html.count(">Live<"), 2)
            self.assertGreaterEqual(html.count("Coming soon"), 1)
            self.assertIn(">Live<", html)
            self.assertIn("https://github.com/rajeshradhakrishnanmvk/agent-toolkit", html)
            self.assertIn('name="viewport"', html)


if __name__ == "__main__":
    unittest.main()
