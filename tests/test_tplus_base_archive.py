from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPLUS_ROOT = ROOT / "services" / "tplus-sync-worker"
for item in (str(TPLUS_ROOT), str(TPLUS_ROOT / "src")):
    if item not in sys.path:
        sys.path.insert(0, item)

from config.settings import Settings
from tplus_datahub.modules.base_archive.sync_base_archive import sync_base_archive

# 模拟 T+ /Query 真实行为：显式 PageSize 完全生效；不传 PageSize 时服务端按默认上限截断。
SERVER_DEFAULT_CAP = 500


def make_settings(tmp_path: Path, page_size: int) -> Settings:
    return Settings(
        base_url="https://example.invalid",
        app_key="k",
        app_secret="s",
        open_token="t",
        default_page_size=page_size,
        timeout_connect=1,
        timeout_read=1,
        output_dir=str(tmp_path / "output"),
        data_dir=str(tmp_path / "data"),
    )


class PagedQueryClient:
    def __init__(self, total: int):
        self.rows = [{"ID": str(i), "Code": f"C{i}"} for i in range(total)]
        self.posts: list[dict] = []

    def post(self, endpoint: str, payload: dict | None = None) -> dict:
        param = dict((payload or {}).get("param", {}))
        self.posts.append(param)
        page_index = int(param.get("PageIndex", 1))
        if "PageSize" in param:
            page_size = int(param["PageSize"])
        else:
            page_size = SERVER_DEFAULT_CAP  # 不传 PageSize -> 服务端默认上限
        start = (page_index - 1) * page_size
        return {"Rows": self.rows[start : start + page_size]}


def test_sync_base_archive_returns_all_rows_across_pages(tmp_path: Path) -> None:
    client = PagedQueryClient(total=1203)  # 远超单页，必须翻页才能全量

    rows = sync_base_archive(
        module_name="current_stock",
        endpoint="/tplus/api/v2/currentStock/Query",
        settings=make_settings(tmp_path, page_size=400),
        client=client,
        timestamp="20260614_120000",
    )

    assert len(rows) == 1203  # 全量，未被单页/服务端默认上限截断
    assert {r["ID"] for r in rows} == {str(i) for i in range(1203)}
    assert [p.get("PageIndex") for p in client.posts] == [1, 2, 3, 4]
    assert all(p.get("PageSize") == 400 for p in client.posts)
