from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPLUS_ROOT = ROOT / "services" / "tplus-sync-worker"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "tplus_price"
for item in (str(TPLUS_ROOT), str(TPLUS_ROOT / "src"), str(FIXTURE_DIR)):
    if item not in sys.path:
        sys.path.insert(0, item)

from config.settings import Settings
from expected_columns import EXPECTED_PURCHASE_COLUMNS, EXPECTED_SALES_COLUMNS
from tplus_datahub.modules.purchase_price.export_purchase_price import export_purchase_price
from tplus_datahub.modules.purchase_price.sync_purchase_price import sync_purchase_price
from tplus_datahub.modules.purchase_price.transform_purchase_price import transform_purchase_price_rows
from tplus_datahub.modules.sales_price.export_sales_price import export_sales_price
from tplus_datahub.modules.sales_price.sync_sales_price import sync_sales_price
from tplus_datahub.modules.sales_price.transform_sales_price import transform_sales_price_rows

REPORT_ENDPOINT = "/tplus/api/v2/reportQuery/GetReportData"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://example.invalid",
        app_key="app-key",
        app_secret="app-secret",
        open_token="open-token",
        default_page_size=500,
        timeout_connect=1,
        timeout_read=1,
        output_dir=str(tmp_path / "output"),
        data_dir=str(tmp_path / "data"),
    )


class FakeReportClient:
    """单页 GetReportData 客户端：返回固定响应，记录请求体供断言。"""

    def __init__(self, response: dict):
        self.response = response
        self.posts: list[tuple[str, dict]] = []

    def post(self, endpoint: str, payload: dict | None = None) -> dict:
        body = dict(payload or {})
        self.posts.append((endpoint, body))
        assert endpoint == REPORT_ENDPOINT, endpoint
        return self.response


def test_transform_purchase_price_rows() -> None:
    rows = transform_purchase_price_rows([load_fixture("purchase_report_sample.json")])

    assert len(rows) == 1  # 合计行(rowType!=D)被过滤
    row = rows[0]
    assert list(row.keys()) == EXPECTED_PURCHASE_COLUMNS
    assert row["单据编号"].startswith("PS-")
    assert row["供应商编码"] == "SUP001"
    assert row["仓库"] == "原材库"
    assert row["存货编码"] == "RM-001"
    assert row["折扣%"] == 100  # 1.0000 -> 100
    assert row["数量"] == 34000
    assert row["含税单价"] == pytest.approx(16.2)
    assert row["单价"] == pytest.approx(14.34)
    assert row["报价"] is None  # 空字符串 -> None


def test_transform_sales_price_rows() -> None:
    rows = transform_sales_price_rows([load_fixture("sales_report_sample.json")])

    assert len(rows) == 1
    row = rows[0]
    assert list(row.keys()) == EXPECTED_SALES_COLUMNS
    assert row["单据编号"].startswith("SA-")
    assert row["客户"] == "脱敏客户"
    assert row["存货编码"] == "FG-001"
    assert row["折扣%"] == 100
    assert row["含税单价"] == pytest.approx(32.0)
    assert row["单价"] == pytest.approx(28.3)


def test_sync_purchase_price_uses_report_query(tmp_path: Path) -> None:
    client = FakeReportClient(load_fixture("purchase_report_sample.json"))

    rows = sync_purchase_price(
        settings=make_settings(tmp_path),
        client=client,
        timestamp="20260614_120000",
        begin_date="2026-05-01",
        end_date="2026-05-31",
    )

    assert rows[0]["单据编号"] == "PS-2026-001"
    endpoint, body = client.posts[0]
    assert endpoint == REPORT_ENDPOINT
    request = body["request"]
    assert request["ReportName"] == "PU_PurchaseArrivalDetailRpt"
    assert request["SearchItems"][0]["BeginDefault"] == "2026-05-01"
    assert request["SearchItems"][0]["EndDefault"] == "2026-05-31"
    assert "partnerCode" in request["ReportTableColNames"]


def test_sync_sales_price_uses_report_query(tmp_path: Path) -> None:
    client = FakeReportClient(load_fixture("sales_report_sample.json"))

    rows = sync_sales_price(
        settings=make_settings(tmp_path),
        client=client,
        timestamp="20260614_120000",
        begin_date="2026-05-01",
        end_date="2026-05-31",
    )

    assert rows[0]["单据编号"] == "SA-2026-001"
    endpoint, body = client.posts[0]
    assert endpoint == REPORT_ENDPOINT
    assert body["request"]["ReportName"] == "SA_SaleDeliveryDetailRpt"


def test_export_price_rows_to_catalog_scanned_xlsx(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    purchase_path = export_purchase_price(
        transform_purchase_price_rows([load_fixture("purchase_report_sample.json")]),
        settings=settings,
        timestamp="20260614_120000",
    )
    sales_path = export_sales_price(
        transform_sales_price_rows([load_fixture("sales_report_sample.json")]),
        settings=settings,
        timestamp="20260614_120000",
    )

    assert purchase_path.name == "purchase_price_20260614_120000.xlsx"
    assert sales_path.name == "sales_price_20260614_120000.xlsx"
    assert purchase_path.exists()
    assert sales_path.exists()
