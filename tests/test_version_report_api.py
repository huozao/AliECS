from __future__ import annotations
import json, os, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class FakeCur:
    def __init__(self, store): self.store = store; self._last = None
    def execute(self, sql, params=None):
        self._last = (sql, params)
        if sql.strip().upper().startswith("DELETE"): self.store["deleted"].append(params)
        elif "INSERT INTO version_reports" in sql: self.store["rows"].append(params)
    def fetchone(self): return [1]
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def __init__(self, store): self.store = store
    def cursor(self): return FakeCur(self.store)
    def commit(self): self.store["committed"] = True
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class VersionReportApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))
        os.environ["BACKUP_REPORT_TOKEN"] = "test-token"

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def _client(self, store):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import versions
        versions._conn = lambda: FakeConn(store)  # type: ignore
        app = FastAPI(); app.include_router(versions.router)
        return TestClient(app)

    def test_report_rejects_bad_token(self) -> None:
        store = {"rows": [], "deleted": []}
        client = self._client(store)
        r = client.post("/v1/internal/versions/report",
                        headers={"X-Backup-Report-Token": "wrong"},
                        json={"device": "aliecs", "containers": [], "apt": {}})
        self.assertEqual(r.status_code, 401)

    def test_report_writes_container_rows(self) -> None:
        store = {"rows": [], "deleted": []}
        client = self._client(store)
        r = client.post("/v1/internal/versions/report",
                        headers={"X-Backup-Report-Token": "test-token"},
                        json={"device": "aliecs",
                              "containers": [{"image": "postgres", "tag": "16.4", "digest": "sha256:x"}],
                              "apt": {"upgradable": 3, "security": 1},
                              "extra": {"openclaw": "2026.6.5"}})
        self.assertEqual(r.status_code, 200)
        # 实现写入 2 行：1 个容器行 + 1 个 apt-summary 行（两者都是 INSERT INTO version_reports）
        self.assertEqual(len(store["rows"]), 2)
        # params 顺序为 (device, image, tag, digest, extra_json)，image 在 index 1
        self.assertTrue(any(p[1] == "postgres" for p in store["rows"]))       # 容器行已写入
        self.assertTrue(any(p[1] == "apt-summary" for p in store["rows"]))    # apt 汇总行已写入
        self.assertTrue(store["deleted"])  # 先删旧快照

    def test_report_apt_count_not_overridden_by_extra(self) -> None:
        # extra 含恶意/巧合的 "apt" key 时，真实 apt 计数（body.apt）必须最终生效，不被 extra 覆盖
        store = {"rows": [], "deleted": []}
        client = self._client(store)
        r = client.post("/v1/internal/versions/report",
                        headers={"X-Backup-Report-Token": "test-token"},
                        json={"device": "aliecs",
                              "containers": [],
                              "apt": {"upgradable": 3, "security": 1},
                              "extra": {"apt": 999, "openclaw": "2026.6.5"}})
        self.assertEqual(r.status_code, 200)
        apt_row = next(p for p in store["rows"] if p[1] == "apt-summary")
        extra_json = apt_row[4]  # Jsonb 对象
        payload = extra_json.obj if hasattr(extra_json, "obj") else json.loads(str(extra_json))
        self.assertEqual(payload["apt"], {"upgradable": 3, "security": 1})
        self.assertEqual(payload["openclaw"], "2026.6.5")
