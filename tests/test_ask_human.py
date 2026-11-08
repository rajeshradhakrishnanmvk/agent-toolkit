"""Tests for the week-4 approval ledger, its CLI/MCP surface, and the example."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ask_human import (  # noqa: E402
    ApprovalError,
    ApprovalLedger,
    handle_message,
    main,
    resolve_ledger,
)
from budget_guard import BudgetGuard  # noqa: E402

QUESTION = "Ask a human before the next costly step."
ACTION = "spend the next step on fix-flaky-test"
OTHER = "spend a different step"


def _fixed_clock(start: datetime | None = None):
    moment = start or datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    ticks = {"n": 0}

    def clock() -> datetime:
        current = moment + timedelta(seconds=ticks["n"])
        ticks["n"] += 1
        return current

    return clock


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "approvals.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def book(self, **kwargs: object) -> ApprovalLedger:
        clock = kwargs.pop("clock", _fixed_clock())
        return ApprovalLedger(self.path, clock=clock)  # type: ignore[arg-type]

    def test_ask_status_and_restart_keep_the_same_request(self) -> None:
        filed = self.book().ask("job", QUESTION, action=ACTION, note="café")
        self.assertEqual(filed["result"], "asked")
        self.assertTrue(filed["ok"])
        self.assertEqual(filed["status"], "pending")
        request_id = filed["request_id"]
        self.assertTrue(request_id.startswith("req_"))
        again = ApprovalLedger(self.path).status("job")
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["request_id"], request_id)
        self.assertEqual(again["question"], QUESTION)
        self.assertEqual(again["action"], ACTION)
        self.assertEqual(again["note"], "café")
        self.assertIsNone(again["answered_at"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["pending"]["job"], request_id)
        self.assertEqual(stored["requests"][request_id]["note"], "café")

    def test_missing_ledger_is_none_and_is_not_created(self) -> None:
        self.assertEqual(ApprovalLedger(self.path).status("job")["status"], "none")
        self.assertFalse(self.path.exists())
        listed = ApprovalLedger(self.path).list_requests()
        self.assertEqual(listed, {"count": 0, "requests": []})
        self.assertFalse(self.path.exists())

    def test_duplicate_pending_ask_writes_nothing(self) -> None:
        first = self.book().ask("job", QUESTION, action=ACTION)
        before = self.path.read_text(encoding="utf-8")
        second = ApprovalLedger(self.path).ask("job", "Another question?", action=OTHER)
        self.assertEqual(second["result"], "pending-exists")
        self.assertFalse(second["ok"])
        self.assertEqual(second["request_id"], first["request_id"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(ApprovalLedger(self.path).status("job")["question"], QUESTION)

    def test_denied_stays_denied_and_a_new_ask_is_a_new_id(self) -> None:
        book = self.book()
        filed = book.ask("job", QUESTION, action=ACTION)
        denied = book.answer(
            "job",
            filed["request_id"],
            "denied",
            question=QUESTION,
            action=ACTION,
            by="riya",
            note="not now",
        )
        self.assertEqual(denied["result"], "denied")
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(denied["by"], "riya")
        self.assertEqual(denied["answer_note"], "not now")
        before = self.path.read_text(encoding="utf-8")
        overwrite = ApprovalLedger(self.path).answer(
            "job",
            filed["request_id"],
            "approved",
            question=QUESTION,
            action=ACTION,
        )
        self.assertEqual(overwrite["result"], "already-decided")
        self.assertFalse(overwrite["ok"])
        self.assertEqual(overwrite["status"], "denied")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        nxt = ApprovalLedger(self.path).ask("job", QUESTION, action=ACTION)
        self.assertEqual(nxt["result"], "asked")
        self.assertNotEqual(nxt["request_id"], filed["request_id"])
        self.assertEqual(ApprovalLedger(self.path).status("job")["status"], "pending")
        inspected = ApprovalLedger(self.path).status("job", request_id=filed["request_id"])
        self.assertEqual(inspected["status"], "denied")
        self.assertEqual(inspected["request_id"], filed["request_id"])

    def test_stale_request_id_does_not_approve_the_later_ask(self) -> None:
        book = self.book()
        first = book.ask("job", QUESTION, action=ACTION)
        book.answer(
            "job", first["request_id"], "approved", question=QUESTION, action=ACTION
        )
        second = book.ask("job", "May the next step spend 1000 tokens?", action=OTHER)
        stale = book.answer(
            "job",
            first["request_id"],
            "approved",
            question=QUESTION,
            action=ACTION,
        )
        self.assertEqual(stale["result"], "already-decided")
        self.assertEqual(stale["request_id"], first["request_id"])
        current = book.status("job")
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["request_id"], second["request_id"])
        self.assertEqual(current["action"], OTHER)
        mismatched = book.answer(
            "job",
            second["request_id"],
            "approved",
            question=QUESTION,
            action=ACTION,
        )
        self.assertEqual(mismatched["result"], "mismatch")
        self.assertFalse(mismatched["ok"])
        self.assertEqual(book.status("job")["status"], "pending")
        unknown = "req_" + "ab" * 16
        absent = book.answer(
            "job", unknown, "approved", question=QUESTION, action=ACTION
        )
        self.assertEqual(absent["result"], "absent")
        self.assertFalse(absent["ok"])
        self.assertEqual(book.status("job")["request_id"], second["request_id"])

    def test_answer_for_another_task_does_not_decide_this_one(self) -> None:
        book = self.book()
        filed = book.ask("job", QUESTION, action=ACTION)
        other = book.answer(
            "other-task",
            filed["request_id"],
            "approved",
            question=QUESTION,
            action=ACTION,
        )
        self.assertEqual(other["result"], "mismatch")
        self.assertEqual(book.status("job")["status"], "pending")
        with self.assertRaises(ApprovalError):
            book.status("other-task", request_id=filed["request_id"])

    def test_approval_does_not_change_the_budget(self) -> None:
        budget_path = self.path.parent / "budget.json"
        guard = BudgetGuard(budget_path)
        guard.open_task("job", max_tokens=100)
        stopped = guard.check("job", tokens=1000)
        self.assertEqual(stopped["decision"], "stop-and-ask-human")
        book = self.book()
        filed = book.ask("job", stopped["ask_human"], action=ACTION)
        book.answer(
            "job",
            filed["request_id"],
            "approved",
            question=stopped["ask_human"],
            action=ACTION,
        )
        self.assertEqual(guard.status("job")["spent"]["tokens"], 0)
        self.assertEqual(guard.status("job")["decision"], "continue")

    def test_bad_input_and_a_full_ledger(self) -> None:
        book = self.book()
        with self.assertRaises(ApprovalError):
            book.ask("job", "   ", action=ACTION)
        with self.assertRaises(ApprovalError):
            book.ask("job", QUESTION, action="")
        with self.assertRaises(ApprovalError):
            book.ask("has space", QUESTION, action=ACTION)
        with self.assertRaises(ApprovalError):
            book.ask("job", "line\nbreak", action=ACTION)
        filed = book.ask("job", QUESTION, action=ACTION)
        with self.assertRaises(ApprovalError):
            book.answer("job", "nope", "approved", question=QUESTION, action=ACTION)
        with self.assertRaises(ApprovalError):
            book.answer(
                "job", filed["request_id"], "yes", question=QUESTION, action=ACTION
            )
        tiny = ApprovalLedger(self.path.parent / "tiny.json", max_requests=1)
        tiny.ask("job", QUESTION, action=ACTION)
        with self.assertRaises(ApprovalError) as caught:
            tiny.ask("other", QUESTION, action=ACTION)
        self.assertIn("was not changed", str(caught.exception))
        self.assertEqual(len(json.loads((self.path.parent / "tiny.json").read_text())["requests"]), 1)

    def test_malformed_ledger_is_refused_and_left_in_place(self) -> None:
        samples = [
            "",
            "{",
            "[]",
            '{"version": 2, "requests": {}, "pending": {}}',
            '{"version": 1, "requests": []}',
            json.dumps(
                {
                    "version": 1,
                    "requests": {
                        "req_" + "ab" * 16: {
                            "request_id": "req_" + "ab" * 16,
                            "task_id": "job",
                            "question": QUESTION,
                            "action": ACTION,
                            "status": "approved",
                            "asked_at": "2026-11-08T12:00:00.000+00:00",
                            "note": "",
                            "answered_at": None,
                            "by": "",
                            "answer_note": "",
                        }
                    },
                    "pending": {},
                }
            ),
        ]
        for raw in samples:
            self.path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ApprovalError):
                ApprovalLedger(self.path).status("job")
            self.assertEqual(self.path.read_text(encoding="utf-8"), raw)
        self.path.write_text('{"version": 1, "requests": {}, "pending": {}}\n', encoding="utf-8")
        with mock.patch("ask_human.MAX_LEDGER_BYTES", 8):
            with self.assertRaises(ApprovalError) as caught:
                ApprovalLedger(self.path).list_requests()
        self.assertIn("refusing to load", str(caught.exception))

    def test_list_filters_and_two_pending_rows_are_corrupt(self) -> None:
        book = self.book()
        first = book.ask("job", QUESTION, action=ACTION)
        book.answer("job", first["request_id"], "approved", question=QUESTION, action=ACTION)
        book.ask("job", "Second question for the same task?", action=OTHER)
        book.ask("other", QUESTION, action=ACTION)
        pending = book.list_requests(state="pending")
        self.assertEqual(pending["count"], 2)
        approved = book.list_requests(task_id="job", state="approved")
        self.assertEqual(approved["count"], 1)
        self.assertEqual(approved["requests"][0]["request_id"], first["request_id"])
        self.assertEqual(book.list_requests(limit=1)["truncated"], True)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        extra_id = "req_" + "cd" * 16
        raw["requests"][extra_id] = dict(raw["requests"][first["request_id"]])
        raw["requests"][extra_id]["request_id"] = extra_id
        raw["requests"][extra_id]["status"] = "pending"
        raw["requests"][extra_id]["answered_at"] = None
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ApprovalError):
            ApprovalLedger(self.path).status("job")

    def test_two_processes_one_ask_and_one_answer(self) -> None:
        filed = self.book().ask("job", QUESTION, action=ACTION)
        code = (
            "import json,sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from ask_human import ApprovalLedger\n"
            "print(json.dumps(ApprovalLedger(sys.argv[2]).ask('same', sys.argv[3], action=sys.argv[4])))\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(ROOT), str(self.path), QUESTION, ACTION],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out)["result"])
        self.assertCountEqual(results, ["asked", "pending-exists"])
        answer_code = (
            "import json,sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from ask_human import ApprovalLedger\n"
            "print(json.dumps(ApprovalLedger(sys.argv[2]).answer("
            "sys.argv[3], sys.argv[4], 'approved', question=sys.argv[5], action=sys.argv[6])))\n"
        )
        open_id = ApprovalLedger(self.path).status("same")["request_id"]
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    answer_code,
                    str(ROOT),
                    str(self.path),
                    "same",
                    open_id,
                    QUESTION,
                    ACTION,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out)["result"])
        self.assertCountEqual(results, ["approved", "already-decided"])
        self.assertEqual(ApprovalLedger(self.path).status("same")["status"], "approved")
        self.assertEqual(ApprovalLedger(self.path).status("job")["request_id"], filed["request_id"])

    def test_distinct_tasks_from_several_threads(self) -> None:
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                result = ApprovalLedger(self.path).ask(
                    f"job-{index}", QUESTION, action=ACTION
                )
                if result["result"] != "asked":
                    raise ApprovalError(result["result"])
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(ApprovalLedger(self.path).list_requests()["count"], 8)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = self.root / "approvals.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip_exit_codes_and_explain(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "ask",
                    "job",
                    "--question",
                    QUESTION,
                    "--action",
                    ACTION,
                    "--ledger",
                    str(self.ledger),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        filed = json.loads(out.getvalue())
        with redirect_stdout(io.StringIO()):
            refused = main(
                [
                    "ask",
                    "job",
                    "--question",
                    "Other?",
                    "--action",
                    OTHER,
                    "--ledger",
                    str(self.ledger),
                ]
            )
        self.assertEqual(refused, 3)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "answer",
                    "job",
                    "--request-id",
                    filed["request_id"],
                    "--decision",
                    "approved",
                    "--question",
                    QUESTION,
                    "--action",
                    ACTION,
                    "--ledger",
                    str(self.ledger),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "approved")
        err = io.StringIO()
        with redirect_stderr(err):
            bad = main(
                [
                    "answer",
                    "job",
                    "--request-id",
                    "not-an-id",
                    "--decision",
                    "approved",
                    "--question",
                    QUESTION,
                    "--action",
                    ACTION,
                    "--ledger",
                    str(self.ledger),
                ]
            )
        self.assertEqual(bad, 1)
        self.assertIn("request_id", err.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["explain"]), 0)
        self.assertIn("local record", out.getvalue().lower())
        self.assertIn("does not raise a budget", out.getvalue())

    def test_status_and_list_cli(self) -> None:
        ApprovalLedger(self.ledger).ask("job", QUESTION, action=ACTION)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["status", "missing", "--ledger", str(self.ledger)])
        self.assertEqual(code, 0)
        self.assertIn("none", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["list", "--task", "job", "--state", "pending", "--ledger", str(self.ledger), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["count"], 1)


class PathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.decoy = self.root / "decoy"
        self.repo.mkdir()
        self.decoy.mkdir()
        self._cwd = Path.cwd()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_relative_ledger_uses_root_not_cwd(self) -> None:
        os.chdir(self.decoy)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "ask",
                    "job",
                    "--question",
                    QUESTION,
                    "--action",
                    ACTION,
                    "--root",
                    str(self.repo),
                    "--ledger",
                    "approvals.json",
                    "--json",
                ]
            )
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue((self.repo / "approvals.json").is_file())
        self.assertFalse((self.decoy / "approvals.json").exists())
        env = os.environ.copy()
        env["ASK_HUMAN_LEDGER"] = "../outside.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "ask_human.py"),
                "status",
                "job",
                "--root",
                str(self.repo),
            ],
            cwd=self.decoy,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("escapes the root", proc.stderr)
        self.assertFalse((self.root / "outside.json").exists())
        self.assertEqual(
            resolve_ledger(self.repo, None).name,
            "approvals.json",
        )


class McpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_protocol_unknown_tool_and_errors(self) -> None:
        from ask_human import _Defaults

        defaults = _Defaults(self.root, self.root / "approvals.json")
        init = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            defaults,
        )
        assert init is not None
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "ask-human")
        self.assertIsNone(
            handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, defaults)
        )
        unknown = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "approval_nope", "arguments": {}},
            },
            defaults,
        )
        assert unknown is not None
        self.assertEqual(unknown["error"]["code"], -32602)
        missing = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "approval_ask", "arguments": {"task_id": "job"}},
            },
            defaults,
        )
        assert missing is not None
        self.assertTrue(missing["result"]["isError"])
        bad = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "approval_answer",
                    "arguments": {
                        "task_id": "job",
                        "request_id": "req_" + "ab" * 16,
                        "decision": "yes",
                        "question": QUESTION,
                        "action": ACTION,
                    },
                },
            },
            defaults,
        )
        assert bad is not None
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("approved or denied", bad["result"]["content"][0]["text"])

    def test_stdio_session(self) -> None:
        ledger = self.root / "approvals.json"
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "approval_ask",
                    "arguments": {"task_id": "job", "question": QUESTION, "action": ACTION},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "approval_ask",
                    "arguments": {"task_id": "job", "question": "Again?", "action": OTHER},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "approval_status", "arguments": {"task_id": "job"}},
            },
        ]
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ask_human.py"), "serve", "--root", str(self.root), "--ledger", str(ledger)],
            input=payload,
            text=True,
            capture_output=True,
            cwd=self.root,
            timeout=15,
            check=False,
        )
        # cwd is the server root here; a second session below uses another cwd.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 5, proc.stdout)
        bodies = [json.loads(line) for line in lines]
        self.assertEqual([body["id"] for body in bodies], [1, 2, 3, 4, 5])
        names = [tool["name"] for tool in bodies[1]["result"]["tools"]]
        self.assertEqual(
            names,
            [
                "approval_ask",
                "approval_answer",
                "approval_status",
                "approval_list",
                "approval_explain",
            ],
        )
        filed = json.loads(bodies[2]["result"]["content"][0]["text"])
        self.assertEqual(filed["result"], "asked")
        self.assertFalse(bodies[2]["result"]["isError"])
        duplicate = json.loads(bodies[3]["result"]["content"][0]["text"])
        self.assertEqual(duplicate["result"], "pending-exists")
        self.assertFalse(bodies[3]["result"]["isError"])
        status = json.loads(bodies[4]["result"]["content"][0]["text"])
        self.assertEqual(status["request_id"], filed["request_id"])
        follow = [
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "approval_answer",
                    "arguments": {
                        "task_id": "job",
                        "request_id": filed["request_id"],
                        "decision": "approved",
                        "question": "wrong question",
                        "action": ACTION,
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "approval_answer",
                    "arguments": {
                        "task_id": "job",
                        "request_id": filed["request_id"],
                        "decision": "approved",
                        "question": QUESTION,
                        "action": ACTION,
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "approval_answer",
                    "arguments": {
                        "task_id": "job",
                        "request_id": filed["request_id"],
                        "decision": "denied",
                        "question": QUESTION,
                        "action": ACTION,
                    },
                },
            },
        ]
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "ask_human.py"),
                "serve",
                "--root",
                str(self.root),
                "--ledger",
                "approvals.json",
            ],
            input="".join(json.dumps(message) + "\n" for message in follow),
            text=True,
            capture_output=True,
            cwd=self.root.parent,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bodies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        mismatch = json.loads(bodies[0]["result"]["content"][0]["text"])
        self.assertEqual(mismatch["result"], "mismatch")
        approved = json.loads(bodies[1]["result"]["content"][0]["text"])
        self.assertEqual(approved["result"], "approved")
        stale = json.loads(bodies[2]["result"]["content"][0]["text"])
        self.assertEqual(stale["result"], "already-decided")
        self.assertEqual(stale["status"], "approved")


class DemoAndSourceTest(unittest.TestCase):
    def test_example_stops_stores_and_does_not_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "demo_ask_human.py")],
                cwd=tmp,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("decision  stop-and-ask-human", proc.stdout)
        self.assertIn("status    pending", proc.stdout)
        self.assertIn("answered  approved", proc.stdout)
        self.assertIn("inspected approved req_", proc.stdout)
        self.assertIn("budget    0 tokens spent", proc.stdout)
        self.assertIn("did not raise the budget", proc.stdout)
        self.assertIn("does not check who answered", proc.stdout)

    def test_sources_do_not_phone_home(self) -> None:
        for name in ("ask_human.py", "demo_ask_human.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)
            self.assertNotIn("urllib", text, name)
            self.assertNotIn("socket", text, name)

    def test_readme_and_page_list_week_4(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Week 4 — Approval ledger", readme)
        self.assertIn("ask_human.py", readme)
        self.assertIn("demo_ask_human.py", readme)
        self.assertIn("pending-exists", readme)
        self.assertIn("live-test harness", readme)
        page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("ask_human.py", page)
        self.assertIn("Approval ledger", page)
        self.assertIn("demo_ask_human.py", page)
        self.assertGreaterEqual(page.count(">Live<"), 4)
        self.assertEqual(page.count("Coming soon"), 0)
        self.assertEqual(page.count("<script>"), 1)
        self.assertEqual(page.count("stats.srv1536099.hstgr.cloud/hit"), 1)


if __name__ == "__main__":
    unittest.main()
