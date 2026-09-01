"""check_doc_freshness 的红绿测试。

用临时 git 仓构造场景，不依赖本仓真实历史——否则测试会随仓库演进而漂。
用 TestCase 子类而非裸函数：CI 的 unittest discover 收不到裸函数式用例
（2026-08-31 实测漏跑 355 个）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_doc_freshness.py"
NAV_DOC = "docs/project-ai-map.md"


class DocFreshnessGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.write(NAV_DOC, "# nav\n")
        self.write("services/backend-api/app/routers/existing.py", "x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base", date="2026-01-01T00:00:00+08:00")
        self.base = self.git("rev-parse", "HEAD")
        self.addCleanup(self._tmp.cleanup)

    def git(self, *args: str, date: str | None = None) -> str:
        env = dict(os.environ)
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True, encoding="utf-8", env=env
        ).strip()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_gate(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root, capture_output=True, text=True,
        )

    def commit(self, message: str, date: str | None = None) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message, date=date)
        return self.git("rev-parse", "HEAD")

    def test_new_router_without_nav_update_fails(self) -> None:
        self.write("services/backend-api/app/routers/added.py", "y = 1\n")
        head = self.commit("add router")
        result = self.run_gate("--range", self.base, head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("A services/backend-api/app/routers/added.py", result.stdout)

    def test_new_router_with_nav_update_passes(self) -> None:
        self.write("services/backend-api/app/routers/added.py", "y = 1\n")
        self.write(NAV_DOC, "# nav\n\n## added\n")
        head = self.commit("add router + nav")
        result = self.run_gate("--range", self.base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_migration_without_nav_update_fails(self) -> None:
        self.write("db/migrations/0001_x.sql", "SELECT 1;\n")
        head = self.commit("add migration")
        self.assertEqual(self.run_gate("--range", self.base, head).returncode, 1)

    def test_deleted_router_without_nav_update_fails(self) -> None:
        (self.root / "services/backend-api/app/routers/existing.py").unlink()
        head = self.commit("drop router")
        result = self.run_gate("--range", self.base, head)
        self.assertEqual(result.returncode, 1)
        self.assertIn("D services/backend-api/app/routers/existing.py", result.stdout)

    def test_content_only_change_does_not_fire(self) -> None:
        """改内容不该要求动导航——否则常态红灯，等于没装。"""
        self.write("services/backend-api/app/routers/existing.py", "x = 2\n")
        head = self.commit("edit router body")
        result = self.run_gate("--range", self.base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("硬门不适用", result.stdout)

    def test_staleness_warns_but_does_not_fail(self) -> None:
        """软告警只 WARN：它抓的是内容腐烂，判据没有硬门精确。"""
        self.write("services/backend-api/app/routers/existing.py", "x = 3\n")
        self.commit("touch code", date="2026-03-01T00:00:00+08:00")  # 比 nav 晚 60 天
        result = self.run_gate("--staleness", "--stale-days", "14")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::warning::", result.stdout)

    def test_staleness_within_threshold_is_quiet(self) -> None:
        """阈值内必须不告警——恒告警和判据为真在输出上长得一样。"""
        self.write("services/backend-api/app/routers/existing.py", "x = 4\n")
        self.commit("touch code", date="2026-01-05T00:00:00+08:00")  # 比 nav 晚 4 天
        result = self.run_gate("--staleness", "--stale-days", "14")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("::warning::", result.stdout)


if __name__ == "__main__":
    unittest.main()
