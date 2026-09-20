#!/usr/bin/env python3
"""Inspect a manually-created market browser storage state without exposing it.

The operator completes OIDC and ``market.read`` authorization manually in a
dedicated browser profile.  This helper deliberately does not open a browser,
connect CDP, submit credentials, or print cookies/tokens.  It only accepts a
state file outside this public repository, requires mode 0600, and emits safe
metadata that an acceptance record may contain.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-state", type=Path, required=True,
                        help="0600 Playwright storageState JSON outside this repository")
    args = parser.parse_args()
    state = args.storage_state.resolve()
    repository = Path(__file__).resolve().parents[1]
    try:
        state.relative_to(repository)
    except ValueError:
        pass
    else:
        parser.error("storage state must stay outside the public repository")
    if not state.is_file():
        parser.error("storage state does not exist")
    mode = state.stat().st_mode & 0o777
    if mode != 0o600:
        parser.error(f"storage state must have mode 0600, found {mode:03o}")
    try:
        payload = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"storage state is not readable JSON: {error}")
    raw = json.dumps(payload, ensure_ascii=False)
    print(json.dumps({
        "ok": True,
        "storage_state_path": str(state),
        "mode": "0600",
        "origin_count": len(payload.get("origins", [])) if isinstance(payload, dict) else 0,
        "cookie_count": len(payload.get("cookies", [])) if isinstance(payload, dict) else 0,
        "authorization_key_present": '"Authorization"' in raw or '"authorization"' in raw,
        "manual_login_required": True,
        "secrets_emitted": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
