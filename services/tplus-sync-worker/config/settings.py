from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only used before dependencies are installed
    def load_dotenv(dotenv_path: str | Path | None = None, override: bool = False) -> bool:
        path = Path(dotenv_path or ".env")
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            if override or key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
        return True


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    app_key: str
    app_secret: str
    open_token: str
    default_page_size: int
    timeout_connect: int
    timeout_read: int
    output_dir: str
    data_dir: str
    # BOM 单独一档：bom/QueryPage 每行要展开整棵子件树，耗时随 PageSize 超线性增长
    # （2026-08-09 实测 500→38.5s 超过读超时，100→14.6s，50→约 6s）。
    # 其余模块是扁平档案，继续用 default_page_size=500 才快。
    bom_page_size: int = 50

    @property
    def timeout(self) -> tuple[int, int]:
        return (self.timeout_connect, self.timeout_read)

    @property
    def output_root(self) -> Path:
        return Path(self.output_dir)

    @property
    def data_root(self) -> Path:
        return Path(self.data_dir)


def _read_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值无法解析") from exc


def load_settings(env_file: str | Path = ".env", validate: bool = True) -> Settings:
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)

    settings = Settings(
        base_url=os.getenv("CHANJET_BASE_URL", "https://openapi.chanjet.com").rstrip("/"),
        app_key=os.getenv("CHANJET_APP_KEY", ""),
        app_secret=os.getenv("CHANJET_APP_SECRET", ""),
        open_token=os.getenv("CHANJET_OPEN_TOKEN", ""),
        default_page_size=_read_int("DEFAULT_PAGE_SIZE", 500),
        timeout_connect=_read_int("REQUEST_TIMEOUT_CONNECT", 5),
        timeout_read=_read_int("REQUEST_TIMEOUT_READ", 30),
        output_dir=os.getenv("OUTPUT_DIR", "output"),
        data_dir=os.getenv("DATA_DIR", "data"),
        bom_page_size=_read_int("TPLUS_BOM_PAGE_SIZE", 50),
    )

    if validate:
        missing = [
            name
            for name, value in (
                ("CHANJET_APP_KEY", settings.app_key),
                ("CHANJET_APP_SECRET", settings.app_secret),
                ("CHANJET_OPEN_TOKEN", settings.open_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"缺少必要配置：{', '.join(missing)}。请在 .env 中配置。")

    return settings
