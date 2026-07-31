#!/usr/bin/env python3
"""Validate Nav-Impact records in PR text or Git commit messages."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

IMPACT_RE = re.compile(r"(?mi)^Nav-Impact:\s*(updated|none)\s*$")
REASON_RE = re.compile(r"(?mi)^Nav-Impact-Reason:\s*(\S.+|\S)\s*$")
ZERO_SHA_RE = re.compile(r"^0+$")


def validate(text: str, label: str) -> list[str]:
    match = IMPACT_RE.search(text)
    if not match:
        return [f"{label}: missing 'Nav-Impact: updated|none'"]
    if match.group(1).lower() == "none" and not REASON_RE.search(text):
        return [f"{label}: Nav-Impact none requires Nav-Impact-Reason"]
    return []


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def messages_in_range(before: str, after: str) -> list[tuple[str, str]]:
    if ZERO_SHA_RE.match(before):
        revisions = git("rev-list", "--no-merges", after).splitlines()
    else:
        revisions = git("rev-list", "--no-merges", f"{before}..{after}").splitlines()
    return [(revision, git("show", "-s", "--format=%B", revision)) for revision in revisions]


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text-file")
    group.add_argument("--range", nargs=2, metavar=("BEFORE", "AFTER"))
    group.add_argument("--message")
    args = parser.parse_args()
    errors: list[str] = []
    checked = 0
    if args.text_file:
        errors.extend(validate(Path(args.text_file).read_text(encoding="utf-8"), "PR body"))
        checked = 1
    elif args.message is not None:
        errors.extend(validate(args.message, "message"))
        checked = 1
    else:
        before, after = args.range
        for revision, message in messages_in_range(before, after):
            checked += 1
            errors.extend(validate(message, revision[:12]))
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"Nav Impact valid: records={checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

