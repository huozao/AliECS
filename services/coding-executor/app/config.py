"""Configuration loading for the coding executor.

The executor runs on 开发机 (the local dev machine) and drives git operations
against an explicit allowlist of repositories. Nothing here is secret except
the bearer token, which is read from EXECUTOR_TOKEN or EXECUTOR_TOKEN_FILE.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path


def load_token() -> str:
    """Bearer token required on every request. Empty token is rejected so the
    executor never starts wide open by accident."""
    token = os.getenv("EXECUTOR_TOKEN", "").strip()
    if not token:
        token_file = os.getenv("EXECUTOR_TOKEN_FILE", "").strip()
        if token_file and Path(token_file).is_file():
            token = Path(token_file).read_text(encoding="utf-8").strip()
    return token


def parse_repos(spec: str) -> dict[str, Repo]:
    """Parse EXECUTOR_REPOS of the form ``name=path;name2=path2``.

    Only directories that exist and contain a ``.git`` entry are accepted, so a
    typo or a stale path fails closed instead of exposing an unintended folder.
    """
    repos: dict[str, Repo] = {}
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, raw_path = chunk.split("=", 1)
        name = name.strip()
        path = Path(raw_path.strip()).expanduser()
        if not name:
            continue
        if not path.is_dir() or not (path / ".git").exists():
            continue
        repos[name] = Repo(name=name, path=path.resolve())
    return repos


def load_repos() -> dict[str, Repo]:
    return parse_repos(os.getenv("EXECUTOR_REPOS", ""))
