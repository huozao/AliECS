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
    assert 'href="#gallery"' in html
    assert "/v1/photos?page=1&page_size=30" in html


def test_map_page_uses_in_app_leaflet_memories():
    html = MAP_PAGE.read_text(encoding="utf-8")

    assert "https://adventure.hydwang.xyz" not in html
    assert "http-equiv=\"refresh\"" not in html
    assert "L.map(" in html
    assert "/v1/map/memories" in html
    assert "/memories/detail.html?id=" in html
