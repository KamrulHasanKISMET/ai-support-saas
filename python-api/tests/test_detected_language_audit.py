"""
Tests for app/language/detected_language_audit.py (read-only inventory).
Skips repo-wide assertions when only python-api is mounted (as in a
container that does not include db/ and node-api/).

Run:
    docker compose exec python-api python -m unittest tests.test_detected_language_audit -v
"""

import tempfile
import unittest
from pathlib import Path

from app.language import detected_language_audit as a

HAVE_REPO = (a.REPO / "db" / "init").exists() and (a.REPO / "python-api" / "app").exists()


class TestScanSynthetic(unittest.TestCase):
    def _repo(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        for rel, txt in files.items():
            p = d / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(txt, encoding="utf-8")
        return d

    def test_code_vs_comment_vs_layer(self):
        d = self._repo({
            "python-api/app/x.py": "# detectedLanguage note\nx = r.detected_language\n",
            "db/init/001.sql": "-- detected_language history\nALTER TABLE t ADD detected_language TEXT;\n",
            "node-api/src/a.ts": "const l = res.detectedLanguage;\n",
            "docs/A.md": "mentions detectedLanguage\n",
        })
        hits = a.scan(d)
        s = a.summarize(hits)
        self.assertEqual(s["python-app"], {"python-api/app/x.py": 1})
        self.assertEqual(s["db-migration"], {"db/init/001.sql": 1})
        self.assertEqual(s["node-api"], {"node-api/src/a.ts": 1})
        self.assertNotIn("doc", s)
        self.assertIn("CHECK BEFORE REMOVING", a.render(hits))

    def test_no_node_readers_message(self):
        d = self._repo({"python-api/app/x.py": "y = detected_language\n"})
        self.assertIn("(none found)", a.render(a.scan(d)))

    def test_missing_repo_is_graceful(self):
        self.assertEqual(a.scan(Path("/nonexistent")), [])
        self.assertIn("no references found", a.render([]))

    def test_skips_node_modules(self):
        d = self._repo({"node-api/node_modules/z/i.js": "detectedLanguage\n"})
        self.assertEqual(a.scan(d), [])


@unittest.skipUnless(HAVE_REPO, "full repo not mounted")
class TestRealRepo(unittest.TestCase):
    def test_known_consumers_are_found(self):
        files = {f for fs in a.summarize(a.scan()).values() for f in fs}
        for expected in ("python-api/app/agent/core_agent.py", "python-api/app/kernel/kernel.py",
                         "python-api/app/trace/trace_service.py", "python-api/app/language/verification_service.py",
                         "python-api/app/language/experience_service.py", "db/init/005_agent_foundation.sql",
                         "db/init/010_language_experience.sql"):
            self.assertIn(expected, files)

    def test_no_node_api_reader_today(self):
        self.assertEqual([h for h in a.scan() if h["layer"] == "node-api"], [])

    def test_pending_work_doc_lists_every_code_file(self):
        doc = (a.REPO / "docs" / "PENDING_WORK.md").read_text(encoding="utf-8")
        s = a.summarize(a.scan())
        for layer in ("python-app", "db-migration", "node-api"):
            for f in s.get(layer, {}):
                self.assertIn(Path(f).name, doc, f"{f} missing from PENDING_WORK.md A2")

    def test_audit_tools_excluded_from_their_own_scan(self):
        names = {Path(h["file"]).name for h in a.scan()}
        self.assertFalse(names & {"detected_language_audit.py", "language_tag_readiness.py"})


if __name__ == "__main__":
    unittest.main()
