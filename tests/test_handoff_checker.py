"""Tests for the week-2 handoff checker, its CLI/MCP surface, and the demo."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo_handoff import FIXED, SLOPPY, SLOPPY_CODES  # noqa: E402
from handoff_checker import (  # noqa: E402
    BLOCKED,
    NEEDS_FIXES,
    READY,
    HandoffChecker,
    HandoffError,
    handle_message,
    main,
    parse_handoff,
    render_template,
)


def _valid(**overrides: object) -> dict:
    payload = {
        "goal": "Publish the week 2 row on the project page.",
        "owner": "writer-agent",
        "budget_left": "4000 tokens",
        "constraints": ["Leave the visit counter script unchanged."],
        "acceptance_criteria": ["The week 2 row shows a Live badge."],
        "questions": [],
        "artifacts": [],
    }
    payload.update(overrides)
    return payload


class CheckerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.checker = HandoffChecker(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ready_handoff_is_quiet(self) -> None:
        decision = self.checker.check(_valid())
        self.assertEqual(decision["status"], READY)
        self.assertTrue(decision["ok_to_hand_off"])
        self.assertEqual(decision["problems"], [])
        self.assertEqual(decision["summary"], "ready to hand off")
        self.assertGreater(decision["bytes"], 0)
        self.assertEqual(decision["max_bytes"], 16 * 1024)

    def test_missing_and_empty_fields_name_the_fix(self) -> None:
        decision = self.checker.check(
            {
                "goal": "   ",
                "acceptance_criteria": [],
                "constraints": ["Leave the visit counter script unchanged."],
                "budget_left": "4000 tokens",
            }
        )
        self.assertEqual(decision["status"], NEEDS_FIXES)
        self.assertFalse(decision["ok_to_hand_off"])
        by_field = {problem["field"]: problem for problem in decision["problems"]}
        self.assertEqual(by_field["goal"]["code"], "missing-field")
        self.assertIn("is empty", by_field["goal"]["message"])
        self.assertIn("is empty", by_field["acceptance_criteria"]["message"])
        self.assertIn("is missing", by_field["owner"]["message"])
        self.assertIn("next step", by_field["owner"]["message"])
        self.assertNotIn("constraints", by_field)
        self.assertNotIn("budget_left", by_field)

    def test_owner_sentinels_and_aliases(self) -> None:
        for owner in ("none", "TBD", "unassigned"):
            decision = self.checker.check(_valid(owner=owner))
            self.assertEqual(decision["problems"][0]["field"], "owner", owner)
        aliased = _valid()
        del aliased["owner"]
        del aliased["budget_left"]
        aliased["assignee"] = "writer-agent"
        aliased["remaining_budget"] = "1000 tokens"
        aliased["done_when"] = aliased.pop("acceptance_criteria")
        decision = self.checker.check(aliased)
        self.assertEqual(decision["status"], READY, decision["problems"])

    def test_vague_criteria_flag_the_word_and_one_hit_per_item(self) -> None:
        decision = self.checker.check(
            _valid(
                acceptance_criteria=[
                    "update the page properly",
                    "add week 2, etc",
                    "fill the gaps as needed",
                    "tests pass correctly and properly",
                ]
            )
        )
        vague = [problem for problem in decision["problems"] if problem["code"] == "vague-criterion"]
        self.assertEqual(len(vague), 4)
        self.assertIn('"properly"', vague[0]["message"])
        self.assertIn("pass or fail", vague[0]["message"])
        self.assertIn('"etc"', vague[1]["message"])
        self.assertIn("list each item", vague[1]["message"].lower())
        self.assertIn('"as needed"', vague[2]["message"])
        self.assertIn('"correctly"', vague[3]["message"])
        self.assertNotIn('"properly"', vague[3]["message"])
        clean = self.checker.check(
            _valid(acceptance_criteria=["Incorrectly spelled names are rejected."])
        )
        self.assertEqual(clean["status"], READY, clean["problems"])

    def test_todo_tbd_and_fixme_are_named(self) -> None:
        decision = self.checker.check(
            _valid(notes="TODO: check the counter", context="Leave the TBD in the spec. FIXME later.")
        )
        labels = [problem["message"].split(" ", 1)[0] for problem in decision["problems"]]
        self.assertEqual(decision["status"], NEEDS_FIXES)
        self.assertEqual(labels, ["TODO", "TBD"])
        self.assertIn("Finish it or delete it", decision["problems"][0]["message"])
        self.assertIn("TODO: check the counter", decision["problems"][0]["message"])

    def test_questions_block_until_answered(self) -> None:
        open_q = self.checker.check(_valid(questions=["Which tone should the page use?"]))
        self.assertEqual(open_q["problems"][0]["code"], "unresolved-question")
        self.assertIn("Which tone should the page use?", open_q["problems"][0]["message"])
        answered = self.checker.check(
            _valid(
                questions=[
                    {
                        "question": "Which tone should the page use?",
                        "answer": "Match week 1. Short sentences.",
                    }
                ],
                notes="Which tone should the page use?",
            )
        )
        self.assertEqual(answered["status"], READY, answered["problems"])
        resolved = self.checker.check(
            _valid(questions=[{"question": "Which tone?", "resolved": True}])
        )
        self.assertEqual(resolved["status"], READY, resolved["problems"])
        junk = self.checker.check(
            _valid(questions=[{"question": "Which tone?", "answer": "TBD"}])
        )
        codes = [problem["code"] for problem in junk["problems"]]
        self.assertIn("unresolved-question", codes)
        self.assertIn("todo-left", codes)
        body = self.checker.check(_valid(body="Which font should the title use?"))
        self.assertEqual(body["problems"][0]["code"], "unresolved-question")
        link = self.checker.check(_valid(notes="See https://example.com/a?b=1 for the mock."))
        self.assertEqual(link["status"], READY, link["problems"])

    def test_missing_paths_and_urls(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "index.html").write_text("ok\n", encoding="utf-8")
        missing = self.checker.check(
            _valid(
                artifacts=["docs/no-such-page.md"],
                acceptance_criteria=["docs/index.html shows a Live badge."],
            )
        )
        self.assertEqual(missing["paths"]["missing"], ["docs/no-such-page.md"])
        self.assertEqual(missing["paths"]["found"], ["docs/index.html"])
        self.assertIn("does not exist", missing["problems"][0]["message"])
        self.assertEqual(missing["status"], NEEDS_FIXES)
        ready = self.checker.check(
            _valid(
                artifacts=["docs/index.html", "./docs/index.html"],
                notes="Also see https://example.com/files/guide.md before editing.",
            )
        )
        self.assertEqual(ready["status"], READY, ready["problems"])
        self.assertEqual(ready["paths"]["found"], ["docs/index.html"])
        prose = self.checker.check(_valid(artifacts=["the design"]))
        self.assertEqual(prose["problems"][0]["code"], "not-a-path")

    def test_size_cap_and_exhausted_budget_block(self) -> None:
        tiny = HandoffChecker(root=self.root, max_bytes=32)
        decision = tiny.check(_valid())
        self.assertEqual(decision["status"], BLOCKED)
        self.assertEqual(decision["problems"][-1]["code"], "too-large")
        self.assertIn("cap is 32", decision["problems"][-1]["message"])
        self.assertFalse(decision["ok_to_hand_off"])
        for budget in (0, "0 tokens", "$0.00", "exhausted", {"tokens": 0, "cost_usd": 0}):
            blocked = self.checker.check(_valid(budget_left=budget))
            self.assertEqual(blocked["status"], BLOCKED, budget)
            self.assertEqual(blocked["problems"][0]["code"], "budget-exhausted")
        mixed = self.checker.check(
            _valid(budget_left=0, acceptance_criteria=["update the page properly"])
        )
        self.assertEqual(mixed["status"], BLOCKED)
        self.assertEqual(
            [problem["code"] for problem in mixed["problems"]],
            ["vague-criterion", "budget-exhausted"],
        )
        partial = self.checker.check(_valid(budget_left={"tokens": 0, "cost_usd": 0.05}))
        self.assertEqual(partial["status"], READY, partial["problems"])
        with self.assertRaises(HandoffError):
            HandoffChecker(root=self.root, max_bytes=0)

    def test_schema_file_adds_required_fields(self) -> None:
        schema = {
            "required": ["ticket", "goal"],
            "properties": {
                "ticket": {"type": "string", "minLength": 3},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "additionalProperties": False,
        }
        path = self.root / "handoff.schema.json"
        path.write_text(json.dumps(schema), encoding="utf-8")
        missing = self.checker.check(_valid(), schema=path)
        self.assertEqual(missing["problems"][0]["field"], "ticket")
        self.assertIn("schema requires it", missing["problems"][0]["message"])
        self.assertEqual(missing["schema"], str(path))
        bad = self.checker.check(
            _valid(ticket="AT", priority="urgent", extra=True),
            schema=schema,
        )
        codes = [(problem["field"], problem["message"]) for problem in bad["problems"]]
        self.assertTrue(any("at least 3" in message for _field, message in codes))
        self.assertTrue(any("low, normal, high" in message for _field, message in codes))
        self.assertTrue(any(field == "extra" for field, _message in codes))
        good = self.checker.check(_valid(ticket="AT-12", priority="high"), schema=schema)
        self.assertEqual(good["status"], READY, good["problems"])
        broken = self.root / "bad.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaises(HandoffError):
            self.checker.check(_valid(), schema=broken)
        with self.assertRaises(HandoffError):
            self.checker.check(_valid(), schema={"anyOf": []})

    def test_markdown_and_json_round_trip(self) -> None:
        parsed = parse_handoff(SLOPPY)
        self.assertEqual(parsed["fields"]["goal"], "Update the project page for week 2")
        self.assertEqual(parsed["fields"]["owner"], "")
        self.assertEqual(
            parsed["fields"]["questions"],
            ["Which tone should the page use?"],
        )
        self.assertIn("TODO:", parsed["body"])
        document = """\
---
goal: Publish the week 2 row.
owner: writer-agent
budget_left:
  tokens: 4000
  cost_usd: 0.05
constraints:
  - "note: leave the counter alone"
acceptance_criteria:
  - The week 2 row shows a Live badge.
questions:
  - question: Which tone?
    answer: Match week 1.
---

Ship it.
"""
        parsed = parse_handoff(document)
        self.assertEqual(parsed["fields"]["budget_left"], {"tokens": 4000, "cost_usd": 0.05})
        self.assertEqual(
            parsed["fields"]["constraints"],
            ["note: leave the counter alone"],
        )
        self.assertEqual(
            parsed["fields"]["questions"],
            [{"question": "Which tone?", "answer": "Match week 1."}],
        )
        decision = self.checker.check(document)
        self.assertEqual(decision["status"], READY, decision["problems"])
        self.assertEqual(decision["format"], "markdown")
        blob = json.dumps(_valid())
        self.assertEqual(self.checker.check(blob)["format"], "json")
        with self.assertRaises(HandoffError):
            parse_handoff("---\ngoal: hi\n")
        with self.assertRaises(HandoffError):
            parse_handoff("[1, 2]")
        with self.assertRaises(HandoffError):
            self.checker.check("missing-handoff.md")

    def test_template_is_blank_and_checkable(self) -> None:
        for fmt in ("markdown", "json"):
            text = render_template(fmt)
            decision = self.checker.check(text)
            self.assertEqual(decision["status"], NEEDS_FIXES)
            self.assertEqual(
                [problem["field"] for problem in decision["problems"]],
                ["goal", "acceptance_criteria", "owner", "constraints", "budget_left"],
            )
            self.assertTrue(all(problem["code"] == "missing-field" for problem in decision["problems"]))
        with self.assertRaises(HandoffError):
            render_template("yaml")

    def test_demo_payloads_match_the_recording(self) -> None:
        self.assertFalse((ROOT / "docs" / "no-such-page.md").exists())
        sloppy = HandoffChecker(root=ROOT).check(SLOPPY)
        self.assertEqual(
            tuple(problem["code"] for problem in sloppy["problems"]),
            SLOPPY_CODES,
        )
        self.assertEqual(sloppy["status"], NEEDS_FIXES)
        fixed = HandoffChecker(root=ROOT).check(FIXED)
        self.assertEqual(fixed["status"], READY, fixed["problems"])
        self.assertEqual(len(fixed["paths"]["found"]), 2)

    def test_check_does_not_write(self) -> None:
        before = sorted(self.root.iterdir())
        self.checker.check(_valid(artifacts=["docs/missing.md"]))
        self.assertEqual(sorted(self.root.iterdir()), before)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_check_template_and_explain(self) -> None:
        code, out, err = self.run_cli("template", "--format", "json", "--output", str(self.root / "blank.json"))
        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / "blank.json").is_file())
        self.assertIn('"goal"', out)
        (self.root / "note.txt").write_text("x\n", encoding="utf-8")
        ready = """\
---
goal: Publish the week 2 row on the project page.
owner: writer-agent
budget_left: 4000 tokens
constraints:
  - Leave the visit counter script unchanged.
acceptance_criteria:
  - The week 2 row shows a Live badge.
questions: []
artifacts:
  - note.txt
---

Ship the row.
"""
        path = self.write("ok.md", ready)
        code, out, err = self.run_cli("check", str(path), "--root", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertIn("ready", out)
        self.assertIn("none", out)
        code, out, err = self.run_cli("check", str(path), "--root", str(self.root), "--json")
        self.assertEqual(json.loads(out)["status"], READY)
        sloppy = self.write("sloppy.md", SLOPPY)
        code, out, err = self.run_cli("check", str(sloppy), "--root", str(ROOT))
        self.assertEqual(code, 3, err)
        self.assertIn("needs-fixes", out)
        self.assertIn("properly", out)
        self.assertIn("1. ", out)
        blocked = self.write(
            "zero.md",
            ready.replace("4000 tokens", "0"),
        )
        code, out, err = self.run_cli("check", str(blocked), "--root", str(self.root))
        self.assertEqual(code, 4, err)
        self.assertIn("blocked", out)
        code, out, err = self.run_cli("explain")
        self.assertEqual(code, 0, err)
        self.assertIn("as needed", out)
        self.assertIn("properly", out)
        self.assertIn("needs-fixes", out)
        code, out, err = self.run_cli("explain", "--json")
        rules = json.loads(out)
        self.assertIn("etc", rules["vague_terms"])
        self.assertEqual(rules["exit_codes"]["blocked"], 4)
        code, _out, err = self.run_cli("check", str(self.root / "nope.md"))
        self.assertEqual(code, 1)
        self.assertIn("no handoff file", err)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_stdin_and_schema_flag(self) -> None:
        schema = self.root / "schema.json"
        schema.write_text(json.dumps({"required": ["ticket"]}), encoding="utf-8")
        text = """\
---
goal: Publish the week 2 row on the project page.
owner: writer-agent
budget_left: 4000 tokens
constraints:
  - Leave the visit counter script unchanged.
acceptance_criteria:
  - The week 2 row shows a Live badge.
questions: []
---

Ship the row.
"""
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(text)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["check", "-", "--root", str(self.root), "--schema", str(schema)])
        self.assertEqual(code, 3, err.getvalue())
        self.assertIn("ticket", out.getvalue())


class McpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handle_message_protocol_and_errors(self) -> None:
        init = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        assert init is not None
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "handoff-checker")
        fallback = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        )
        assert fallback is not None
        self.assertEqual(fallback["result"]["protocolVersion"], "2024-11-05")
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        missing = handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "handoff_check", "arguments": {}},
            }
        )
        assert missing is not None
        self.assertTrue(missing["result"]["isError"])
        unknown = handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        assert unknown is not None
        self.assertEqual(unknown["error"]["code"], -32601)
        tools = handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        assert tools is not None
        names = [tool["name"] for tool in tools["result"]["tools"]]
        self.assertEqual(names, ["handoff_check", "handoff_template", "handoff_explain"])

    def test_stdio_session(self) -> None:
        note = self.root / "note.txt"
        note.write_text("ok\n", encoding="utf-8")
        handoff = self.root / "handoff.md"
        handoff.write_text(
            """\
---
goal: Publish the week 2 row on the project page.
owner: writer-agent
budget_left: 4000 tokens
constraints:
  - Leave the visit counter script unchanged.
acceptance_criteria:
  - The week 2 row shows a Live badge.
questions: []
artifacts:
  - note.txt
---

Ship the row.
""",
            encoding="utf-8",
        )
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
                "params": {"name": "handoff_template", "arguments": {"format": "json"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "handoff_check",
                    "arguments": {"path": str(handoff), "root": str(self.root)},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "handoff_check",
                    "arguments": {"handoff": SLOPPY, "root": str(ROOT)},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "handoff_explain", "arguments": {}},
            },
        ]
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "handoff_checker.py"), "serve", "--root", str(self.root)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 6)
        bodies = [json.loads(line) for line in lines]
        self.assertEqual([body["id"] for body in bodies], [1, 2, 3, 4, 5, 6])
        names = [tool["name"] for tool in bodies[1]["result"]["tools"]]
        self.assertEqual(names, ["handoff_check", "handoff_template", "handoff_explain"])
        template = bodies[2]["result"]["content"][0]["text"]
        self.assertIn('"acceptance_criteria"', template)
        ready = json.loads(bodies[3]["result"]["content"][0]["text"])
        self.assertEqual(ready["status"], READY)
        self.assertFalse(bodies[3]["result"]["isError"])
        sloppy = json.loads(bodies[4]["result"]["content"][0]["text"])
        self.assertEqual(sloppy["status"], NEEDS_FIXES)
        self.assertEqual(
            tuple(problem["code"] for problem in sloppy["problems"]),
            SLOPPY_CODES,
        )
        rules = json.loads(bodies[5]["result"]["content"][0]["text"])
        self.assertIn("as needed", rules["vague_terms"])


class DemoAndSourceTest(unittest.TestCase):
    def test_demo_shows_the_punch_list_then_ready(self) -> None:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "demo_handoff.py")],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            text = proc.stdout
            self.assertIn("agent-toolkit · week 2 · handoff checker", text)
            self.assertLess(
                text.index("needs-fixes"),
                text.index("ready"),
            )
            for line in (
                "owner is empty. Name who owns the next step.",
                "budget_left is empty. Say what is left to spend (tokens, dollars, or tool calls).",
                'Acceptance criterion "update the page properly" contains "properly".',
                'Acceptance criterion "add week 2, etc" contains "etc".',
                "docs/no-such-page.md does not exist. Create it, or remove the citation.",
                'Unresolved question: "Which tone should the page use?" Answer it, or remove it.',
                'TODO left in: "TODO: check the visitor counter before publishing." Finish it or delete it.',
                "2 cited paths exist",
                "the sloppy handoff was not sent. The repaired one is ready.",
            ):
                self.assertIn(line, text)

    def test_sources_do_not_phone_home(self) -> None:
        for name in ("handoff_checker.py", "demo_handoff.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)
            self.assertNotIn("urllib", text, name)
            self.assertNotIn("socket", text, name)

    def test_readme_and_page_list_week_2(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Week 2 — Handoff checker", readme)
        self.assertIn("handoff_checker.py", readme)
        self.assertIn("demo_handoff.py", readme)
        self.assertIn("`ready`", readme)
        page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("handoff_checker.py", page)
        self.assertIn("demo_handoff.py", page)
        self.assertGreaterEqual(page.count(">Live<"), 2)
        self.assertIn("needs-fixes", page)


if __name__ == "__main__":
    unittest.main()
