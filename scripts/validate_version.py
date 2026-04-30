from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"

VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")
TITLE_RE = re.compile(r"^##\s+(v\d+\.\d+\.\d+)：(.+)$", re.MULTILINE)


def fail(msg: str) -> None:
    print(f"[版本检查失败] {msg}")
    sys.exit(1)


if not VERSION_FILE.exists():
    fail("缺少 VERSION 文件。")

version = VERSION_FILE.read_text(encoding="utf-8").strip()
if not VERSION_RE.fullmatch(version):
    fail(f"VERSION 格式错误：{version}，必须是 vX.Y.Z。")

if not CHANGELOG_FILE.exists():
    fail("缺少 CHANGELOG.md 文件。")

changelog = CHANGELOG_FILE.read_text(encoding="utf-8")

matches = list(TITLE_RE.finditer(changelog))
if not matches:
    fail("CHANGELOG.md 未找到格式正确的版本标题（例如：## v2.1.4：说明）。")

found = None
for m in matches:
    if m.group(1) == version:
        found = m
        break

if not found:
    fail(f"CHANGELOG.md 中未找到当前 VERSION 对应标题：{version}")

summary = found.group(2).strip()
if not summary:
    fail(f"版本标题缺少中文说明：{version}")

if not re.search(r"[\u4e00-\u9fff]", summary):
    fail(f"版本标题说明必须包含中文：{version}：{summary}")

print(f"[版本检查通过] {version}：{summary}")
