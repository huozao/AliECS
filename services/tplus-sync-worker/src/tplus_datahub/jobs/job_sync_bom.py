from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import ConfigError, load_settings
from tplus_datahub.core.exceptions import ChanjetAPIError, TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp, text_preview
from tplus_datahub.jobs.sync_state import upsert_and_snapshot_full_bom
from tplus_datahub.modules.bom.export_bom import export_bom
from tplus_datahub.modules.bom.sync_bom import sync_bom


def build_query_params_from_target(target: dict | None) -> dict[str, str]:
    if not target:
        return {}
    params: dict[str, str] = {}
    parent_code = target.get("parent_code") or target.get("Code") or target.get("code")
    version = target.get("version") or target.get("Version")
    if parent_code:
        params["Code"] = str(parent_code)
    if version:
        params["Version"] = str(version)
    return params


@dataclass
class SyncBomResult:
    exit_code: int
    export_files: list[str] = field(default_factory=list)


def run(target: dict | None = None, mode: str = "full_bom") -> SyncBomResult:
    logger = get_logger("tplus_datahub.job_sync_bom", "output/logs/job_sync_bom.log")
    timestamp = now_timestamp()
    try:
        settings = load_settings()
        query_params = build_query_params_from_target(target)
        if mode == "incremental" and query_params:
            rows = sync_bom(settings=settings, timestamp=timestamp, query_params=query_params, include_disabled=True)
        else:
            rows = sync_bom(settings=settings, timestamp=timestamp)
        full_rows = upsert_and_snapshot_full_bom(rows, mode=mode, source_json={"target": target or {}})
        excel_path = export_bom(full_rows, settings=settings, timestamp=timestamp)
        logger.info("Excel 已导出(全量 %s 行)：%s", len(full_rows), excel_path)
        return SyncBomResult(exit_code=0, export_files=[excel_path.name])
    except ConfigError as exc:
        logger.error("配置错误：%s", exc); return SyncBomResult(2)
    except ChanjetAPIError as exc:
        logger.error("接口错误：endpoint=%s status=%s body=%s", exc.endpoint, exc.status_code, text_preview(exc.body_preview)); return SyncBomResult(3)
    except TPlusDataHubError as exc:
        logger.error("同步失败：%s", exc); return SyncBomResult(4)
    except Exception as exc:
        logger.exception("未知异常：%s", exc); return SyncBomResult(1)


def main(target: dict | None = None, mode: str = "full_bom") -> int:
    return run(target=target, mode=mode).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
