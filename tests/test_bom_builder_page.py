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


def test_children_render_as_table_without_more_section():
    html = read_page()
    assert 'id="childTableWrap"' in html
    for header in ("编码", "名称", "规格型号", "单位", "数量", "可用"):
        assert f"<th>{header}</th>" in html or f'<th class="num">{header}</th>' in html, header
    # 「更多（预出仓库/子BOM版本）」已删：生产数据 2032/2034 子件两字段皆空，留空由 T+ 解析
    assert "更多（预出仓库" not in html
    assert "预出仓库编码" not in html.split("<script>")[1]


def test_parent_new_mode_grouped_with_attributes():
    html = read_page()
    # 三分组标题
    for title in ("基本信息", "计量单位", "存货属性"):
        assert title in html, title
    assert 'id="parentAttrBox"' in html
    assert 'id="customAttrBox"' in html
    # 6 个属性复选框锚点
    for attr in ("is_purchase", "is_sale", "is_made_self", "is_material", "is_made_request", "is_phantom"):
        assert f'data-attr="{attr}"' in html, attr


def test_parent_attribute_defaults_five_checked_phantom_off():
    html = read_page()
    parent_box = html.split('id="parentAttrBox"')[1].split("</details>")[0]
    for attr in ("is_purchase", "is_sale", "is_made_self", "is_material", "is_made_request"):
        assert f'data-attr="{attr}" checked' in parent_box, attr
    assert 'data-attr="is_phantom" checked' not in parent_box


def test_custom_material_attribute_defaults_purchase_and_material_only():
    html = read_page()
    custom_box = html.split('id="customAttrBox"')[1].split("</div></div>")[0]
    assert 'data-attr="is_purchase" checked' in custom_box
    assert 'data-attr="is_material" checked' in custom_box
    for attr in ("is_sale", "is_made_self", "is_made_request", "is_phantom"):
        assert f'data-attr="{attr}" checked' not in custom_box, attr
