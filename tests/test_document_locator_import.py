from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER))


VALID_DOCID = "d" + "c" + ("v" * 86)
OTHER_DOCID = "d" + "c" + ("w" * 86)
SHARE_REF = "s" + "3_" + ("u" * 30)


class FakeStore:
    def __init__(self, sources: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.sources = sources or {}
        self.upserts: list[tuple[dict[str, Any], str, str]] = []

    def find_document_locator_sources(self, *, api_doc_id: str = "", share_ref: str = "") -> list[dict[str, Any]]:
        return list(self.sources.get(api_doc_id or share_ref, []))

    def upsert_document_locator(
        self,
        locator: dict[str, Any],
        *,
        event_type: str,
        actor: str,
    ) -> dict[str, Any]:
        self.upserts.append((locator, event_type, actor))
        return {
            "id": len(self.upserts),
            "locator_version": 1,
            "changed": True,
            "created": locator["document_name"] != "existing-update",
            "mirror_job_id": len(self.upserts),
        }

    def close(self) -> None:
        return None


def registry_payload() -> dict[str, Any]:
    return {
        "docs": {
            VALID_DOCID: {
                "docid": VALID_DOCID,
                "doc_name": "stale-name",
                "url": "https://example.invalid/" + SHARE_REF,
                "admin_userid": "admin-fixture",
                "sheets": {"one": {"title": "One"}},
            },
            SHARE_REF: {
                "docid": SHARE_REF,
                "doc_name": "link-only",
                "url": "https://example.invalid/" + SHARE_REF,
                "sheets": {},
            },
        }
    }


class DocumentLocatorImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = importlib.import_module("app.pipelines.document_locator_import")

    def test_import_uses_exact_source_identity_and_live_name_profile(self) -> None:
        store = FakeStore(
            {
                VALID_DOCID: [
                    {
                        "id": 17,
                        "env_profile": "COMPANY_B",
                        "document_name": "live-name",
                        "source_url": "https://live.invalid/share",
                        "source_type": "smartsheet_doc",
                        "status": "active",
                        "sheet_count": 3,
                        "last_sync_at": "2026-08-14T00:00:00Z",
                    }
                ],
                SHARE_REF: [
                    {
                        "id": 18,
                        "env_profile": "COMPANY_A",
                        "document_name": "link-live-name",
                        "source_url": "https://link.invalid/share",
                        "source_type": "smartsheet_link",
                        "status": "active",
                        "sheet_count": 1,
                        "last_sync_at": None,
                    }
                ],
            }
        )

        result = self.module.import_document_locators(registry_payload(), store)

        self.assertEqual(
            {"inserted": 2, "updated": 0, "linked": 1, "unresolved": 1, "conflicts": 0},
            result,
        )
        resolved, unresolved = [item[0] for item in store.upserts]
        self.assertEqual("COMPANY_B", resolved["env_profile"])
        self.assertEqual("live-name", resolved["document_name"])
        self.assertEqual(VALID_DOCID, resolved["api_doc_id"])
        self.assertEqual(SHARE_REF, resolved["share_ref"])
        self.assertEqual("verified", resolved["syncability_status"])
        self.assertEqual("verified", resolved["capabilities"]["read"])
        self.assertIsNone(unresolved["api_doc_id"])
        self.assertEqual(SHARE_REF, unresolved["share_ref"])
        self.assertEqual("unresolved", unresolved["lifecycle_status"])
        self.assertEqual("invalid-id", unresolved["syncability_status"])

    def test_unmatched_resolved_entry_requires_explicit_profile(self) -> None:
        store = FakeStore()
        payload = {"docs": {OTHER_DOCID: {"docid": OTHER_DOCID, "doc_name": "unmatched"}}}

        result = self.module.import_document_locators(payload, store)

        self.assertEqual(1, result["conflicts"])
        self.assertEqual([], store.upserts)

        payload["docs"][OTHER_DOCID]["env_profile"] = "COMPANY_A"
        result = self.module.import_document_locators(payload, store)
        self.assertEqual(0, result["conflicts"])
        self.assertEqual("COMPANY_A", store.upserts[0][0]["env_profile"])

    def test_ambiguous_source_profile_is_conflict_and_names_never_match(self) -> None:
        sources = {
            VALID_DOCID: [
                {"id": 1, "env_profile": "COMPANY_A", "document_name": "same", "status": "active"},
                {"id": 2, "env_profile": "COMPANY_B", "document_name": "same", "status": "active"},
            ]
        }
        store = FakeStore(sources)

        result = self.module.import_document_locators(
            {"docs": {VALID_DOCID: {"docid": VALID_DOCID, "doc_name": "same"}}},
            store,
        )

        self.assertEqual(1, result["conflicts"])
        self.assertEqual([], store.upserts)

    def test_duplicate_registries_dedupe_and_cli_output_is_count_only(self) -> None:
        payload = {"registries": [registry_payload(), registry_payload()]}
        store = FakeStore(
            {
                VALID_DOCID: [
                    {
                        "id": 17,
                        "env_profile": "COMPANY_A",
                        "document_name": "live-name",
                        "status": "active",
                        "sheet_count": 1,
                        "last_sync_at": "2026-08-14T00:00:00Z",
                    }
                ],
                SHARE_REF: [
                    {
                        "id": 18,
                        "env_profile": "COMPANY_A",
                        "document_name": "link-only",
                        "status": "active",
                    }
                ],
            }
        )

        result = self.module.import_document_locators(payload, store)
        self.assertEqual(2, len(store.upserts))
        self.assertEqual(2, result["inserted"])

        output = io.StringIO()
        with mock.patch.object(self.module, "open_store", return_value=store), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(output):
            exit_code = self.module.run_import_document_locators_from_stdin()
        self.assertEqual(0, exit_code)
        rendered = output.getvalue()
        self.assertNotIn(VALID_DOCID, rendered)
        self.assertNotIn(SHARE_REF, rendered)
        self.assertNotIn("admin-fixture", rendered)
        self.assertEqual({"conflicts", "inserted", "linked", "unresolved", "updated"}, set(json.loads(rendered)))

    def test_main_exposes_import_command(self) -> None:
        main_module = importlib.import_module("app.main")
        with mock.patch.object(main_module, "run_import_document_locators_from_stdin", return_value=0) as run:
            self.assertEqual(0, main_module.main(["import-document-locators"]))
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
