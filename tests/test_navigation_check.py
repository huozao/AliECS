from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_navigation.py"


class NavigationCheckTest(unittest.TestCase):
    def run_check(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root)],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_red_green_for_path_and_python_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs" / "index.md").write_text("[module](../module.py)\n", encoding="utf-8")
            (root / "module.py").write_text("def present():\n    return True\n", encoding="utf-8")
            config = {
                "documents": ["docs/index.md"],
                "paths": ["module.py"],
                "python_symbols": [{"file": "module.py", "symbol": "present"}],
            }
            (root / ".navigation-check.json").write_text(json.dumps(config), encoding="utf-8")

            self.assertEqual(self.run_check(root).returncode, 0)
            (root / "docs" / "index.md").write_text("[missing](../missing.py)\n", encoding="utf-8")
            broken_path = self.run_check(root)
            self.assertEqual(broken_path.returncode, 1)
            self.assertIn("broken link", broken_path.stdout)

            (root / "docs" / "index.md").write_text("[module](../module.py)\n", encoding="utf-8")
            config["python_symbols"][0]["symbol"] = "missing"
            (root / ".navigation-check.json").write_text(json.dumps(config), encoding="utf-8")
            broken_symbol = self.run_check(root)
            self.assertEqual(broken_symbol.returncode, 1)
            self.assertIn("missing Python symbol", broken_symbol.stdout)


if __name__ == "__main__":
    unittest.main()

