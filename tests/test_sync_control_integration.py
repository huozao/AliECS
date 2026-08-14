from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any

import psycopg


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = (ROOT / "services" / "backend-api").resolve()
WRITER_PATH = (ROOT / "services" / "doc-sync-worker" / "app" / "storage" / "sync_job_platform.py").resolve()
WRITER_MODULE = "_p5_sync_job_platform"
_MISSING = object()


def _load_modules() -> tuple[Any, Any]:
    old_path = list(sys.path)
    old_app = {name: module for name, module in sys.modules.items() if name == "app" or name.startswith("app.")}
    previous_writer = sys.modules.get(WRITER_MODULE, _MISSING)
    try:
        for name in tuple(old_app):
            sys.modules.pop(name, None)
        sys.path.insert(0, str(BACKEND_ROOT))
        control = importlib.import_module("app.sync_control")
        spec = importlib.util.spec_from_file_location(WRITER_MODULE, WRITER_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load sync job platform writer")
        writer = importlib.util.module_from_spec(spec)
        sys.modules[WRITER_MODULE] = writer
        spec.loader.exec_module(writer)
        return control, writer
    finally:
        sys.path[:] = old_path
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(old_app)
        if previous_writer is _MISSING:
            sys.modules.pop(WRITER_MODULE, None)
        else:
            sys.modules[WRITER_MODULE] = previous_writer


class SyncControlPostgresIntegrationTests(unittest.TestCase):
    def test_assets_catalog_reconciliation_and_run_all_use_real_postgresql(self) -> None:
        database_url = os.getenv("SYNC_CONTROL_INTEGRATION_DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest("set SYNC_CONTROL_INTEGRATION_DATABASE_URL to run PostgreSQL integration")

        control, writer_module = _load_modules()
        conn = psycopg.connect(database_url, connect_timeout=5)
        token = uuid.uuid4().hex
        source_ids: list[int] = []
        locator_ids: list[int] = []
        request_ids: list[int] = []
        tplus_request_id: int | None = None
        try:
            with conn.cursor() as cur:
                fixtures = [
                    ("wecom", "COMPANY_A", f"ci.p5.wecom.{token}", "registry_doc", "dc" + token * 3, "", "P5 企微A", ""),
                    ("wecom", "COMPANY_B", f"ci.p5.link.{token}", "smartsheet_link", "s3_ci_" + token, "", "P5 不可同步", ""),
                    ("feishu", "COMPANY_A", f"ci.p5.feishu.{token}", "bitable_app", "ci-app-" + token, "", "P5 飞书", ""),
                    ("wecom", "COMPANY_A", f"ci.p5.table.{token}", "smartsheet_sheet", "dc" + token * 3, "sheet-" + token, "P5 企微A", "明细"),
                    ("feishu", "COMPANY_A", f"ci.p5.ftable.{token}", "bitable_table", "ci-app-" + token, "table-" + token, "P5 飞书", "数据"),
                ]
                for provider, profile, name, source_type, docid, sheetid, document, sheet in fixtures:
                    cur.execute(
                        """
                        INSERT INTO external_sources(
                            provider, env_profile, source_name, source_type,
                            external_doc_id, external_sheet_id, document_name, sheet_name, status
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active') RETURNING id
                        """,
                        (provider, profile, name, source_type, docid, sheetid, document, sheet),
                    )
                    source_ids.append(int(cur.fetchone()[0]))
                # 资产目录以定位档案为主数据，来源行本身不再直接进目录。
                locator_fixtures = [
                    ("wecom", "COMPANY_A", "dc" + token * 3, None, "P5 企微A", "active", "verified",
                     '{"read":"verified","write":"unknown","copy":"allowed"}', source_ids[0]),
                    ("wecom", "COMPANY_B", None, "s3_ci_" + token, "P5 不可同步", "unresolved", "invalid-id",
                     '{"read":"unavailable","write":"unavailable","copy":"unavailable"}', None),
                    ("feishu", "COMPANY_A", "ci-app-" + token, None, "P5 飞书", "active", "verified",
                     '{"read":"verified","write":"unknown","copy":"unavailable"}', source_ids[2]),
                ]
                for provider, profile, api_doc_id, share_ref, document, lifecycle, syncability, capabilities, external_id in locator_fixtures:
                    cur.execute(
                        """
                        INSERT INTO document_locator_registry(
                            provider, env_profile, api_doc_id, share_ref, document_name, source_kind,
                            lifecycle_status, syncability_status, capabilities, external_source_id, last_verified_at
                        ) VALUES (%s,%s,%s,%s,%s,'ci-fixture',%s,%s,%s::jsonb,%s,NOW()) RETURNING id
                        """,
                        (provider, profile, api_doc_id, share_ref, document, lifecycle, syncability, capabilities, external_id),
                    )
                    locator_ids.append(int(cur.fetchone()[0]))
            conn.commit()

            reconciled = writer_module.SyncJobPlatformWriter(conn).reconcile_document_jobs()
            self.assertEqual({"enabled": 2, "disabled": 0}, reconciled)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_id, enabled FROM sync_jobs WHERE source_id = ANY(%s) ORDER BY source_id",
                    (source_ids,),
                )
                jobs = cur.fetchall()
                cur.execute("SELECT COUNT(*) FROM sync_job_runs j JOIN sync_jobs s ON s.id=j.job_id WHERE s.source_id=ANY(%s)", (source_ids,))
                self.assertEqual(0, int(cur.fetchone()[0]))
            self.assertEqual([(source_ids[3], True), (source_ids[4], True)], jobs)

            catalog = control.assets(conn, tplus_items=[{"name": "P5 T+", "updated_at": None}])
            payload = json.dumps(catalog, ensure_ascii=False, default=str)
            self.assertNotIn("external_doc_id", payload)
            self.assertNotIn("s3_ci_", payload)
            groups = {group["key"]: group for group in catalog["groups"]}
            self.assertEqual({"tplus", "wecom_company_a", "wecom_company_b", "feishu"}, set(groups))
            invalid = groups["wecom_company_b"]["items"][0]
            self.assertFalse(invalid["syncable"])
            self.assertNotIn("source_id", invalid)

            queued = control.enqueue_all(conn, "ci-p5")
            request_ids = [int(value) for value in queued["document_request_ids"]]
            tplus_request_id = int(queued["tplus_request_id"])
            self.assertEqual(2, queued["documents_queued"])
            with conn.cursor() as cur:
                cur.execute("SELECT source_id FROM sync_requests WHERE id=ANY(%s) ORDER BY source_id", (request_ids,))
                self.assertEqual([source_ids[0], source_ids[2]], [int(row[0]) for row in cur.fetchall()])
                cur.execute("UPDATE external_sources SET status='disabled' WHERE id=%s", (source_ids[3],))
            conn.commit()
            self.assertEqual({"enabled": 1, "disabled": 1}, writer_module.SyncJobPlatformWriter(conn).reconcile_document_jobs())
            with conn.cursor() as cur:
                cur.execute("SELECT enabled FROM sync_jobs WHERE source_id=%s", (source_ids[3],))
                self.assertFalse(bool(cur.fetchone()[0]))
        finally:
            conn.rollback()
            try:
                with conn.cursor() as cur:
                    if request_ids:
                        cur.execute("DELETE FROM sync_requests WHERE id=ANY(%s)", (request_ids,))
                    if tplus_request_id is not None:
                        cur.execute("DELETE FROM integration_sync_requests WHERE id=%s", (tplus_request_id,))
                    if locator_ids:
                        cur.execute("DELETE FROM document_locator_mirror_jobs WHERE locator_id=ANY(%s)", (locator_ids,))
                        cur.execute("DELETE FROM document_locator_events WHERE locator_id=ANY(%s)", (locator_ids,))
                        cur.execute("DELETE FROM document_locator_registry WHERE id=ANY(%s)", (locator_ids,))
                    if source_ids:
                        cur.execute("DELETE FROM sync_jobs WHERE source_id=ANY(%s)", (source_ids,))
                        cur.execute("DELETE FROM external_sources WHERE id=ANY(%s)", (source_ids,))
                conn.commit()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
