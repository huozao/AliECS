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


def test_code_suggestion_and_duplicate_check_wiring():
    html = read_page()
    assert "/v1/tplus/inventory-code-suggestion" in html
    assert "include_disabled=true" in html
    assert 'id="parentCodeHint"' in html and 'id="parentCodeWarn"' in html
    assert 'id="customCodeHint"' in html and 'id="customCodeWarn"' in html
    assert "编码已存在" in html
    assert "dupState" in html
    assert "live_checked" in html


def test_parent_only_submit_allowed_and_guarded():
    html = read_page()
    # 无子件可直接提交（仅创建父件），不再强制至少一个子件
    assert "请至少添加一个子件" not in html
    assert "仅创建父件（不创建 BOM）" in html
    # 已选 T+ 已有父件且无子件时没有可写入内容，给出明确提示
    assert "已选 T+ 已有父件且未添加子件" in html


def test_version_defaults_to_yymmdd_today():
    html = read_page()
    # 版本号默认当日日期（如 260713），不再是固定 V1
    assert "function todayVersion()" in html
    assert 'value="V1"' not in html
    assert "form.version||todayVersion()" in html


def test_entry_audit_tabs_present():
    html = read_page()
    for anchor in ("tabEntry", "tabAudit", "entryTab", "auditTab"):
        assert f'id="{anchor}"' in html, anchor
    assert "function switchTab(" in html
    assert "录入" in html and "审核" in html


def test_audit_list_wiring():
    html = read_page()
    assert "/v1/tplus/bom-pending" in html
    assert "/v1/tplus/bom-audit" in html
    assert 'id="auditList"' in html
    assert 'id="refreshAuditBtn"' in html
    assert "function loadPending(" in html
    assert "function auditRow(" in html
    # 只保留「本工具建的」实时未审列表（T+ bom/Query 不支持列表形态，故砍掉「全部未审」）
    assert "本工具提交的未审" in html
    assert "T+ 全部未审" not in html
    assert "当前没有待审核" in html


def test_audit_row_confirm_and_recheck_semantics():
    html = read_page()
    assert "确认审核" in html
    assert "data.audited" in html
    assert "switchTab('audit')" in html


def test_desktop_two_column_layout_and_inventory_events():
    html = read_page()
    assert 'class="layout"' in html
    assert "min-width:901px" in html
    assert "inventory_created" in html
    assert "已在 T+ 创建存货" in html


def test_custom_material_attribute_defaults_purchase_and_material_only():
    html = read_page()
    custom_box = html.split('id="customAttrBox"')[1].split("</div></div>")[0]
    assert 'data-attr="is_purchase" checked' in custom_box
    assert 'data-attr="is_material" checked' in custom_box
    for attr in ("is_sale", "is_made_self", "is_made_request", "is_phantom"):
        assert f'data-attr="{attr}" checked' not in custom_box, attr
