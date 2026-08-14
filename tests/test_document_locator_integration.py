from __future__ import annotations

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
WORKER_ROOT = (ROOT / "services" / "doc-sync-worker").resolve()
STORE_PATH = WORKER_ROOT / "app" / "storage" / "postgres.py"
BACKEND_LOCATOR_PATH = ROOT / "services" / "backend-api" / "app" / "document_locator.py"
STORE_MODULE = "_document_locator_integration_store"
BACKEND_MODULE = "_document_locator_integration_backend"
_MISSING = object()


def _load_module(name: str, path: Path) -> Any:
    previous = sys.modules.get(name, _MISSING)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load integration module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class DocumentLocatorPostgresIntegrationTests(unittest.TestCase):
    def test_real_registry_mirror_and_copy_transactions(self) -> None:
        database_url = os.getenv("DOCUMENT_LOCATOR_INTEGRATION_DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest("set DOCUMENT_LOCATOR_INTEGRATION_DATABASE_URL to run PostgreSQL integration")

        old_path = list(sys.path)
        old_app = {name: module for name, module in sys.modules.items() if name == "app" or name.startswith("app.")}
        try:
            for name in old_app:
                sys.modules.pop(name, None)
            sys.path.insert(0, str(WORKER_ROOT))
            store_module = _load_module(STORE_MODULE, STORE_PATH)
            backend_module = _load_module(BACKEND_MODULE, BACKEND_LOCATOR_PATH)
        finally:
            sys.path[:] = old_path
            for name in tuple(sys.modules):
                if name == "app" or name.startswith("app."):
                    sys.modules.pop(name, None)
            sys.modules.update(old_app)

        suffix = uuid.uuid4().hex
        resolved_docid = "dc" + (suffix * 3)
        copied_docid = "dc" + ((suffix[::-1]) * 3)
        share_ref = "s3_" + suffix
        conn = psycopg.connect(database_url, connect_timeout=5)
        source_ids: list[int] = []
        locator_ids: list[int] = []
        copy_request_id: int | None = None
        sync_request_ids: list[int] = []
        try:
            with conn.cursor() as cur:
                for external_id, source_type, name in (
                    (resolved_docid, "registry_doc", "CI resolved locator"),
                    (share_ref, "smartsheet_link", "CI unresolved locator"),
                ):
                    cur.execute(
                        """
                        INSERT INTO external_sources(
                            provider, env_profile, source_name, source_type, external_doc_id,
                            external_sheet_id, document_name, sheet_name, status
                        ) VALUES ('wecom', 'COMPANY_A', %s, %s, %s, '', %s, '', 'active')
                        RETURNING id
                        """,
                        (name, source_type, external_id, name),
                    )
                    source_ids.append(int(cur.fetchone()[0]))
            conn.commit()

            store = store_module.PostgresDocSyncStore(conn)
            resolved = store.upsert_document_locator(
                {
                    "provider": "wecom", "env_profile": "COMPANY_A", "api_doc_id": resolved_docid,
                    "document_name": "CI resolved locator", "source_kind": "registry",
                    "lifecycle_status": "active", "syncability_status": "verified",
                    "capabilities": {"read": "verified", "write": "unknown", "copy": "allowed"},
                    "external_source_id": source_ids[0],
                },
                event_type="ci-import", actor="ci",
            )
            unresolved = store.upsert_document_locator(
                {
                    "provider": "wecom", "env_profile": "COMPANY_A", "share_ref": share_ref,
                    "document_name": "CI unresolved locator", "source_kind": "registry",
                    "lifecycle_status": "unresolved", "syncability_status": "invalid-id",
                    "capabilities": {"read": "unavailable", "write": "unknown", "copy": "unavailable"},
                    "external_source_id": source_ids[1], "last_error_summary": "synthetic invalid id",
                },
                event_type="ci-import", actor="ci",
            )
            locator_ids.extend([int(resolved["id"]), int(unresolved["id"])])
            catalog = backend_module.asset_catalog(conn, tplus_items=[])
            company_a = next(group for group in catalog["groups"] if group["key"] == "wecom_company_a")
            self.assertEqual(set(source_ids), {item["source_id"] for item in company_a["items"]})
            serialized_catalog = json.dumps(catalog, default=str)
            self.assertNotIn(resolved_docid, serialized_catalog)
            self.assertNotIn(share_ref, serialized_catalog)
            jobs = store.claim_document_locator_mirror_jobs(limit=10)
            self.assertEqual(set(locator_ids), {int(job["locator_id"]) for job in jobs})
            store.finish_document_locator_mirror_job(int(jobs[0]["id"]))
            store.retry_document_locator_mirror_job(
                int(jobs[1]["id"]),
                f"token=synthetic-secret docid={resolved_docid}",
                60,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT last_error FROM document_locator_mirror_jobs WHERE id = %s", (int(jobs[1]["id"]),))
                safe_error = str(cur.fetchone()[0])
            self.assertNotIn("synthetic-secret", safe_error)
            self.assertNotIn(resolved_docid, safe_error)

            with conn.transaction():
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO document_locator_mirror_jobs(locator_id, locator_version, trigger) VALUES (%s, 0, 'ci-fail')",
                                (locator_ids[0],),
                            )
                except psycopg.Error:
                    pass
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    self.assertEqual(1, cur.fetchone()[0])

            class RepairClient:
                def get_doc_name(self, _docid: str) -> str:
                    return "CI resolved locator"

                def get_sheets(self, _docid: str) -> list[dict[str, str]]:
                    return [{"sheet_id": "synthetic-sheet"}]

            repaired = backend_module.repair_docid(
                lambda: psycopg.connect(database_url, connect_timeout=5),
                source_id=source_ids[1], api_doc_id=resolved_docid, requested_by="ci",
                client_factory=lambda _profile: RepairClient(),
            )
            self.assertEqual(source_ids[0], int(repaired["source_id"]))
            self.assertEqual(locator_ids[0], int(repaired["locator_id"]))
            sync_request_ids.append(int(repaired["sync_request_id"]))
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT api_doc_id, lifecycle_status, external_source_id FROM document_locator_registry WHERE id = %s",
                    (locator_ids[1],),
                )
                self.assertEqual((None, "disabled", None), cur.fetchone())

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO document_copy_requests(idempotency_key, source_id, requested_by, requested_name)
                    VALUES (%s, %s, 'ci', 'CI copied locator') RETURNING id
                    """,
                    (f"ci.locator.{suffix}", source_ids[0]),
                )
                copy_request_id = int(cur.fetchone()[0])
            conn.commit()
            registered = backend_module._register_external_copy(
                conn,
                copy_request_id=copy_request_id,
                env_profile="COMPANY_A",
                api_doc_id=copied_docid,
                source_url="",
                document_name="CI copied locator",
                requested_by="ci",
            )
            source_ids.append(int(registered["source_id"]))
            locator_ids.append(int(registered["locator_id"]))
            sync_request_ids.append(int(registered["sync_request_id"]))
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM document_locator_registry WHERE id = %s),
                        (SELECT COUNT(*) FROM document_locator_mirror_jobs WHERE locator_id = %s),
                        (SELECT COUNT(*) FROM sync_requests WHERE id = %s),
                        (SELECT status FROM document_copy_requests WHERE id = %s)
                    """,
                    (locator_ids[-1], locator_ids[-1], sync_request_ids[-1], copy_request_id),
                )
                self.assertEqual((1, 1, 1, "registered"), cur.fetchone())
        finally:
            try:
                with conn.cursor() as cur:
                    if copy_request_id is not None:
                        cur.execute("DELETE FROM document_copy_requests WHERE id = %s", (copy_request_id,))
                    if sync_request_ids:
                        cur.execute("DELETE FROM sync_requests WHERE id = ANY(%s)", (sync_request_ids,))
                    if locator_ids:
                        cur.execute("DELETE FROM document_locator_registry WHERE id = ANY(%s)", (locator_ids,))
                    if source_ids:
                        cur.execute("DELETE FROM external_sources WHERE id = ANY(%s)", (source_ids,))
                conn.commit()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM document_locator_registry WHERE id = ANY(%s)),
                            (SELECT COUNT(*) FROM external_sources WHERE id = ANY(%s)),
                            (SELECT COUNT(*) FROM document_copy_requests WHERE id = %s)
                        """,
                        (locator_ids or [0], source_ids or [0], copy_request_id or 0),
                    )
                    self.assertEqual((0, 0, 0), cur.fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
