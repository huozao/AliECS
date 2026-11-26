"""Update a recovery decision card using its Feishu delivery receipt."""
from __future__ import annotations

import logging
import re

from app.notify.channels import feishu

logger = logging.getLogger("aliecs.notify.recovery_cards")
_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]{1,128}\Z")
_QR_EVENTS = {"wechat.login.qr", "wechat.telegram.qr"}
_RETRY_EVENTS = {"wechat.login.retry", "wechat.telegram.retry"}
_STATUS = {
    "generating": "正在生成新二维码",
    "sent": "新二维码已发出，请扫描最新一张。",
    "skipped": "已记录，暂不处理",
    "unconfirmed": "新二维码送达尚未确认，请查看最新通知。",
}


def record_receipt(conn, delivery_id, notification, receipt):
    if notification.source != "wechat-digest" or not isinstance(receipt, dict):
        return
    if notification.event not in _QR_EVENTS and not (
        notification.event in _RETRY_EVENTS and feishu.is_wechat_recovery_decision(notification)
    ):
        return
    message_id = receipt.get("message_id")
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        return
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE notify_deliveries
               SET feishu_recovery_message_id = %s, feishu_recovery_qr_attached = %s
               WHERE id = %s""",
            (message_id, notification.event in _QR_EVENTS and receipt.get("qr_attached") is True, delivery_id),
        )


def refresh_cards(conn, limit):
    # Let the synchronous Feishu callback finish before the first PATCH. The exact
    # next attempt, same target and confirmed image receipt are required for “sent”.
    for _ in range(limit):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT d.id, d.feishu_recovery_message_id, d.target_json->>'profile',
                          n.decision_key, stage.value
                   FROM notify_deliveries d
                   JOIN notify_outbox o ON o.id = d.outbox_id
                   JOIN notify_decisions n
                     ON n.source_key = o.source_key
                    AND n.decision_key = o.payload_json->'decision'->>'key'
                   CROSS JOIN LATERAL (SELECT CASE
                       WHEN n.chosen_option = 'skip' THEN 'skipped'
                       WHEN EXISTS (
                           SELECT 1 FROM notify_outbox qo
                           JOIN notify_deliveries qd ON qd.outbox_id = qo.id
                           WHERE qo.source_key = n.source_key
                             AND qo.event IN ('wechat.login.qr', 'wechat.telegram.qr')
                             AND qo.dedup_key = split_part(n.decision_key, ':', 1) || ':' ||
                                 split_part(n.decision_key, ':', 2) || ':' ||
                                 (substring(n.decision_key FROM ':([0-9]{1,6})$')::int + 1)::text || ':qr'
                             AND qd.channel = 'feishu' AND qd.status = 'sent'
                             AND qd.target_json = d.target_json AND qd.feishu_recovery_qr_attached
                       ) THEN 'sent'
                       WHEN n.chosen_at < now() - interval '2 minutes' THEN 'unconfirmed'
                       ELSE 'generating'
                   END AS value) stage
                   WHERE o.source_key = 'wechat-digest'
                     AND o.event IN ('wechat.login.retry', 'wechat.telegram.retry')
                     AND d.channel = 'feishu' AND d.feishu_recovery_message_id IS NOT NULL
                     AND d.feishu_recovery_card_state IS DISTINCT FROM stage.value
                     AND COALESCE(d.feishu_recovery_retry_after, '-infinity') <= now()
                     AND n.chosen_option IN ('regen', 'skip')
                     AND n.chosen_at < now() - interval '4 seconds'
                     AND n.chosen_at > now() - interval '1 day'
                   ORDER BY n.chosen_at DESC
                   LIMIT 1 FOR UPDATE OF d SKIP LOCKED""",
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                break
            delivery_id, message_id, profile, key, stage = row
            service = "Telegram" if key.startswith("telegram-login:") else "微信"
            try:
                feishu.update_recovery_card(profile or "COMPANY_A", message_id, service + " 登录恢复", _STATUS[stage])
            except Exception as exc:
                logger.warning("recovery card update failed delivery=%s error=%s", delivery_id, type(exc).__name__)
                cur.execute(
                    "UPDATE notify_deliveries SET feishu_recovery_retry_after = now() + interval '1 minute' WHERE id = %s",
                    (delivery_id,),
                )
            else:
                cur.execute(
                    "UPDATE notify_deliveries SET feishu_recovery_card_state = %s WHERE id = %s",
                    (stage, delivery_id),
                )
        conn.commit()
