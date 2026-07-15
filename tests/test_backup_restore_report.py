from __future__ import annotations

from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "services" / "backend-api" / "app" / "routers" / "backups.py"


def test_restore_check_internal_endpoint_is_defined() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    assert '"/v1/internal/backups/restore-check"' in text
    assert "INSERT INTO backup_restore_checks" in text
    assert "Depends(_require_backup_report_token)" in text
