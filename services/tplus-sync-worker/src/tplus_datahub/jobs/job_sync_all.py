from __future__ import annotations

from config.endpoints import VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS, VERIFIED_VOUCHER_LIST_ENDPOINTS
from config.settings import ConfigError, load_settings
from tplus_datahub.core.exceptions import ChanjetAPIError, TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp, text_preview
from tplus_datahub.jobs.sync_state import persist_inventory_records, upsert_and_snapshot_full_bom
from tplus_datahub.modules.base_archive.export_base_archive import export_base_archive
from tplus_datahub.modules.base_archive.sync_base_archive import sync_base_archive
from tplus_datahub.modules.bom.export_bom import export_bom
from tplus_datahub.modules.bom.sync_bom import sync_bom
from tplus_datahub.modules.inventory.export_stock import export_inventory
from tplus_datahub.modules.inventory.sync_stock import sync_inventory
from tplus_datahub.modules.partner.export_partner import export_partner
from tplus_datahub.modules.partner.sync_partner import sync_partner
from tplus_datahub.modules.purchase_price.export_purchase_price import export_purchase_price
from tplus_datahub.modules.purchase_price.sync_purchase_price import sync_purchase_price
from tplus_datahub.modules.sales_price.export_sales_price import export_sales_price
from tplus_datahub.modules.sales_price.sync_sales_price import sync_sales_price
from tplus_datahub.modules.voucher.export_voucher_list import export_voucher_list
from tplus_datahub.modules.voucher.sync_voucher_list import sync_voucher_list

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SyncAllResult:
    exit_code: int
    export_files: list[str] = field(default_factory=list)
    diff_summary: dict | None = None
    full_snapshot_id: int | None = None
    failed_modules: list[str] = field(default_factory=list)


def _basename(path: object) -> str:
    return Path(str(path)).name


def _exit_code_for(exc: BaseException) -> int:
    if isinstance(exc, ConfigError):
        return 2
    if isinstance(exc, ChanjetAPIError):
        return 3
    if isinstance(exc, TPlusDataHubError):
        return 4
    return 1


def _log_stage_error(logger, module_name: str, exc: BaseException) -> None:
    """保留原有的三种错误日志措辞（runbook 和告警按它们检索），再补一行模块名。"""
    if isinstance(exc, ConfigError):
        logger.error("Config error: %s", exc)
    elif isinstance(exc, ChanjetAPIError):
        logger.error("API error: endpoint=%s status=%s body=%s", exc.endpoint, exc.status_code, text_preview(exc.body_preview))
    elif isinstance(exc, TPlusDataHubError):
        logger.error("Sync failed: %s", exc)
    else:
        logger.exception("Unexpected error: %s", exc)
    logger.error("Module failed, continuing with the rest: module=%s", module_name)


PENDING_MODULES = [
    "material",
    "product",
    "cost",
    "department",
    "person",
    "marketing_organ",
    "settle_style",
    "bank_account",
    "currency",
    "expense",
    "income",
    "sales",
    "purchase",
]


def run() -> SyncAllResult:
    """每个模块独立容错：一个模块挂掉只记账，后面的模块照跑。

    2026-08-07 18:00 生产实测过反例——BOM 接口超时让整轮 abort，存货档案跟着不落库，
    41 个纯存货父件在 /formula/colors/ 上被误判「编码失联」。存货是 BOM 之后的一步，
    却因为共用一个 try 被连坐。
    """
    logger = get_logger("tplus_datahub.job_sync_all", "output/logs/job_sync_all.log")
    timestamp = now_timestamp()
    exports: list[str] = []
    failures: list[tuple[str, int]] = []
    snap = None

    try:
        settings = load_settings()
    except ConfigError as exc:
        # 配置读不出来时每个模块都会同样失败，没必要逐个撞一遍。
        logger.error("Config error: %s", exc)
        return SyncAllResult(2, exports, failed_modules=["config"])

    def stage(module_name: str, action):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - 单模块失败不拖垮整轮全量
            _log_stage_error(logger, module_name, exc)
            failures.append((module_name, _exit_code_for(exc)))
            return None

    def _bom():
        nonlocal snap
        bom_rows = sync_bom(settings=settings, timestamp=timestamp)
        snap = upsert_and_snapshot_full_bom(bom_rows, mode="scheduled_full", source_json={"job": "job_sync_all"})
        bom_path = export_bom(snap.full_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(bom_path)); logger.info("BOM Excel exported: %s", bom_path)

    def _inventory():
        inventory_rows = sync_inventory(settings=settings, timestamp=timestamp)
        inventory_path = export_inventory(inventory_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(inventory_path)); logger.info("Inventory Excel exported: %s", inventory_path)
        persist_inventory_records(inventory_rows, mode="scheduled_full")

    def _partner():
        partner_rows = sync_partner(settings=settings, timestamp=timestamp)
        partner_path = export_partner(partner_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(partner_path)); logger.info("Partner Excel exported: %s", partner_path)

    def _archive(module_name: str, endpoint: str):
        archive_rows = sync_base_archive(module_name=module_name, endpoint=endpoint, settings=settings, timestamp=timestamp)
        archive_path = export_base_archive(module_name, archive_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(archive_path)); logger.info("%s Excel exported: %s", module_name, archive_path)

    def _voucher(module_name: str, config: dict):
        voucher_rows = sync_voucher_list(module_name=module_name, endpoint=config["endpoint"], select_fields=config["select_fields"], settings=settings, timestamp=timestamp)
        voucher_path = export_voucher_list(module_name, voucher_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(voucher_path)); logger.info("%s Excel exported: %s", module_name, voucher_path)

    def _purchase_price():
        purchase_price_rows = sync_purchase_price(settings=settings, timestamp=timestamp)
        purchase_price_path = export_purchase_price(purchase_price_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(purchase_price_path)); logger.info("purchase_price Excel exported: %s", purchase_price_path)

    def _sales_price():
        sales_price_rows = sync_sales_price(settings=settings, timestamp=timestamp)
        sales_price_path = export_sales_price(sales_price_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(sales_price_path)); logger.info("sales_price Excel exported: %s", sales_price_path)

    stage("bom", _bom)
    stage("inventory", _inventory)
    stage("partner", _partner)
    for module_name, endpoint in VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS.items():
        stage(module_name, lambda name=module_name, ep=endpoint: _archive(name, ep))
    for module_name, config in VERIFIED_VOUCHER_LIST_ENDPOINTS.items():
        stage(module_name, lambda name=module_name, cfg=config: _voucher(name, cfg))
    stage("purchase_price", _purchase_price)
    stage("sales_price", _sales_price)

    for module_name in PENDING_MODULES:
        logger.info("%s module endpoint is not confirmed; skipped", module_name)

    if failures:
        logger.error("T+ full sync finished with failed modules: %s", ", ".join(name for name, _ in failures))
    # 退出码取第一个失败模块的码，与拆分前「第一个异常决定退出码」的语义一致。
    exit_code = failures[0][1] if failures else 0
    return SyncAllResult(
        exit_code,
        exports,
        diff_summary=snap.diff_summary if snap else None,
        full_snapshot_id=snap.full_snapshot_id if snap else None,
        failed_modules=[name for name, _ in failures],
    )


def main() -> int:
    return run().exit_code


if __name__ == "__main__":
    raise SystemExit(main())
