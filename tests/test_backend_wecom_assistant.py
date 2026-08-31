from __future__ import annotations

import base64
import importlib
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))


def _module():
    # DATABASE_URL 只在 import 期间需要，用完必须还原：这里原来是裸的 setdefault，
    # 进程环境被永久污染，后面 test_backend_wecom_kf 的两个用例就会走进
    # task_store_for_env() 的「有库」分支，去 mkdir /app/wecom-kf-materials 而失败
    # （2026-08-31 实测；单独跑那个文件不会复现，只在全量顺序下出现）。
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ.setdefault("DATABASE_URL", "postgresql://unit-test/not-used")
    previous_modules = {
        name: module for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    previous_path = list(sys.path)
    try:
        for name in previous_modules:
            del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        return importlib.import_module("app.routers.wecom_assistant")
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(previous_modules)
        sys.path[:] = previous_path
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


def test_parse_node_commands_are_separate() -> None:
    mod = _module()
    assert mod._parse_node_command("#节点 打样 第一版完成") == (True, "打样", "第一版完成")
    assert mod._parse_node_command("#AI节点 识图 判断色差", ai=True) == (True, "识图", "判断色差")
    assert mod._parse_node_command("#AI节点 识图 判断色差") == (False, "", "")


def test_first_text_handles_wecom_cells() -> None:
    mod = _module()
    assert mod._first_text([{"type": "text", "text": "202607160001"}]) == "202607160001"
    assert mod._approval_from_link("https://example.test/detail?sp_no=202607160002") == "202607160002"


def test_requirement_source_is_resolved_from_registered_table_without_hardcoded_docid(monkeypatch) -> None:
    mod = _module()
    monkeypatch.delenv("WECOM_RND_DOCID", raising=False)

    class Cursor:
        def execute(self, query, params=None):
            self.query, self.params = str(query), params

        def fetchall(self):
            return [("dc" + "x" * 80,)]

    cursor = Cursor()
    assert mod._requirement_source(cursor) == ("dc" + "x" * 80, "配色&样品需求单")
    assert "external_sources" in cursor.query
    assert cursor.params == ("配色&样品需求单",)


def test_public_source_contains_no_real_wecom_document_identifier() -> None:
    source = (BACKEND_ROOT / "app" / "routers" / "wecom_assistant.py").read_text(encoding="utf-8")
    import re

    assert re.search(r'dc[A-Za-z0-9_-]{60,}', source) is None


def test_save_images_is_content_addressed_and_idempotent(tmp_path, monkeypatch) -> None:
    mod = _module()
    monkeypatch.setenv("WECOM_GROUP_MEDIA_DIR", str(tmp_path))
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsmall").decode("ascii")
    first = mod._save_images("msg-1", [data_url])
    second = mod._save_images("msg-1", [data_url])
    assert first == second
    assert len(first) == 1
    assert Path(first[0]).read_bytes().endswith(b"small")


def test_internal_token_is_required(monkeypatch) -> None:
    from fastapi import HTTPException

    mod = _module()
    monkeypatch.setenv("OPENCLAW_INTERNAL_TOKEN", "expected")
    try:
        mod._require_internal_token("wrong")
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("invalid token must be rejected")


def test_unbound_group_plain_question_continues_to_ai(monkeypatch) -> None:
    mod = _module()

    class FakeCursor:
        def __init__(self) -> None:
            self.query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None) -> None:
            self.query = str(query)

        def fetchone(self):
            if "INSERT INTO group_messages" in self.query:
                return (1,)
            return None

        def fetchall(self):
            return []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setenv("OPENCLAW_INTERNAL_TOKEN", "expected")
    monkeypatch.setattr(mod, "_conn", FakeConnection)
    result = mod.wecom_inbound(
        mod.InboundMessage(
            msgid="plain-question-1",
            chatid="wr_group",
            chattype="group",
            from_userid="WangHao",
            text_content="后天天气怎么样",
        ),
        "expected",
    )

    assert result == {"action": "continue", "reply": ""}
