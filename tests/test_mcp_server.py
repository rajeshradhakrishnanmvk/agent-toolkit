"""Stdio tests for the unified MCP server.

These cover the handoff tools, which used to call module attributes
``handoff_checker.check`` and ``handoff_checker.template`` that do not exist.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_READY = {
    "goal": "Publish the week 2 row on the project page.",
    "owner": "writer-agent",
    "budget_left": "4000 tokens",
    "constraints": ["Leave the visit counter script unchanged."],
    "acceptance_criteria": ["The week 2 row shows a Live badge."],
    "questions": [],
    "artifacts": [],
}

_READY_WITH_NOTE = """\
---
goal: Publish the week 2 row on the project page.
owner: writer-agent
budget_left: 4000 tokens
constraints:
  - Leave the visit counter script unchanged.
acceptance_criteria:
  - The week 2 row shows a Live badge.
artifacts:
  - note.txt
---

Ship the row.
"""

_FILE_HANDOFF = """\
---
goal: This file must not be read when its name is passed as payload text.
owner: writer-agent
budget_left: 4000 tokens
constraints:
  - Leave the visit counter script unchanged.
acceptance_criteria:
  - The week 2 row shows a Live badge.
---

On disk only.
"""


def _request(req_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def _tool(req_id: int, name: str, arguments: dict | None = None) -> dict:
    return _request(
        req_id,
        "tools/call",
        {"name": name, "arguments": {} if arguments is None else arguments},
    )


class UnifiedServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "root"
        self.decoy = base / "decoy"
        self.root.mkdir()
        self.decoy.mkdir()
        (self.root / "note.txt").write_text("ok\n", encoding="utf-8")
        (self.root / "ready.md").write_text(_FILE_HANDOFF, encoding="utf-8")
        (self.decoy / "ready.md").write_text(_FILE_HANDOFF, encoding="utf-8")
        (self.root / "extra.schema.json").write_text(
            json.dumps(
                {
                    "required": ["ticket"],
                    "properties": {"ticket": {"type": "string", "minLength": 3}},
                }
            ),
            encoding="utf-8",
        )
        # A different schema in the process cwd. handoff-check must ignore it.
        (self.decoy / "extra.schema.json").write_text(
            json.dumps({"required": ["cwd_only_field"]}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handoff_and_partial_record_over_stdio(self) -> None:
        messages = [
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            ),
            _tool(2, "handoff-template"),
            _tool(3, "handoff-check", {"payload": "---\ngoal: g\n---\n"}),
            _tool(4, "handoff-check", {"payload": json.dumps(_READY)}),
            _tool(5, "handoff-check", {"payload": "ready.md"}),
            _tool(6, "handoff-check", {"payload": _READY_WITH_NOTE}),
            _tool(
                7,
                "handoff-check",
                {"payload": json.dumps(_READY), "schema_path": "extra.schema.json"},
            ),
            _tool(8, "budget-guard-open", {"task": "job", "max_tokens": 100}),
            _tool(9, "budget-guard-record", {"task": "job"}),
            _tool(10, "budget-guard-record", {"task": "job", "tokens": 10, "tool_calls": 1}),
        ]
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "mcp_server.py"),
                "--root",
                str(self.root),
                "--budget-ledger",
                str(self.root / "budget.json"),
                "--work-ledger",
                str(self.root / "work.json"),
            ],
            input=payload,
            text=True,
            capture_output=True,
            cwd=self.decoy,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), len(messages), proc.stdout)
        bodies = [json.loads(line) for line in lines]
        self.assertEqual([body["id"] for body in bodies], list(range(1, len(messages) + 1)))

        self.assertEqual(bodies[0]["result"]["serverInfo"]["name"], "agent-toolkit")

        template = self._ok_text(bodies[1])
        self.assertIsInstance(template, str)
        self.assertIn("goal:", template)
        self.assertIn("acceptance_criteria:", template)
        self.assertNotIn("has no attribute", bodies[1]["result"]["content"][0]["text"])

        partial = self._ok_text(bodies[2])
        self.assertEqual(partial["status"], "needs-fixes")
        self.assertFalse(partial["ok_to_hand_off"])
        self.assertNotIn("has no attribute", json.dumps(partial))

        ready = self._ok_text(bodies[3])
        self.assertEqual(ready["status"], "ready", ready["problems"])
        self.assertTrue(ready["ok_to_hand_off"])
        self.assertEqual(ready["format"], "json")

        missed = bodies[4]["result"]
        self.assertTrue(missed["isError"])
        error_text = missed["content"][0]["text"]
        self.assertIn("JSON object or Markdown", error_text)
        self.assertNotIn("ready to hand off", error_text)
        self.assertNotIn("no handoff file", error_text)
        self.assertNotIn("On disk only", error_text)

        cited = self._ok_text(bodies[5])
        self.assertEqual(cited["status"], "ready", cited["problems"])
        self.assertEqual(cited["paths"]["found"], ["note.txt"])
        self.assertEqual(Path(cited["root"]).resolve(), self.root.resolve())

        schema = self._ok_text(bodies[6])
        self.assertEqual(schema["status"], "needs-fixes")
        fields = [problem["field"] for problem in schema["problems"]]
        self.assertIn("ticket", fields)
        self.assertNotIn("cwd_only_field", fields)

        opened = self._ok_text(bodies[7])
        self.assertEqual(opened["task_id"], "job")
        self.assertEqual(opened["decision"], "continue")

        recorded = self._ok_text(bodies[8])
        self.assertEqual(recorded["spent"]["tokens"], 0)
        self.assertEqual(recorded["spent"]["cost_usd"], 0.0)
        self.assertEqual(recorded["spent"]["tool_calls"], 0)

        partial_record = self._ok_text(bodies[9])
        self.assertEqual(partial_record["spent"]["tokens"], 10)
        self.assertEqual(partial_record["spent"]["cost_usd"], 0.0)
        self.assertEqual(partial_record["spent"]["tool_calls"], 1)

    def _ok_text(self, body: dict) -> object:
        self.assertNotIn("error", body)
        result = body["result"]
        self.assertFalse(result.get("isError", False), result)
        return json.loads(result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
