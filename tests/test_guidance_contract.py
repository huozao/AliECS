"""Guard loading and discovery behavior, not document wording."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_navigation.py"


class GuidanceContractTest(unittest.TestCase):
    def check(self, root, config):
        (root / ".navigation-check.json").write_text(json.dumps(config))
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root)],
                              capture_output=True, text=True)

    def test_utf8_entrypoint_budget_catches_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("中" * 11)
            result = self.check(root, {"documents": ["AGENTS.md"],
                                      "entrypoint_max_bytes": {"AGENTS.md": 32}})
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("entrypoint too large", result.stdout)
            (root / "AGENTS.md").write_text("中" * 10)
            self.assertEqual(self.check(root, {"documents": ["AGENTS.md"],
                             "entrypoint_max_bytes": {"AGENTS.md": 32}}).returncode, 0)

    def test_new_current_document_is_checked_without_manual_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs/new.md").write_text("[missing](missing.py)")
            result = self.check(root, {"document_globs": ["docs/*.md"]})
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("broken link", result.stdout)
            (root / "docs/missing.py").write_text("pass")
            self.assertEqual(self.check(root, {"document_globs": ["docs/*.md"]}).returncode, 0)

    def test_empty_discovery_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.check(Path(tmp), {"document_globs": ["typo/*.md"]})
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("empty document glob", result.stdout)

    def test_missing_optional_repo_is_explicitly_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("[child](Child/AGENTS.md)")
            result = self.check(root, {"documents": ["AGENTS.md"],
                                      "optional_link_prefixes": ["Child"]})
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("UNVERIFIED", result.stdout)

    def test_nested_document_cannot_skip_missing_file_in_present_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "guidance").mkdir()
            (root / "Child").mkdir()
            (root / "guidance/routes.md").write_text("[missing](../Child/missing.md)")
            result = self.check(root, {"documents": ["guidance/routes.md"],
                                      "optional_link_prefixes": ["../Child"]})
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("broken link", result.stdout)


if __name__ == "__main__":
    unittest.main()
