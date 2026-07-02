from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app.routers import couple as main

    return main


class FakeCursor:
    def __init__(self, memory_space_row=(100,), rows=None, insert_row=None):
        self.memory_space_row = memory_space_row
        self.rows = rows or []
        self.insert_row = insert_row or (
            9,
            100,
            1,
            "immich",
            "asset-1",
            None,
            "a.jpg",
            "2026-03-20T12:00:00Z",
            30.1,
            120.2,
            None,
            0,
            7,
            "2026-06-12T00:00:00Z",
            "2026-06-12T00:00:00Z",
        )
        self.calls = []
        self._next = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split()).lower()
        self.calls.append((normalized, params))
        if "from memories m" in normalized and "join couple_members" in normalized:
            self._next = self.memory_space_row
        elif normalized.startswith("insert into couple_memory_assets"):
            self._next = self.insert_row
        elif normalized.startswith("select id, couple_space_id, memory_id"):
            self._next = list(self.rows)
        elif normalized.startswith("delete from couple_memory_assets"):
            self._next = (1,)
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def fetchall(self):
        return self._next or []


class FakeConn:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        pass


class CoupleImmichAssetTests(unittest.TestCase):
    def test_bind_manual_asset_when_immich_disabled(self) -> None:
        main = load_main()
        cursor = FakeCursor()
        conn = FakeConn(cursor)

        with (
            patch.object(main, "_conn", return_value=conn),
            patch.object(main, "_require_couple_user", return_value=7),
            patch.dict(os.environ, {"IMMICH_ENABLED": "false"}, clear=False),
        ):
            body = main.ImmichAssetBindRequest(immich_asset_id="asset-1", original_filename="a.jpg")
            result = main.bind_memory_immich_asset(1, body, {"sub": "alice"})

        self.assertEqual("asset-1", result["immich_asset_id"])
        self.assertEqual("a.jpg", result["original_filename"])
        self.assertTrue(conn.committed)

    def test_bind_requires_access_to_memory_space(self) -> None:
        main = load_main()
        cursor = FakeCursor(memory_space_row=None)

        with (
            patch.object(main, "_conn", return_value=FakeConn(cursor)),
            patch.object(main, "_require_couple_user", return_value=7),
            patch.dict(os.environ, {"IMMICH_ENABLED": "false"}, clear=False),
        ):
            body = main.ImmichAssetBindRequest(immich_asset_id="asset-1", original_filename="a.jpg")
            with self.assertRaises(HTTPException) as exc:
                main.bind_memory_immich_asset(1, body, {"sub": "alice"})

        self.assertEqual(404, exc.exception.status_code)

    def test_list_bound_assets(self) -> None:
        main = load_main()
        rows = [
            (
                9,
                100,
                1,
                "immich",
                "asset-1",
                None,
                "a.jpg",
                "2026-03-20T12:00:00Z",
                30.1,
                120.2,
                "thumb-1",
                0,
                7,
                "2026-06-12T00:00:00Z",
                "2026-06-12T00:00:00Z",
            )
        ]
        cursor = FakeCursor(rows=rows)

        with (
            patch.object(main, "_conn", return_value=FakeConn(cursor)),
            patch.object(main, "_require_couple_user", return_value=7),
        ):
            result = main.list_memory_immich_assets(1, {"sub": "alice"})

        self.assertEqual(1, len(result["items"]))
        self.assertEqual("asset-1", result["items"][0]["immich_asset_id"])
        self.assertEqual("thumb-1", result["items"][0]["thumbnail_cache_key"])

    def test_delete_bound_asset(self) -> None:
        main = load_main()
        cursor = FakeCursor()
        conn = FakeConn(cursor)

        with (
            patch.object(main, "_conn", return_value=conn),
            patch.object(main, "_require_couple_user", return_value=7),
        ):
            result = main.delete_memory_immich_asset(1, 9, {"sub": "alice"})

        self.assertEqual({"status": "ok"}, result)
        self.assertTrue(conn.committed)


if __name__ == "__main__":
    unittest.main()
