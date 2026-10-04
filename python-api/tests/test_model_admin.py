"""Tests for app/language/model_admin.py (Phase 6 P6-2a). Structural +
argparse only -- the DB dispatch itself needs a real DB (pragma: no cover
in the source), so we test that the CLI surface is complete and that it
calls through to the SAME functions the other modules already test.
    docker compose exec python-api python -m unittest tests.test_model_admin -v
"""
import unittest

from tests import _sa_stub  # noqa: F401
from app.language import model_admin as ma
from app.language import model_registry as mr


class TestParser(unittest.TestCase):
    def test_subcommands_exist(self):
        ap = ma.build_parser()
        for args in (
            ["list", "--tenant", "1"],
            ["shadow-report", "--tenant", "1", "--version", "v1"],
            ["promote", "--tenant", "1", "--version", "v1", "--to", "shadow", "--reason", "r"],
            ["start-canary", "--tenant", "1", "--version", "v1", "--reason", "r"],
            ["retire", "--tenant", "1", "--version", "v1", "--reason", "r"],
        ):
            ns = ap.parse_args(args)
            self.assertTrue(ns.cmd)

    def test_promote_only_accepts_real_statuses(self):
        ap = ma.build_parser()
        with self.assertRaises(SystemExit):
            ap.parse_args(["promote", "--tenant", "1", "--version", "v1", "--to", "bogus", "--reason", "r"])

    def test_promote_requires_reason(self):
        ap = ma.build_parser()
        with self.assertRaises(SystemExit):
            ap.parse_args(["promote", "--tenant", "1", "--version", "v1", "--to", "shadow"])

    def test_defaults_capability_intent(self):
        ns = ma.build_parser().parse_args(["list", "--tenant", "1"])
        self.assertEqual(ns.capability, "intent")

    def test_no_reimplemented_lifecycle_logic(self):
        src = __import__("pathlib").Path(ma.__file__).read_text()
        self.assertNotIn("ALLOWED_TRANSITIONS", src)   # must come from model_registry, not be copied
        self.assertIn("model_registry.set_status" if False else "mr.set_status", src)


if __name__ == "__main__":
    unittest.main()
