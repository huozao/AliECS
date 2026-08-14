#!/usr/bin/env python3
"""Validate repository-owned AI navigation without reading runtime state."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
PATH_MARKER_RE = re.compile(r"<!--\s*nav-check:\s*([^>]+?)\s*-->")
PY_MARKER_RE = re.compile(r"<!--\s*nav-check-python:\s*([^:>]+):([^>]+?)\s*-->")


def outside_fences(text: str) -> str:
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return "\n".join(lines)


def clean_link(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = value.split(maxsplit=1)[0]
    return unquote(value.split("#", 1)[0].split("?", 1)[0])


def symbol_names(path: Path) -> set[str]:
    """收集模块级与类级定义，刻意不进入函数体。

    局部变量若也算命中，断言一个随处可见的名字（status、config、click）就会
    永远为真，等于装了个哑掉的警报器。模块级的 if/try/with/for 仍要下探，
    否则 try 包着的条件定义会被误判成缺失。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()

    def collect(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
                continue
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
                collect(node.body)
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                continue
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(node, field, None)
                if isinstance(inner, list):
                    collect(inner)
            for handler in getattr(node, "handlers", []):
                collect(handler.body)

    collect(tree.body)
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default=".navigation-check.json")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = json.loads(config_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    optional_prefixes = tuple(config.get("optional_link_prefixes", []))
    explicit_paths = list(config.get("paths", []))
    for relative in config.get("optional_paths", []):
        if (root / Path(relative).parts[0]).exists():
            explicit_paths.append(relative)
    python_symbols = list(config.get("python_symbols", []))

    for document in config.get("documents", []):
        doc = root / document
        if not doc.exists():
            errors.append(f"missing document: {document}")
            continue
        text = outside_fences(doc.read_text(encoding="utf-8"))
        explicit_paths.extend(match.strip() for match in PATH_MARKER_RE.findall(text))
        python_symbols.extend(
            {"file": file.strip(), "symbol": symbol.strip()}
            for file, symbol in PY_MARKER_RE.findall(text)
        )
        for raw in LINK_RE.findall(text):
            link = clean_link(raw)
            if not link or link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            optional = next(
                (prefix for prefix in optional_prefixes
                 if link == prefix or link.startswith(prefix.rstrip("/") + "/")),
                None,
            )
            if optional and (optional.startswith("..") or not (root / optional).exists()):
                continue
            if any(token in link for token in ("*", "<", ">", "${")):
                continue
            target = (doc.parent / link).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                errors.append(f"link escapes root: {document} -> {link}")
                continue
            if not target.exists():
                errors.append(f"broken link: {document} -> {link}")

    for relative in explicit_paths:
        if any(token in relative for token in ("*", "<", ">", "${")):
            errors.append(f"invalid explicit path marker: {relative}")
        elif not (root / relative).exists():
            errors.append(f"missing path: {relative}")

    for item in python_symbols:
        relative = item["file"]
        symbol = item["symbol"]
        path = root / relative
        if not path.is_file():
            errors.append(f"missing Python file: {relative}")
            continue
        try:
            names = symbol_names(path)
        except (SyntaxError, UnicodeError) as exc:
            errors.append(f"cannot parse Python file {relative}: {exc}")
            continue
        if symbol not in names:
            errors.append(f"missing Python symbol: {relative}:{symbol}")

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(
        "navigation valid: "
        f"documents={len(config.get('documents', []))} "
        f"paths={len(explicit_paths)} symbols={len(python_symbols)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
