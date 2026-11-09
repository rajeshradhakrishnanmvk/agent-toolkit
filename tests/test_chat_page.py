"""The static chat page under docs/chat/."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CHAT = DOCS / "chat"


class ChatPageTest(unittest.TestCase):
    def test_page_exists_and_homepage_links_to_it(self) -> None:
        html = (CHAT / "index.html").read_text(encoding="utf-8")
        css = (CHAT / "chat.css").read_text(encoding="utf-8")
        script = (CHAT / "chat.js").read_text(encoding="utf-8")
        memory = (CHAT / "memory.js").read_text(encoding="utf-8")
        home = (DOCS / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="chat/"', home)
        self.assertEqual(home.count("<script>"), 1)
        self.assertEqual(home.count("stats.srv1536099.hstgr.cloud/hit"), 1)
        self.assertIn('src="memory.js"', html)
        self.assertIn('src="profiles.js"', html)
        self.assertIn('src="airport.js"', html)
        self.assertIn('src="chat.js"', html)
        self.assertIn('href="chat.css"', html)
        self.assertNotIn("src=\"http", html.lower())
        self.assertNotIn("href=\"http", html.lower())
        self.assertIn('name="viewport"', html)
        global_profile = json.loads((CHAT / "profiles" / "global.json").read_text(encoding="utf-8"))
        self.assertEqual(global_profile["config"]["baseUrl"], "https://chat.srv1536099.hstgr.cloud/v1")
        self.assertEqual(global_profile["config"]["model"], "Llama-3.2-3B-Instruct")
        self.assertIn("{reason}", global_profile["config"]["askHuman"]["template"])
        self.assertIn("localStorage", script)
        self.assertIn("sessionStorage", script)
        self.assertIn("indexedDB", memory)
        self.assertIn("SYNONYMS", memory)
        self.assertIn("keyword match, not a semantic search", memory)
        self.assertIn("What was recalled", script)
        self.assertNotIn("fonts.googleapis", html.lower())
        self.assertNotIn("fonts.googleapis", css.lower())
        secret = re.compile(r"sk-[A-Za-z0-9]{12,}")
        for name, text in (("html", html), ("css", css), ("js", script), ("memory", memory)):
            self.assertIsNone(secret.search(text), name)
            self.assertNotIn("OPENAI_API_KEY=", text, name)
            self.assertNotIn("Bearer sk-", text, name)
        self.assertIn("stays in this browser", html)
        self.assertIn("not a hosted service", html)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/chat/", readme)
        self.assertIn("https://rajeshradhakrishnanmvk.github.io/agent-toolkit/chat/", readme)
        self.assertIn("keyword-based", readme)
        self.assertIn("IndexedDB", readme)

    def test_chat_page_does_not_replace_the_homepage_counter(self) -> None:
        html = (CHAT / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("stats.srv1536099.hstgr.cloud/hit", html)
        self.assertEqual(html.lower().count("<script"), 4)

    def test_profiles_are_data_and_logic_is_not_a_persona(self) -> None:
        logic = ["chat.js", "memory.js", "profiles.js", "index.html", "chat.css"]
        for name in logic:
            text = (CHAT / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("financial", text, name)
        profile_root = CHAT / "profiles"
        files = [profile_root / "global.json", profile_root / "manifest.json"]
        files.extend((profile_root / "souls").glob("*.json"))
        files.extend((profile_root / "sessions").glob("*.json"))
        kinds = set()
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            if path.name == "manifest.json":
                self.assertIn("financial-consultant.json", data["souls"])
                continue
            self.assertIn(data["kind"], {"global", "soul", "session"})
            self.assertTrue(data["id"])
            self.assertIsInstance(data.get("config", {}), dict)
            kinds.add(data["kind"])
            blob = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{12,}", blob), path.name)
        self.assertEqual(kinds, {"global", "soul", "session"})
        doc = (CHAT / "PROFILES.md").read_text(encoding="utf-8")
        self.assertIn("global", doc)
        self.assertIn("soul", doc)
        self.assertIn("session", doc)


if __name__ == "__main__":
    unittest.main()
