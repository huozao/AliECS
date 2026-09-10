from __future__ import annotations

import logging
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services' / 'backend-api'))


def test_uvicorn_access_filter_removes_query_from_auth_callback_log():
    from app.main import _AccessPathOnlyFilter

    record = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ('127.0.0.1', 'GET', '/v1/auth/oidc/callback?code=secret&state=secret', '1.1', 302),
                               None)
    assert _AccessPathOnlyFilter().filter(record)
    assert record.args[2] == '/v1/auth/oidc/callback'
