"""Tests for the week-3 work-claim ledger, its CLI/MCP surface, and the demo."""

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

from work_claim import (  # noqa: E402
    DEFAULT_TTL,
    WorkClaimError,
    WorkLedger,
    handle_message,
    main,
    resolve_ledger,
)


def _at(seconds: int = 0) -> datetime:
    return datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(seconds=seconds)


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "claims.json"
        self.now = [_at(0)]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ledger(self, **kwargs: object) -> WorkLedger:
        return WorkLedger(self.path, clock=lambda: self.now[0], **kwargs)  # type: ignore[arg-type]

    def test_take_blocks_a_second_owner_until_the_lease_expires(self) -> None:
        book = self.ledger()
        taken = book.take("fix-flaky-test", "agent-a", ttl_seconds=60, note="editing")
        self.assertEqual(taken["result"], "taken")
        self.assertTrue(taken["ok"])
        self.assertEqual(taken["expires_in"], 60)
        self.assertEqual(taken["state"], "active")
        self.assertNotIn("takeable", taken)
        self.assertEqual(taken["note"], "editing")

        before = self.path.read_bytes()
        self.now[0] = _at(10)
        held = book.take("fix-flaky-test", "agent-a", ttl_seconds=60)
        self.assertEqual(held["result"], "held")
        self.assertEqual(held["expires_in"], 50)
        self.assertEqual(self.path.read_bytes(), before)

        busy = book.take("fix-flaky-test", "agent-b", ttl_seconds=60)
        self.assertEqual(busy["result"], "busy")
        self.assertFalse(busy["ok"])
        self.assertEqual(busy["owner"], "agent-a")
        self.assertEqual(busy["expires_in"], 50)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

        self.now[0] = _at(59)
        self.assertEqual(book.take("fix-flaky-test", "agent-b")["result"], "busy")
        self.now[0] = _at(60)
        listed = book.list_claims(state="stale")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["claims"][0]["state"], "expired")
        self.assertTrue(listed["claims"][0]["takeable"])
        self.assertEqual(listed["claims"][0]["expires_in"], 0)

        over = book.take("fix-flaky-test", "agent-b", ttl_seconds=30)
        self.assertEqual(over["result"], "taken-over")
        self.assertTrue(over["ok"])
        self.assertEqual(over["previous_owner"], "agent-a")
        self.assertEqual(over["owner"], "agent-b")
        self.assertEqual(over["expires_in"], 30)
        self.assertNotIn("note", over)

        denied = book.renew("fix-flaky-test", "agent-a")
        self.assertEqual(denied["result"], "not-owner")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["owner"], "agent-b")
        same = book.take("fix-flaky-test", "agent-b")
        self.assertEqual(same["result"], "held")

    def test_renew_release_and_done(self) -> None:
        book = self.ledger()
        book.take("job", "agent-a", ttl_seconds=60)
        self.now[0] = _at(20)
        renewed = book.renew("job", "agent-a")
        self.assertEqual(renewed["result"], "renewed")
        self.assertEqual(renewed["expires_in"], 60)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["claims"]["job"]["status"], "claimed")
        self.assertIn("renewed_at", stored["claims"]["job"])

        self.now[0] = _at(20 + 60)
        late = book.renew("job", "agent-a", ttl_seconds=15)
        self.assertEqual(late["result"], "renewed")
        self.assertEqual(late["expires_in"], 15)

        released = book.release("job", "agent-a")
        self.assertEqual(released, {"result": "released", "ok": True, "item": "job"})
        self.assertEqual(book.list_claims()["count"], 0)
        self.assertNotIn("job", json.loads(self.path.read_text(encoding="utf-8"))["claims"])
        self.assertEqual(book.take("job", "agent-b", ttl_seconds=10)["result"], "taken")

        finished = book.done("job", "agent-b", note="shipped")
        self.assertEqual(finished["result"], "done")
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["note"], "shipped")
        self.assertNotIn("expires_in", finished)
        self.assertEqual(book.list_claims()["count"], 0)
        self.assertEqual(book.list_claims(state="done")["claims"][0]["note"], "shipped")
        again = book.done("job", "agent-b")
        self.assertEqual(again["result"], "done")
        self.assertTrue(again["ok"])
        closed = book.take("job", "agent-a")
        self.assertEqual(closed["result"], "closed")
        self.assertFalse(closed["ok"])
        reopened = book.take("job", "agent-a", reopen=True, ttl_seconds=5)
        self.assertEqual(reopened["result"], "taken-over")
        self.assertEqual(reopened["previous_owner"], "agent-b")

    def test_list_filters_limits_and_a_missing_ledger(self) -> None:
        missing = self.root / "nope" / "claims.json"
        empty = WorkLedger(missing).list_claims()
        self.assertEqual(empty, {"count": 0, "claims": []})
        self.assertFalse(missing.parent.exists())
        self.assertEqual(WorkLedger(missing).renew("job", "agent-a")["result"], "absent")

        book = self.ledger()
        book.take("b-task", "agent-b", ttl_seconds=10)
        book.take("a-task", "agent-a", ttl_seconds=10)
        book.done("b-task", "agent-b")
        open_rows = book.list_claims()
        self.assertEqual([row["item"] for row in open_rows["claims"]], ["a-task"])
        self.assertEqual(book.list_claims(state="all")["count"], 2)
        self.assertEqual(book.list_claims(owner="agent-a")["claims"][0]["item"], "a-task")
        self.assertEqual(book.list_claims(item="b-task")["claims"][0]["state"], "done")
        self.assertEqual(book.list_claims(item="missing")["count"], 0)

        wide = WorkLedger(self.root / "wide.json", clock=lambda: self.now[0])
        for index in range(3):
            wide.take(f"item-{index}", "agent-a", ttl_seconds=10)
        listed = wide.list_claims(limit=2)
        self.assertEqual(listed["count"], 2)
        self.assertEqual(listed["total"], 3)
        self.assertTrue(listed["truncated"])
        with self.assertRaises(WorkClaimError):
            wide.list_claims(state="nope")
        with self.assertRaises(WorkClaimError):
            wide.list_claims(limit=0)

    def test_same_owner_retake_after_expiry_is_taken(self) -> None:
        book = self.ledger()
        book.take("job", "agent-a", ttl_seconds=5)
        self.now[0] = _at(5)
        again = book.take("job", "agent-a", ttl_seconds=5)
        self.assertEqual(again["result"], "taken")
        self.assertNotIn("previous_owner", again)
        self.assertEqual(book.release("job", "agent-b")["result"], "not-owner")
        self.assertEqual(book.release("job", "agent-a")["result"], "released")

    def test_done_history_is_capped_and_the_ledger_can_fill(self) -> None:
        book = self.ledger(max_done=1, max_claims=2)
        book.take("old", "agent-a", ttl_seconds=10)
        book.done("old", "agent-a")
        self.now[0] = _at(1)
        book.take("new", "agent-a", ttl_seconds=10)
        book.done("new", "agent-a")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(stored["claims"]), ["new"])
        book.take("live", "agent-a", ttl_seconds=10)
        with self.assertRaises(WorkClaimError) as caught:
            book.take("extra", "agent-a", ttl_seconds=10)
        self.assertIn("full", str(caught.exception))

    def test_rejects_bad_input_notes_and_corrupt_files(self) -> None:
        book = self.ledger()
        for bad in ("", " has space", "../x", "-rf", "a" * 161):
            with self.assertRaises(WorkClaimError):
                book.take(bad, "agent-a")
        self.assertFalse(self.path.exists())
        with self.assertRaises(WorkClaimError):
            book.take("job", "agent a")
        with self.assertRaises(WorkClaimError):
            book.take("job", "agent-a", ttl_seconds=0)
        with self.assertRaises(WorkClaimError):
            book.take("job", "agent-a", ttl_seconds=True)  # type: ignore[arg-type]
        with self.assertRaises(WorkClaimError):
            book.take("job", "agent-a", ttl_seconds=86401)
        book.take("job", "agent-a", ttl_seconds=30.0)  # type: ignore[arg-type]
        with self.assertRaises(WorkClaimError):
            book.done("job", "agent-a", note="line\nbreak")
        with self.assertRaises(WorkClaimError):
            book.done("job", "agent-a", note="x" * 201)
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(WorkClaimError):
            WorkLedger(self.path).list_claims()
        self.path.write_text('{"claims": []}\n', encoding="utf-8")
        with self.assertRaises(WorkClaimError):
            WorkLedger(self.path).list_claims()
        naive = WorkLedger(self.path, clock=lambda: datetime(2026, 1, 1))
        self.path.write_text('{"version": 1, "claims": {}}\n', encoding="utf-8")
        with self.assertRaises(WorkClaimError):
            naive.take("job", "agent-a")

    def test_refuses_a_ledger_that_is_too_large(self) -> None:
        self.path.write_text('{"version": 1, "claims": {}}\n', encoding="utf-8")
        with mock.patch("work_claim.MAX_LEDGER_BYTES", 8):
            with self.assertRaises(WorkClaimError) as caught:
                WorkLedger(self.path).list_claims()
        self.assertIn("refusing to load", str(caught.exception))

    def test_note_round_trip_and_release_of_a_finished_item(self) -> None:
        book = self.ledger()
        book.take("job", "agent-a", ttl_seconds=10, note="café")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["claims"]["job"]["note"], "café")
        book.done("job", "agent-a")
        self.assertEqual(book.release("job", "agent-a")["result"], "closed")
        self.assertEqual(book.renew("job", "agent-b")["result"], "closed")
        self.assertIn("job", json.loads(self.path.read_text(encoding="utf-8"))["claims"])

    def test_two_processes_one_winner(self) -> None:
        code = (
            "import json,sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from work_claim import WorkLedger\n"
            "print(json.dumps(WorkLedger(sys.argv[2]).take('same', sys.argv[3], ttl_seconds=30)))\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(ROOT), str(self.path), owner],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for owner in ("agent-a", "agent-b")
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out)["result"])
        self.assertCountEqual(results, ["taken", "busy"])
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["claims"]), 1)

    def test_concurrent_distinct_items(self) -> None:
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                result = WorkLedger(self.path).take(f"item-{index}", "agent-a", ttl_seconds=30)
                if result["result"] != "taken":
                    raise WorkClaimError(result["result"])
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(WorkLedger(self.path).list_claims()["count"], 8)


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

    def test_cli_relative_ledger_uses_root_not_cwd(self) -> None:
        os.chdir(self.decoy)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                [
                    "take",
                    "--root",
                    str(self.repo),
                    "--ledger",
                    "claims.json",
                    "job",
                    "--owner",
                    "agent-a",
                    "--ttl",
                    "30",
                ]
            )
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue((self.repo / "claims.json").is_file())
        self.assertFalse((self.decoy / "claims.json").exists())
        self.assertFalse(Path("claims.json").exists())

        os.chdir(self.repo)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(
                ["take", "--root", ".", "--ledger", "nested/other.json", "other", "--owner", "agent-b", "--ttl", "15"]
            )
        self.assertEqual(code, 0)
        self.assertTrue((self.repo / "nested" / "other.json").is_file())

    def test_env_ledger_is_relative_to_root(self) -> None:
        os.chdir(self.decoy)
        with mock.patch.dict(os.environ, {"WORK_CLAIM_LEDGER": "from-env.json"}):
            path = resolve_ledger(self.repo, None)
            self.assertEqual(path, self.repo / "from-env.json")
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["take", "--root", str(self.repo), "job", "--owner", "agent-a", "--ttl", "10"])
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue((self.repo / "from-env.json").is_file())
            self.assertFalse((self.decoy / "from-env.json").exists())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                flagged = main(
                    [
                        "take",
                        "--root",
                        str(self.repo),
                        "--ledger",
                        "flag.json",
                        "other",
                        "--owner",
                        "agent-a",
                        "--ttl",
                        "10",
                    ]
                )
            self.assertEqual(flagged, 0)
            self.assertTrue((self.repo / "flag.json").is_file())
        with mock.patch.dict(os.environ, {"WORK_CLAIM_LEDGER": "../outside.json"}):
            with self.assertRaises(WorkClaimError):
                resolve_ledger(self.repo, None)

    def test_per_call_root_stays_under_the_server_root(self) -> None:
        os.chdir(self.decoy)
        defaults_root = self.repo
        from work_claim import _Defaults

        defaults = _Defaults(defaults_root, defaults_root / ".agent-toolkit" / "work-claims.json")
        taken = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "claim_take",
                    "arguments": {
                        "item": "job",
                        "owner": "agent-a",
                        "ttl_seconds": 30,
                        "root": "team",
                        "ledger": "claims.json",
                    },
                },
            },
            defaults,
        )
        assert taken is not None
        self.assertFalse(taken["result"]["isError"])
        self.assertTrue((self.repo / "team" / "claims.json").is_file())
        self.assertFalse((self.decoy / "team").exists())
        escaped = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "claim_take",
                    "arguments": {
                        "item": "job",
                        "owner": "agent-a",
                        "root": "..",
                        "ledger": "claims.json",
                    },
                },
            },
            defaults,
        )
        assert escaped is not None
        self.assertTrue(escaped["result"]["isError"])
        self.assertIn("escapes", escaped["result"]["content"][0]["text"])
        self.assertFalse((self.decoy / "claims.json").exists())


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        command, rest = args[0], args[1:]
        if command != "explain":
            rest = ["--root", str(self.root), *rest]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([command, *rest])
        return code, out.getvalue(), err.getvalue()

    def test_round_trip_exit_codes_and_explain(self) -> None:
        code, out, err = self.run_cli("take", "job", "--owner", "agent-a", "--ttl", "30")
        self.assertEqual(code, 0, err)
        self.assertIn("taken", out)
        self.assertIn("30s", out)
        self.assertTrue((self.root / ".agent-toolkit" / "work-claims.json").is_file())
        code, out, err = self.run_cli("take", "job", "--owner", "agent-b", "--json")
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(out)["result"], "busy")
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertIn("job", out)
        self.assertIn("agent-a", out)
        code, out, err = self.run_cli("renew", "job", "--owner", "agent-a", "--ttl", "10")
        self.assertEqual(code, 0, err)
        self.assertIn("renewed", out)
        code, out, err = self.run_cli("release", "missing", "--owner", "agent-a")
        self.assertEqual(code, 3, err)
        self.assertIn("absent", out)
        code, out, err = self.run_cli("done", "job", "--owner", "agent-a", "--note", "shipped")
        self.assertEqual(code, 0, err)
        self.assertIn("done", out)
        code, out, err = self.run_cli("list", "--state", "open")
        self.assertIn("none", out)
        code, out, err = self.run_cli("take", "nope", "--owner", "bad id")
        self.assertEqual(code, 1)
        self.assertIn("owner must be", err)
        code, out, err = self.run_cli("explain")
        self.assertEqual(code, 0, err)
        self.assertIn("busy", out)
        self.assertIn("stale", out.lower())
        code, out, err = self.run_cli("explain", "--json")
        rules = json.loads(out)
        self.assertEqual(rules["exit_codes"]["refused"], 3)
        self.assertEqual(rules["default_ttl_seconds"], DEFAULT_TTL)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main([])
        self.assertEqual(ctx.exception.code, 2)


class McpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        from work_claim import _Defaults

        self.defaults = _Defaults(self.root, self.root / "claims.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_protocol_unknown_tool_and_compact_results(self) -> None:
        for version in ("2024-11-05", "2025-03-26", "2025-06-18"):
            init = handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": version},
                },
                self.defaults,
            )
            assert init is not None
            self.assertEqual(init["result"]["protocolVersion"], version)
            self.assertEqual(init["result"]["serverInfo"]["name"], "work-claim")
        newer = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2099-01-01"},
            },
            self.defaults,
        )
        assert newer is not None
        self.assertEqual(newer["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.defaults))
        unknown_method = handle_message({"jsonrpc": "2.0", "id": 3, "method": "nope"}, self.defaults)
        assert unknown_method is not None
        self.assertEqual(unknown_method["error"]["code"], -32601)
        unknown_tool = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "claim_nope", "arguments": {}},
            },
            self.defaults,
        )
        assert unknown_tool is not None
        self.assertEqual(unknown_tool["error"]["code"], -32602)
        self.assertNotIn("result", unknown_tool)
        self.assertNotIn("isError", json.dumps(unknown_tool))
        missing_name = handle_message(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}},
            self.defaults,
        )
        assert missing_name is not None
        self.assertEqual(missing_name["error"]["code"], -32602)
        tools = handle_message({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, self.defaults)
        assert tools is not None
        names = [tool["name"] for tool in tools["result"]["tools"]]
        self.assertEqual(
            names,
            ["claim_take", "claim_renew", "claim_release", "claim_done", "claim_list", "claim_explain"],
        )
        bad = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "claim_take",
                    "arguments": {"item": "job", "owner": "agent-a", "ttl_seconds": 0, "extra": True},
                },
            },
            self.defaults,
        )
        assert bad is not None
        self.assertTrue(bad["result"]["isError"])
        self.assertFalse((self.root / "claims.json").exists())

    def test_stdio_session_resolves_against_root(self) -> None:
        repo = self.root / "repo"
        decoy = self.root / "decoy"
        repo.mkdir()
        decoy.mkdir()
        messages = [
            "not-json",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-01",
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
                    "name": "claim_take",
                    "arguments": {"item": "job", "owner": "agent-a", "ttl_seconds": 60, "ledger": "claims.json"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "claim_take",
                    "arguments": {"item": "job", "owner": "agent-b", "ledger": "claims.json"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "claim_list",
                    "arguments": {"ledger": "claims.json", "state": "open"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "claim_explain", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "claim_missing", "arguments": {}},
            },
        ]
        payload = "".join(
            (message if isinstance(message, str) else json.dumps(message)) + "\n" for message in messages
        )
        env = os.environ.copy()
        env.pop("WORK_CLAIM_LEDGER", None)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "work_claim.py"), "serve", "--root", str(repo)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
            cwd=decoy,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertTrue((repo / "claims.json").is_file())
        self.assertFalse((decoy / "claims.json").exists())
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 8)
        bodies = [json.loads(line) for line in lines]
        self.assertEqual(bodies[0]["error"]["code"], -32700)
        self.assertEqual(bodies[1]["result"]["protocolVersion"], "2025-06-18")
        names = [tool["name"] for tool in bodies[2]["result"]["tools"]]
        self.assertEqual(names[0], "claim_take")
        self.assertEqual(names[-1], "claim_explain")
        taken_text = bodies[3]["result"]["content"][0]["text"]
        self.assertNotIn("\n", taken_text)
        taken = json.loads(taken_text)
        self.assertEqual(taken["result"], "taken")
        self.assertTrue(taken["ok"])
        self.assertLess(len(taken_text), 400)
        busy = json.loads(bodies[4]["result"]["content"][0]["text"])
        self.assertEqual(busy["result"], "busy")
        self.assertFalse(busy["ok"])
        listed = json.loads(bodies[5]["result"]["content"][0]["text"])
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["claims"][0]["owner"], "agent-a")
        rules = json.loads(bodies[6]["result"]["content"][0]["text"])
        self.assertIn("stale", rules["stale"].lower())
        self.assertEqual(bodies[7]["error"]["code"], -32602)


class DemoAndPageTest(unittest.TestCase):
    def test_demo_blocks_then_takes_over(self) -> None:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env.pop("WORK_CLAIM_LEDGER", None)
        ledger = ROOT / ".agent-toolkit" / "demo-claims.json"
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "demo_work_claim.py")],
                cwd=self._elsewhere(),
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            text = proc.stdout
            self.assertIn("agent-toolkit · week 3 · work-claim ledger", text)
            self.assertLess(text.index("busy"), text.index("taken-over"))
            self.assertIn("held by agent-a, 60s left", text)
            self.assertLess(text.index("stale"), text.index("takeable"))
            self.assertIn("expired", text)
            self.assertIn("takeable", text)
            self.assertIn("was agent-a", text)
            self.assertIn("not-owner", text)
            self.assertIn("held by agent-b", text)
            self.assertIn("agent-b was blocked, then took over the expired claim.", text)
            data = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(data["claims"]["fix-flaky-test"]["status"], "done")
            self.assertEqual(data["claims"]["fix-flaky-test"]["owner"], "agent-b")

    def _elsewhere(self) -> str:
        path = Path(tempfile.mkdtemp(prefix="work-claim-cwd-"))
        self.addCleanup(lambda: os.rmdir(path) if path.exists() and not any(path.iterdir()) else None)
        return str(path)

    def test_sources_do_not_phone_home(self) -> None:
        for name in ("work_claim.py", "demo_work_claim.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("import budget_guard", text, name)
            self.assertNotIn("import handoff_checker", text, name)
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)
            self.assertNotIn("urllib", text, name)
            self.assertNotIn("socket", text, name)

    def test_readme_and_page_list_week_3(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Week 3 — Work-claim ledger", readme)
        self.assertIn("work_claim.py", readme)
        self.assertIn("demo_work_claim.py", readme)
        self.assertIn("`busy`", readme)
        self.assertIn("`taken-over`", readme)
        page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("work_claim.py", page)
        self.assertIn("demo_work_claim.py", page)
        self.assertIn("Work-claim ledger", page)
        self.assertGreaterEqual(page.count(">Live<"), 4)
        self.assertEqual(page.count("Coming soon"), 0)
        self.assertEqual(page.count("<script>"), 1)
        self.assertEqual(page.count("stats.srv1536099.hstgr.cloud/hit"), 1)
        self.assertIn("navigator.doNotTrack", page)


if __name__ == "__main__":
    unittest.main()
