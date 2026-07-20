"""微信客服资料任务：持久化收集、分析确认和安全归档。"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import uuid

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row


OPEN_TASK_STATUSES = (
    "collecting",
    "analyzing",
    "awaiting_confirmation",
    "executing",
    "failed",
)
SUPPORTED_MEDIA_TYPES = frozenset({"image", "file"})
PROCESSOR_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/avif",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/zip",
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
    }
)
START_RE = re.compile(r"^(?:开始任务|新建任务)(?:\s*[:：]\s*(.*))?$", re.IGNORECASE)
FINISH_COMMANDS = frozenset({"发送完毕", "开始处理", "资料发送完毕"})
RETRY_COMMANDS = frozenset({"重试处理", "重新处理"})
CONFIRM_COMMANDS = frozenset({"确认处理", "确认归档"})
SUPPLEMENT_COMMANDS = frozenset({"补充资料", "继续补充"})
CANCEL_COMMANDS = frozenset({"取消任务", "取消处理"})
MENU_RE = re.compile(r"^kf_(confirm|supplement|cancel)_([0-9a-f]{32})$")


@dataclass(frozen=True)
class DownloadedMedia:
    data: bytes
    mime_type: str
    filename: str


@dataclass(frozen=True)
class ProcessorAttachment:
    filename: str
    mime_type: str
    byte_size: int
    data_url: str


@dataclass(frozen=True)
class TaskOutcome:
    handled: bool
    reply_type: str = ""
    text: str = ""
    purpose: str = "task"
    task_id: int | None = None
    task_key: str = ""


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _safe_filename(value: str, mime_type: str) -> str:
    name = Path(str(value or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name).strip("._")
    if not name:
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
        name = "attachment" + suffix
    stem = Path(name).stem[:80] or "attachment"
    suffix = Path(name).suffix[:16]
    return stem + suffix


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


class PostgresKfTaskStore:
    def __init__(self, database_url: str, storage_root: str) -> None:
        self.database_url = database_url
        self.storage_root = Path(storage_root).resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "PostgresKfTaskStore | None":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            return None
        storage_root = os.getenv(
            "WECOM_KF_TASK_STORAGE_DIR", "/app/wecom-kf-materials"
        ).strip()
        return cls(database_url, storage_root)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            self.database_url,
            connect_timeout=3,
            row_factory=dict_row,
        )

    def get_cursor(self, open_kfid: str) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cursor FROM wecom_kf_cursors WHERE open_kfid=%s",
                (open_kfid,),
            )
            row = cur.fetchone()
        return str(row["cursor"] if row else "")

    def set_cursor(self, open_kfid: str, cursor: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wecom_kf_cursors(open_kfid, cursor)
                VALUES (%s, %s)
                ON CONFLICT(open_kfid) DO UPDATE
                SET cursor=EXCLUDED.cursor, updated_at=NOW()
                """,
                (open_kfid, cursor),
            )

    def active_task(self, open_kfid: str, external_userid: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM wecom_kf_material_tasks
                WHERE open_kfid=%s AND external_userid=%s
                  AND status = ANY(%s)
                ORDER BY id DESC LIMIT 1
                """,
                (open_kfid, external_userid, list(OPEN_TASK_STATUSES)),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def task_by_key(
        self, open_kfid: str, external_userid: str, task_key: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM wecom_kf_material_tasks
                WHERE open_kfid=%s AND external_userid=%s AND task_key=%s
                """,
                (open_kfid, external_userid, task_key),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def create_task(
        self, open_kfid: str, external_userid: str, title: str
    ) -> dict[str, Any]:
        task_key = uuid.uuid4().hex
        safe_title = str(title or "").strip()[:240] or "微信资料任务"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wecom_kf_material_tasks(
                    task_key, open_kfid, external_userid, title
                ) VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (task_key, open_kfid, external_userid, safe_title),
            )
            row = cur.fetchone()
        self.task_dir(task_key).mkdir(parents=True, exist_ok=True)
        return dict(row)

    def set_status(
        self, task_id: int, status: str, *, analysis: str = "", error_text: str = ""
    ) -> None:
        extra = ""
        if status == "completed":
            extra = ", completed_at=NOW()"
        elif status == "executing":
            extra = ", confirmed_at=NOW()"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE wecom_kf_material_tasks
                SET status=%s,
                    analysis_text=CASE WHEN %s <> '' THEN %s ELSE analysis_text END,
                    last_error=%s,
                    updated_at=NOW(){extra}
                WHERE id=%s
                """,
                (status, analysis, analysis, error_text[:1000], task_id),
            )

    def add_text(self, task_id: int, msgid: str, content: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wecom_kf_material_items(
                    task_id, msgid, msgtype, text_content
                ) VALUES (%s, %s, 'text', %s)
                ON CONFLICT(msgid) DO NOTHING
                RETURNING id
                """,
                (task_id, msgid, content[:10000]),
            )
            return cur.fetchone() is not None

    def add_media(
        self,
        task: dict[str, Any],
        msgid: str,
        msgtype: str,
        media: DownloadedMedia,
    ) -> bool:
        digest = hashlib.sha256(media.data).hexdigest()
        filename = _safe_filename(media.filename, media.mime_type)
        mime_type = media.mime_type.lower().split(";", 1)[0].strip()
        if not mime_type or mime_type == "application/octet-stream":
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        path = self.task_dir(str(task["task_key"])) / "originals" / (
            f"{Path(filename).stem[:80]}-{digest[:12]}{Path(filename).suffix[:16]}"
        )
        if not path.exists():
            _atomic_write(path, media.data)
        relative_path = path.relative_to(self.storage_root).as_posix()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wecom_kf_material_items(
                    task_id, msgid, msgtype, original_filename, mime_type,
                    byte_size, sha256, storage_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(msgid) DO NOTHING
                RETURNING id
                """,
                (
                    task["id"],
                    msgid,
                    msgtype,
                    filename,
                    mime_type,
                    len(media.data),
                    digest,
                    relative_path,
                ),
            )
            return cur.fetchone() is not None

    def items(self, task_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM wecom_kf_material_items
                WHERE task_id=%s ORDER BY id
                """,
                (task_id,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def task_dir(self, task_key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task_key):
            raise ValueError("invalid task key")
        return self.storage_root / task_key

    def processor_attachments(
        self, task_id: int
    ) -> tuple[list[ProcessorAttachment], int]:
        limit = _positive_int_env("WECOM_KF_TASK_MAX_ATTACHMENTS", 12)
        max_total = _positive_int_env("WECOM_KF_TASK_MAX_TOTAL_MB", 40) * 1024 * 1024
        selected: list[ProcessorAttachment] = []
        skipped = 0
        total = 0
        for item in self.items(task_id):
            path_value = str(item.get("storage_path") or "")
            mime_type = str(item.get("mime_type") or "").lower()
            path = (self.storage_root / path_value).resolve()
            if (
                not path_value
                or not path.is_relative_to(self.storage_root)
                or mime_type not in PROCESSOR_MIME_TYPES
                or len(selected) >= limit
            ):
                if path_value:
                    skipped += 1
                continue
            try:
                data = path.read_bytes()
            except OSError:
                skipped += 1
                continue
            if not data or len(data) > 20 * 1024 * 1024 or total + len(data) > max_total:
                skipped += 1
                continue
            total += len(data)
            selected.append(
                ProcessorAttachment(
                    filename=str(item.get("original_filename") or path.name),
                    mime_type=mime_type,
                    byte_size=len(data),
                    data_url=(
                        f"data:{mime_type};base64," + base64.b64encode(data).decode("ascii")
                    ),
                )
            )
        return selected, skipped

    def write_archive(self, task: dict[str, Any], items: list[dict[str, Any]]) -> None:
        directory = self.task_dir(str(task["task_key"]))
        manifest = {
            "task_key": task["task_key"],
            "title": task.get("title") or "",
            "status": "completed",
            "items": [
                {
                    "msgtype": item.get("msgtype"),
                    "text_content": item.get("text_content") or "",
                    "original_filename": item.get("original_filename") or "",
                    "mime_type": item.get("mime_type") or "",
                    "byte_size": int(item.get("byte_size") or 0),
                    "sha256": item.get("sha256") or "",
                    "storage_path": item.get("storage_path") or "",
                }
                for item in items
            ],
        }
        _atomic_write(
            directory / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        analysis = str(task.get("analysis_text") or "").strip()
        _atomic_write(
            directory / "analysis.md",
            (f"# {task.get('title') or '微信资料任务'}\n\n{analysis}\n").encode("utf-8"),
        )

    def record_outbound(
        self,
        msgid: str,
        task_id: int | None,
        open_kfid: str,
        external_userid: str,
        purpose: str,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wecom_kf_outbound_messages(
                    msgid, task_id, open_kfid, external_userid, purpose
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(msgid) DO UPDATE
                SET task_id=EXCLUDED.task_id, purpose=EXCLUDED.purpose, updated_at=NOW()
                """,
                (msgid, task_id, open_kfid, external_userid, purpose),
            )

    def mark_outbound_sent(self, msgid: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wecom_kf_outbound_messages
                SET status='sent', updated_at=NOW() WHERE msgid=%s
                """,
                (msgid,),
            )

    def mark_outbound_failed(self, msgid: str, fail_type: int) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wecom_kf_outbound_messages
                SET status='failed', fail_type=%s, updated_at=NOW() WHERE msgid=%s
                """,
                (fail_type, msgid),
            )


def _message_text(message: dict[str, Any]) -> str:
    return str((message.get("text") or {}).get("content") or "").strip()


def _menu_command(message: dict[str, Any]) -> tuple[str, str]:
    menu_id = str((message.get("text") or {}).get("menu_id") or "").strip()
    match = MENU_RE.fullmatch(menu_id)
    return (match.group(1), match.group(2)) if match else ("", "")


def _task_label(task: dict[str, Any]) -> str:
    return "#" + str(task["task_key"])[:8]


def _analysis_prompt(task: dict[str, Any], items: list[dict[str, Any]], skipped: int) -> str:
    text_items = [
        str(item.get("text_content") or "").strip()
        for item in items
        if item.get("msgtype") == "text" and str(item.get("text_content") or "").strip()
    ]
    media_items = [item for item in items if item.get("storage_path")]
    file_lines = [
        f"- {item.get('original_filename') or '附件'} | {item.get('mime_type') or '未知类型'} | "
        f"{int(item.get('byte_size') or 0)} bytes"
        for item in media_items
    ]
    skipped_note = f"\n其中有 {skipped} 个附件只归档、未上传给模型。" if skipped else ""
    return (
        "你是资料整理智能体。请只根据本批次说明和附件提出整理方案，不要声称已经执行文件操作。\n"
        "输出必须包含：资料识别、建议归档位置、拟执行操作、风险或需补充信息。\n"
        "回答应简洁，适合在微信里确认。\n\n"
        f"任务：{task.get('title') or '微信资料任务'}\n"
        f"用户说明：{'；'.join(text_items) if text_items else '未提供额外说明'}\n"
        f"附件清单：\n{chr(10).join(file_lines) if file_lines else '- 无附件'}"
        f"{skipped_note}\n"
    )


class KfTaskCoordinator:
    def __init__(self, store: PostgresKfTaskStore) -> None:
        self.store = store

    def handle(
        self,
        message: dict[str, Any],
        *,
        download_media: Callable[[str], DownloadedMedia],
        analyze: Callable[[str, list[ProcessorAttachment]], str],
    ) -> TaskOutcome | None:
        msgid = str(message.get("msgid") or "").strip()
        open_kfid = str(message.get("open_kfid") or "").strip()
        external_userid = str(message.get("external_userid") or "").strip()
        msgtype = str(message.get("msgtype") or "").strip().lower()
        if not msgid or not open_kfid or not external_userid:
            return None

        text = _message_text(message)
        menu_action, menu_task_key = _menu_command(message)
        active = self.store.active_task(open_kfid, external_userid)

        start_match = START_RE.fullmatch(text)
        if start_match:
            if active:
                return TaskOutcome(
                    True,
                    "text",
                    f"当前已有资料任务 {_task_label(active)}，请先发送“发送完毕”或“取消任务”。",
                    "task_busy",
                    int(active["id"]),
                    str(active["task_key"]),
                )
            task = self.store.create_task(
                open_kfid, external_userid, str(start_match.group(1) or "").strip()
            )
            return TaskOutcome(
                True,
                "text",
                f"已创建资料任务 {_task_label(task)}。请继续发送说明、图片或文件，完成后发送“发送完毕”。",
                "task_started",
                int(task["id"]),
                str(task["task_key"]),
            )

        if msgtype in SUPPORTED_MEDIA_TYPES:
            if active and active["status"] in {"analyzing", "executing"}:
                return TaskOutcome(
                    True,
                    "text",
                    f"资料任务 {_task_label(active)} 正在处理，请稍后再补充。",
                    "task_busy",
                    int(active["id"]),
                    str(active["task_key"]),
                )
            created = active is None
            task = active or self.store.create_task(open_kfid, external_userid, "微信资料任务")
            if task["status"] in {"awaiting_confirmation", "failed"}:
                self.store.set_status(int(task["id"]), "collecting")
                task["status"] = "collecting"
            media_id = str((message.get(msgtype) or {}).get("media_id") or "").strip()
            if not media_id:
                return TaskOutcome(True, "text", "附件缺少 media_id，无法保存。", "task_media_error")
            media = download_media(media_id)
            supplied_filename = str(
                (message.get(msgtype) or {}).get("filename")
                or (message.get(msgtype) or {}).get("file_name")
                or ""
            ).strip()
            if supplied_filename:
                media = DownloadedMedia(media.data, media.mime_type, supplied_filename)
            self.store.add_media(task, msgid, msgtype, media)
            if created:
                return TaskOutcome(
                    True,
                    "text",
                    f"已创建资料任务 {_task_label(task)} 并保存首个附件。请继续发送，完成后发送“发送完毕”。",
                    "task_started",
                    int(task["id"]),
                    str(task["task_key"]),
                )
            return TaskOutcome(True, task_id=int(task["id"]), task_key=str(task["task_key"]))

        if msgtype != "text":
            return None

        action = menu_action
        target = None
        if menu_task_key:
            target = self.store.task_by_key(open_kfid, external_userid, menu_task_key)
        if not action:
            if text in CONFIRM_COMMANDS:
                action = "confirm"
            elif text in SUPPLEMENT_COMMANDS:
                action = "supplement"
            elif text in CANCEL_COMMANDS:
                action = "cancel"
            target = target or active

        if action:
            if not target:
                return TaskOutcome(True, "text", "没有可操作的资料任务。", "task_missing")
            if action == "confirm":
                if target["status"] != "awaiting_confirmation":
                    return TaskOutcome(True, "text", "该任务当前不在待确认状态。", "task_state")
                self.store.set_status(int(target["id"]), "executing")
                target["status"] = "executing"
                target["analysis_text"] = str(target.get("analysis_text") or "")
                items = self.store.items(int(target["id"]))
                self.store.write_archive(target, items)
                self.store.set_status(int(target["id"]), "completed")
                media_count = sum(1 for item in items if item.get("storage_path"))
                return TaskOutcome(
                    True,
                    "text",
                    f"✅ 资料任务 {_task_label(target)} 已确认归档，共保存 {media_count} 个附件和分析结果。",
                    "task_completed",
                    int(target["id"]),
                    str(target["task_key"]),
                )
            if action == "supplement":
                if target["status"] not in {"awaiting_confirmation", "failed"}:
                    return TaskOutcome(True, "text", "该任务当前不能补充资料。", "task_state")
                self.store.set_status(int(target["id"]), "collecting")
                return TaskOutcome(
                    True,
                    "text",
                    f"资料任务 {_task_label(target)} 已恢复收集。补充完成后请再次发送“发送完毕”。",
                    "task_supplement",
                    int(target["id"]),
                    str(target["task_key"]),
                )
            if action == "cancel":
                self.store.set_status(int(target["id"]), "cancelled")
                return TaskOutcome(
                    True,
                    "text",
                    f"资料任务 {_task_label(target)} 已取消；已接收原件保留，不执行归档确认。",
                    "task_cancelled",
                    int(target["id"]),
                    str(target["task_key"]),
                )

        if text in FINISH_COMMANDS or text in RETRY_COMMANDS:
            task = active
            if not task or task["status"] not in {"collecting", "failed"}:
                return TaskOutcome(True, "text", "没有等待处理的资料任务。", "task_missing")
            items = self.store.items(int(task["id"]))
            if not items:
                return TaskOutcome(True, "text", "当前资料任务还是空的，请先发送说明或附件。", "task_empty")
            self.store.set_status(int(task["id"]), "analyzing")
            try:
                attachments, skipped = self.store.processor_attachments(int(task["id"]))
                analysis = analyze(_analysis_prompt(task, items, skipped), attachments).strip()
                if not analysis:
                    raise RuntimeError("empty processor reply")
                self.store.set_status(int(task["id"]), "awaiting_confirmation", analysis=analysis)
            except Exception as exc:
                self.store.set_status(
                    int(task["id"]), "failed", error_text=type(exc).__name__
                )
                return TaskOutcome(
                    True,
                    "text",
                    f"资料任务 {_task_label(task)} 分析失败，但原件已保存。可稍后发送“重试处理”。",
                    "task_analysis_failed",
                    int(task["id"]),
                    str(task["task_key"]),
                )
            return TaskOutcome(
                True,
                "menu",
                f"资料任务 {_task_label(task)}\n{analysis}\n\n请选择下一步：",
                "task_review",
                int(task["id"]),
                str(task["task_key"]),
            )

        if active and active["status"] == "collecting" and text:
            self.store.add_text(int(active["id"]), msgid, text)
            return TaskOutcome(
                True,
                task_id=int(active["id"]),
                task_key=str(active["task_key"]),
            )

        return None
