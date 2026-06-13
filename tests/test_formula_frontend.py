from __future__ import annotations

import unittest
from pathlib import Path


FORMULA_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "formula" / "index.html"


class FormulaFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = FORMULA_PAGE.read_text(encoding="utf-8")

    def test_cost_panel_matches_reset_and_grouped_table_contract(self) -> None:
        self.assertIn("重置模拟数量", self.html)
        self.assertIn("重置当下价格", self.html)
        self.assertIn("基础信息", self.html)
        self.assertIn("原配方", self.html)
        self.assertIn("模拟调整", self.html)
        self.assertIn("价格信息", self.html)
        self.assertIn("成本结果", self.html)
        self.assertIn("resetLine(", self.html)

    def test_cost_table_includes_spec_column_and_clear_alignment_contract(self) -> None:
        self.assertIn('<col class="spec">', self.html)
        self.assertIn('<th class="g-basic">子件名称</th>', self.html)
        self.assertIn('<th class="g-basic">规格型号</th>', self.html)
        self.assertIn('colspan="4">基础信息</th>', self.html)
        self.assertIn("line.spec?escapeHtml(line.spec):'—'", self.html)
        self.assertIn('sum-basic basic-end" colspan="4">汇总</td>', self.html)
        self.assertIn(".cost-table tbody td{text-align:center", self.html)
        self.assertIn(".cost-table td.text-left{text-align:left", self.html)

    def test_cost_reset_button_has_visible_button_border(self) -> None:
        self.assertIn("border:1px solid #dacdbd", self.html)
        self.assertIn("background:#fffdf8", self.html)

    def test_cost_inputs_use_restyled_fixed_width(self) -> None:
        self.assertIn(".cost-table{width:100%;min-width:1480px", self.html)
        self.assertIn(".price-input,.sim-qty-input{width:76px;max-width:76px;height:34px", self.html)

    def test_cost_price_cost_subheaders_not_pushed_down_by_relative_offset(self) -> None:
        # the relative+top:54px combo that shifted the 5 price/cost headers down is removed
        self.assertNotIn(".src-line{position:relative}", self.html)
        # the 3px accent bars are kept (anchored to the sticky header cell)
        self.assertIn(".cost-table .src-line::before{content:\"\";position:absolute", self.html)

    def test_cost_spec_column_narrow_with_ellipsis_and_hover_title(self) -> None:
        self.assertIn(".cost-table col.spec{width:96px}", self.html)
        self.assertIn(".cost-table td.spec-cell>span{display:block;max-width:90px;overflow:hidden;text-overflow:ellipsis", self.html)
        self.assertIn('<td class="text-left spec-cell${line.spec?\'\':\' muted\'}" title="${escapeHtml(line.spec||\'\')}"><span>', self.html)

    def test_cost_table_restyle_module_borders_and_colored_results(self) -> None:
        # group/sub module-end colored separators
        self.assertIn(".cost-table .basic-end{border-right:2px solid", self.html)
        self.assertIn(".cost-table .cost-end{border-right:2px solid", self.html)
        # colored price/cost result text + zero muting + footer sums
        self.assertIn(".cost-table td.sys-cost{color:#a86a10}", self.html)
        self.assertIn(".cost-table td.cur-cost{color:#c84a1b}", self.html)
        self.assertIn(".cost-table td.sim-cost{color:#5b4ac9}", self.html)
        self.assertIn(".cost-table td.zero{color:#9ca3af}", self.html)
        self.assertIn('<tfoot id="costFoot"></tfoot>', self.html)
        self.assertIn(".cost-table .sum-cost{background:", self.html)

    def test_cost_export_uses_server_filename(self) -> None:
        self.assertIn("downloadNameFromDisposition", self.html)
        self.assertIn("content-disposition", self.html)
        self.assertNotIn("recipe-cost-${new Date().toISOString().slice(0,10)}.xlsx", self.html)

    def test_current_price_time_is_input_hover_tooltip(self) -> None:
        self.assertIn('class="price-cell"${priceTooltip}', self.html)
        self.assertIn('data-tooltip="更新时间：${fmtPriceTime(t)}"', self.html)
        self.assertIn(".price-cell[data-tooltip]:hover::after", self.html)

    def test_bom_scope_defaults_to_default_bom_and_lists_all_versions_last(self) -> None:
        select_start = self.html.index('<select id="defaultBom">')
        select_end = self.html.index("</select>", select_start)
        select_html = self.html[select_start:select_end]

        self.assertIn('<option value="1" selected>默认 BOM</option>', select_html)
        self.assertLess(select_html.index('value="1"'), select_html.index('value="disabled"'))
        self.assertLess(select_html.index('value="enabled"'), select_html.index('value="all"'))

    def test_query_results_render_compare_panel_and_keep_raw_details(self) -> None:
        self.assertIn('id="comparePanel"', self.html)
        self.assertIn('id="versionGrid"', self.html)
        self.assertIn('id="compareTable"', self.html)
        self.assertIn("查看原始查询结果明细", self.html)
        self.assertIn("下载原始明细", self.html)
        self.assertIn("下载对比表Excel", self.html)
        self.assertIn("匹配 ${data.match_count||0} 条明细，配方 ${data.recipe_count||0} 个", self.html)
        self.assertIn("来源：${data.source_file||'unknown'}", self.html)

    def test_query_compare_logic_has_bars_filters_and_code_warnings(self) -> None:
        self.assertIn("function refreshMajorityPatterns(", self.html)
        self.assertIn("function isSpecialItemCode(", self.html)
        self.assertIn("function buildCompareMatrix(", self.html)
        self.assertIn("function renderCompareTable(", self.html)
        self.assertIn("function downloadCompareResult(", self.html)
        self.assertIn("codeWarn", self.html)
        self.assertIn("barLine", self.html)
        self.assertIn("仅差异", self.html)
        self.assertIn("仅替换", self.html)

    def test_compare_card_checkbox_radio_not_stretched_by_global_input_rule(self) -> None:
        self.assertIn('input[type="checkbox"],input[type="radio"]{width:16px', self.html)

    def test_compare_cell_shows_qty_primary_without_unit_and_ratio_secondary(self) -> None:
        self.assertIn('<span class="ratio-main">${formatQty(cell.qty)}</span>', self.html)
        self.assertIn('<div class="qty-line">${ratio(cell.ratio)}</div>', self.html)

    def test_compare_cell_bar_is_rounded_near_bottom(self) -> None:
        self.assertIn(".barLine{position:absolute;left:6px;right:6px;bottom:3px;height:4px;margin:0", self.html)
        self.assertIn("border-radius:999px;overflow:hidden}.barLine i{display:block;height:100%;border-radius:999px", self.html)
        self.assertIn(".cell-value{position:relative;text-align:center}", self.html)

    def test_compare_cell_quantity_heat_fill_removed(self) -> None:
        self.assertNotIn("--heat", self.html)
        self.assertNotIn("rgba(159,116,62,var(--heat))", self.html)

    def test_base_target_columns_have_no_cell_outline_or_fill(self) -> None:
        self.assertNotIn(".cell-value.current{box-shadow", self.html)
        self.assertNotIn(".cell-value.base{outline", self.html)

    def test_version_columns_use_consistent_border_no_gap(self) -> None:
        self.assertIn(".version-col{width:155px;text-align:center}", self.html)
        self.assertNotIn("border-left:8px solid #f0e9db", self.html)

    def test_target_version_is_switchable_like_base(self) -> None:
        self.assertIn('name="targetVersion"', self.html)
        self.assertIn("设为目标", self.html)
        self.assertIn("input[name=\"targetVersion\"]", self.html)
        self.assertIn("state.targetKey=event.target.dataset.key", self.html)

    def test_version_card_set_row_is_compact_single_row_without_max_ratio(self) -> None:
        self.assertIn('<div class="set-row">', self.html)
        self.assertIn("设为基准", self.html)
        self.assertNotIn("设为基准版本", self.html)
        self.assertNotIn("设为目标版本", self.html)
        self.assertNotIn("最大占比", self.html)

    def test_spec_column_can_be_toggled(self) -> None:
        self.assertIn('id="toggleSpecBtn"', self.html)
        self.assertIn("showSpec:true", self.html)
        self.assertIn("${state.showSpec?'<th class=\"col-spec\">规格型号</th>':''}", self.html)
        self.assertIn("state.showSpec=!state.showSpec", self.html)

    def test_version_card_shows_parent_name_on_top_without_label(self) -> None:
        self.assertIn('<div class="v-name" title="${escapeHtml(version.parentName)}">', self.html)
        self.assertNotIn('<div class="m-key">父件名称</div>', self.html)

    def test_default_bom_tag_moved_into_status_row(self) -> None:
        # 默认BOM no longer sits in the code line; it is appended to the 状态 row
        self.assertIn("${disabled?'停用':'启用'}${version.defaultBOM==='1'?'<span class=\"tag default\" style=\"margin-left:6px\">默认BOM</span>':''}", self.html)

    def test_compare_table_status_column_is_last_and_name_auto_width(self) -> None:
        head_status = self.html.index('<th class="col-status">状态</th>')
        head_code = self.html.index('<th class="col-code">子件编码</th>')
        self.assertGreater(head_status, head_code)
        self.assertIn(".col-name{width:1%;white-space:nowrap", self.html)

    def test_compare_code_and_unit_columns_auto_width(self) -> None:
        self.assertIn(".col-code{width:1%;white-space:nowrap", self.html)
        self.assertIn(".col-unit{width:1%;white-space:nowrap}", self.html)
        self.assertIn(".col-spec{width:1%;white-space:nowrap", self.html)

    def test_compare_header_shows_parent_name_with_version(self) -> None:
        self.assertIn('<div title="${escapeHtml(version.parentName)}">${escapeHtml(version.parentName)}</div>', self.html)
        self.assertIn("${escapeHtml(shortVersion(version.version))}", self.html)

    def test_compare_header_tags_sit_above_name_and_are_subtle(self) -> None:
        # tags row precedes the parent-name div within each version header cell
        tags_row = self.html.index('<th class="version-col"><div class="vh-tags">')
        name_div = self.html.index('<div title="${escapeHtml(version.parentName)}">', tags_row)
        self.assertLess(tags_row, name_div)
        # status (停用) added to header; long "当前目标" shortened to "目标"
        self.assertIn('<span class="vh-tag off">停用</span>', self.html)
        self.assertIn('<span class="vh-tag target">目标</span>', self.html)
        # subtle styling: small font, muted
        self.assertIn(".vh-tag{display:inline-flex;align-items:center;height:16px;padding:0 6px;border-radius:999px;font-size:10px", self.html)

    def test_base_badge_only_in_header_not_in_each_cell(self) -> None:
        self.assertIn("if(base&&version.key===base.key)flag='';", self.html)
        self.assertNotIn('<span class="flag base">基准</span>', self.html)
        self.assertIn('<span class="vh-tag base">基准</span>', self.html)

    def test_raw_download_still_uses_server_workbook_and_compare_download_is_client_table(self) -> None:
        self.assertIn("function downloadRawResult()", self.html)
        self.assertIn("/v1/recipes/download/${state.fileId}", self.html)
        self.assertIn("function downloadHtmlAsXls(", self.html)
        self.assertIn("对比的配方_比例对比表", self.html)
        self.assertIn("application/vnd.ms-excel", self.html)


if __name__ == "__main__":
    unittest.main()
