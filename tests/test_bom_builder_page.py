"""bom-builder 页面结构断言：行内录入重构后的关键锚点与禁止项。"""
from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "bom-builder" / "index.html"


def read_page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_login_is_sso_only():
    html = read_page()
    assert "oidc/login?rd=" in html
    assert "/v1/auth/login" not in html
    assert "passwordInput" not in html
    assert "authModal" not in html


def test_unified_search_section_removed():
    html = read_page()
    assert 'id="searchScope"' not in html
    assert 'id="searchBtn"' not in html


def test_inline_adder_anchors_present():
    html = read_page()
    for anchor in ("adderInput", "adderResults", "childList", "totalsLine", "submitBtn"):
        assert f'id="{anchor}"' in html, anchor


def test_parent_card_new_mode_anchors_present():
    html = read_page()
    for anchor in (
        "parentCode", "parentName", "parentClassSelect", "parentUnitSelect",
        "parentModeBtn", "parentSearchInput", "parentResults",
    ):
        assert f'id="{anchor}"' in html, anchor


def test_mobile_and_autosave_essentials():
    html = read_page()
    assert 'inputmode="decimal"' in html
    assert "bom_builder_draft_v2" in html
    assert "物料清单" in html
    assert "Idempotency-Key" in html
