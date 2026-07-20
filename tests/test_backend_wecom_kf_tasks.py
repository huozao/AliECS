from __future__ import annotations

import importlib
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))


def _module():
    return importlib.import_module("app.integrations.wecom_kf_tasks")


class FakeStore:
    def __init__(self) -> None:
        self.task = None
        self.rows = []
        self.archived = False

    def active_task(self, open_kfid, external_userid):
        if self.task and self.task["status"] in {
            "collecting", "analyzing", "awaiting_confirmation", "executing", "failed"
        }:
            return self.task
        return None

    def task_by_key(self, open_kfid, external_userid, task_key):
        return self.task if self.task and self.task["task_key"] == task_key else None

    def create_task(self, open_kfid, external_userid, title):
        self.task = {
            "id": 1,
            "task_key": "a" * 32,
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "title": title or "微信资料任务",
            "status": "collecting",
            "analysis_text": "",
        }
        return self.task

    def set_status(self, task_id, status, *, analysis="", error_text=""):
        self.task["status"] = status
        if analysis:
            self.task["analysis_text"] = analysis
        self.task["last_error"] = error_text

    def add_text(self, task_id, msgid, content):
        if any(row["msgid"] == msgid for row in self.rows):
            return False
        self.rows.append({"msgid": msgid, "msgtype": "text", "text_content": content})
        return True

    def add_media(self, task, msgid, msgtype, media):
        self.rows.append(
            {
                "msgid": msgid,
                "msgtype": msgtype,
                "storage_path": f"originals/{media.filename}",
                "original_filename": media.filename,
                "mime_type": media.mime_type,
                "byte_size": len(media.data),
            }
        )
        return True

    def items(self, task_id):
        return list(self.rows)

    def processor_attachments(self, task_id):
        mod = _module()
        return [mod.ProcessorAttachment("photo.jpg", "image/jpeg", 3, "data:image/jpeg;base64,eHl6")], 0

    def write_archive(self, task, items):
        self.archived = True


def _message(msgid: str, msgtype: str, payload: dict) -> dict:
    return {
        "msgid": msgid,
        "origin": 3,
        "open_kfid": "wk1",
        "external_userid": "wm1",
        "msgtype": msgtype,
        msgtype: payload,
    }


def test_material_task_collect_analyze_confirm_workflow() -> None:
    mod = _module()
    store = FakeStore()
    coordinator = mod.KfTaskCoordinator(store)
    analyzed = {}

    start = coordinator.handle(
        _message("m1", "text", {"content": "开始任务：整理合同"}),
        download_media=lambda _: None,
        analyze=lambda *_: "",
    )
    assert start.reply_type == "text"
    assert "#aaaaaaaa" in start.text

    note = coordinator.handle(
        _message("m2", "text", {"content": "这是供应商合同"}),
        download_media=lambda _: None,
        analyze=lambda *_: "",
    )
    assert note.handled is True and note.reply_type == ""

    media = coordinator.handle(
        _message("m3", "image", {"media_id": "media-1"}),
        download_media=lambda _: mod.DownloadedMedia(b"xyz", "image/jpeg", "photo.jpg"),
        analyze=lambda *_: "",
    )
    assert media.handled is True and media.reply_type == ""

    def analyze(prompt, attachments):
        analyzed["prompt"] = prompt
        analyzed["attachments"] = attachments
        return "识别为合同；建议归档到供应商/合同；确认后生成归档清单。"

    review = coordinator.handle(
        _message("m4", "text", {"content": "发送完毕"}),
        download_media=lambda _: None,
        analyze=analyze,
    )
    assert review.reply_type == "menu"
    assert review.task_key == "a" * 32
    assert "这是供应商合同" in analyzed["prompt"]
    assert analyzed["attachments"][0].data_url.startswith("data:image/jpeg;base64,")
    assert store.task["status"] == "awaiting_confirmation"

    confirmed = coordinator.handle(
        _message(
            "m5",
            "text",
            {"content": "确认处理", "menu_id": "kf_confirm_" + "a" * 32},
        ),
        download_media=lambda _: None,
        analyze=lambda *_: "",
    )
    assert confirmed.purpose == "task_completed"
    assert store.task["status"] == "completed"
    assert store.archived is True


def test_first_attachment_creates_task_and_analysis_failure_is_retryable() -> None:
    mod = _module()
    store = FakeStore()
    coordinator = mod.KfTaskCoordinator(store)

    created = coordinator.handle(
        _message("m1", "file", {"media_id": "media-1"}),
        download_media=lambda _: mod.DownloadedMedia(b"doc", "application/pdf", "a.pdf"),
        analyze=lambda *_: "",
    )
    assert created.purpose == "task_started"
    assert "保存首个附件" in created.text

    failed = coordinator.handle(
        _message("m2", "text", {"content": "发送完毕"}),
        download_media=lambda _: None,
        analyze=lambda *_: (_ for _ in ()).throw(RuntimeError("processor down")),
    )
    assert failed.purpose == "task_analysis_failed"
    assert store.task["status"] == "failed"
    assert store.task["last_error"] == "RuntimeError"

    cancelled = coordinator.handle(
        _message("m3", "text", {"content": "取消任务"}),
        download_media=lambda _: None,
        analyze=lambda *_: "",
    )
    assert cancelled.purpose == "task_cancelled"
    assert store.task["status"] == "cancelled"
