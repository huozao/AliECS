from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COUPLE_PAGE = ROOT / "services" / "public-web" / "couple" / "index.html"
MAP_PAGE = ROOT / "services" / "public-web" / "map" / "index.html"


def test_couple_dashboard_keeps_map_and_album_in_app():
    html = COUPLE_PAGE.read_text(encoding="utf-8")

    assert "https://adventure.hydwang.xyz" not in html
    assert "data-adventure-label" not in html
    assert 'href="/map/"' in html
    # ⚠️ 这里原本断言的是导航卡片 href="#gallery"。6686dec 的回忆工作台改版把那张卡片去掉了，
    # 但相册本身仍在站内（<section id="gallery">）——这条用例要守的是「相册不跳外链」，
    # 守的不是某个跳转锚点，所以断言改到区块本身。2026-09-07 改，此前 main 一直红。
    assert 'id="gallery"' in html
    assert "/v1/photos?page=1&page_size=30" in html


def test_map_page_uses_in_app_leaflet_memories():
    html = MAP_PAGE.read_text(encoding="utf-8")

    assert "https://adventure.hydwang.xyz" not in html
    assert "http-equiv=\"refresh\"" not in html
    assert "L.map(" in html
    assert "/v1/map/memories" in html
    assert "/memories/detail.html?id=" in html
