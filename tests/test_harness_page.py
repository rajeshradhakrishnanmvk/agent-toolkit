"""The browser harness under docs/harness/ has no package dependencies, no extra URLs, and no keys."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "docs" / "harness"
ENDPOINT = "https://chat.srv1536099.hstgr.cloud/v1"
URL = re.compile(r"https?://[^\s\"'<>)\]]+")
SECRET = re.compile(r"sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}")
DEP_KEYS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _urls(text: str) -> list[str]:
    found = []
    for match in URL.findall(text):
        found.append(match.rstrip(".,;`)\"'"))
    return found


class HarnessPageTest(unittest.TestCase):
    def test_folder_layout(self) -> None:
        for name in (
            "index.html",
            "main.js",
            "harness.js",
            "harness.css",
            "test.html",
            "test.js",
            "test-page.js",
            "README.md",
            "package.json",
            "profiles/manifest.json",
            "profiles/global.json",
            "profiles/souls/general.json",
            "profiles/souls/planner.json",
            "profiles/souls/writer.json",
            "profiles/sessions/demo.json",
            "profiles/sessions/handoff.json",
            "handoff.js",
            "claims.js",
        ):
            self.assertTrue((HARNESS / name).is_file(), name)
        self.assertFalse((HARNESS / "cli.js").exists())
        self.assertFalse((ROOT / "harness" / "cli.js").exists())
        self.assertFalse((HARNESS / "package-lock.json").exists())
        self.assertFalse((HARNESS / "node_modules").exists())

    def test_package_json_has_no_dependencies(self) -> None:
        data = json.loads((HARNESS / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data.get("type"), "module")
        for key in DEP_KEYS:
            deps = data.get(key) or {}
            self.assertFalse(deps, key)

    def test_only_the_configured_endpoint_is_named(self) -> None:
        named = False
        for path in HARNESS.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for url in _urls(text):
                self.assertTrue(
                    url == ENDPOINT or url.startswith(ENDPOINT + "/"),
                    f"{path.relative_to(ROOT)} names {url}",
                )
                named = True
            self.assertIsNone(SECRET.search(text), path.name)
            self.assertNotIn("Bearer sk-", text, path.name)
            lowered = text.lower()
            self.assertNotIn("fonts.googleapis", lowered, path.name)
            self.assertNotIn("cdn.jsdelivr", lowered, path.name)
            self.assertNotRegex(lowered, r"dog[\s-]*food", path.name)
        self.assertTrue(named)
        html = (HARNESS / "index.html").read_text(encoding="utf-8")
        self.assertIn('type="module"', html)
        self.assertIn('src="main.js"', html)
        self.assertNotIn("src=\"http", html.lower())
        self.assertIn('name="viewport"', html)
        self.assertIn("stays in this tab", html)
        page = (HARNESS / "test.html").read_text(encoding="utf-8")
        self.assertIn('src="test-page.js"', page)
        index = (HARNESS / "index.html").read_text(encoding="utf-8")
        self.assertIn("Run two agents", index)
        self.assertIn('id="duo"', index)
        harness_readme = (HARNESS / "README.md").read_text(encoding="utf-8")
        self.assertIn("Planner", harness_readme)
        self.assertIn("Writer", harness_readme)
        self.assertIn("work_claim", harness_readme)
        self.assertIn("IndexedDB keyword memory", harness_readme)
        self.assertIn("virtual file workspace", harness_readme)
        self.assertIn("MCP-style bridge", harness_readme)
        self.assertIn("golden-prompt", harness_readme)
        self.assertIn("inbound channel", harness_readme)
        self.assertIn("scheduled runs", harness_readme)
        self.assertIn("multi-session router", harness_readme)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/harness/", readme)
        self.assertIn("https://rajeshradhakrishnanmvk.github.io/agent-toolkit/harness/", readme)

    def test_global_profile_points_at_the_endpoint(self) -> None:
        profile = json.loads((HARNESS / "profiles" / "global.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["config"]["baseUrl"], ENDPOINT)
        self.assertEqual(profile["config"]["model"], "Llama-3.2-3B-Instruct")
        self.assertEqual(profile["config"]["autonomy"], "supervised")
        self.assertEqual(profile["config"]["contextTokens"], 2048)
        self.assertEqual(profile["config"]["maxReplyTokens"], 256)


if __name__ == "__main__":
    unittest.main()
