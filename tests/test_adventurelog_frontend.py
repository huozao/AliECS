from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COUPLE_PAGE = ROOT / "services" / "public-web" / "couple" / "index.html"
MAP_PAGE = ROOT / "services" / "public-web" / "map" / "index.html"


def test_couple_dashboard_links_map_and_album_to_adventurelog():
    html = COUPLE_PAGE.read_text(encoding="utf-8")

    assert "https://adventure.hydwang.xyz" in html
    assert "window.open('https://adventure.hydwang.xyz','_blank','noopener')" in html
    assert "前往 旅行足迹/相册（AdventureLog）" in html


def test_map_page_is_retired_with_adventurelog_redirect():
    html = MAP_PAGE.read_text(encoding="utf-8")

    assert "地图足迹已迁移至 AdventureLog" in html
    assert "setTimeout(()=>location.href='https://adventure.hydwang.xyz',1500)" in html
    assert "L.map(" not in html
    assert "/v1/map/memories" not in html
