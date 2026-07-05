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
        self.assertIn("原配方", self.html)
        self.assertIn("当下成本", self.html)
        self.assertIn("模拟与结果", self.html)
        self.assertIn("resetLine(", self.html)

    def test_cost_table_includes_spec_column_and_clear_alignment_contract(self) -> None:
        self.assertIn('<col class="spec">', self.html)
        self.assertIn('<th class="g-recipe">子件名称</th>', self.html)
        self.assertIn('<th class="g-recipe">规格型号</th>', self.html)
        self.assertIn('colspan="8">原配方</th>', self.html)
        self.assertIn("line.spec?escapeHtml(line.spec):'—'", self.html)
        self.assertIn('sum-basic" colspan="4">汇总</td>', self.html)
        self.assertIn(".cost-table tbody td{text-align:center", self.html)
        self.assertIn(".cost-table td.text-left{text-align:left", self.html)

    def test_cost_reset_button_has_visible_button_border(self) -> None:
        self.assertIn("border:1px solid #dacdbd", self.html)
        self.assertIn("background:#fffdf8", self.html)

    def test_cost_inputs_use_restyled_fixed_width(self) -> None:
        self.assertIn(".cost-table{width:100%;min-width:1480px", self.html)
        self.assertIn(".price-input,.sim-qty-input{width:76px;max-width:76px;height:34px", self.html)

    def test_cost_source_shown_by_column_color_and_simulated_subtitle(self) -> None:
        # the colored top accent bars (src-line) are removed; source is conveyed by column color
        self.assertNotIn(".src-line", self.html)
        # 模拟分价 carries the "按当下价" subtitle with a small dot, per the mockup
        self.assertIn('模拟分价<span class="subtle-source">按当下价<span class="source-dot"></span></span>', self.html)
        self.assertIn(".cost-table .subtle-source{display:block", self.html)
        self.assertIn(".cost-table .source-dot{display:inline-block", self.html)

    def test_cost_spec_column_narrow_with_ellipsis_and_hover_title(self) -> None:
        self.assertIn(".cost-table col.spec{width:96px}", self.html)
        self.assertIn(".cost-table td.spec-cell>span{display:block;max-width:90px;overflow:hidden;text-overflow:ellipsis", self.html)
        self.assertIn('<td class="text-left spec-cell${line.spec?\'\':\' muted\'}" title="${escapeHtml(line.spec||\'\')}"><span>', self.html)

    def test_cost_table_restyle_module_borders_and_colored_results(self) -> None:
        # group/sub module-end colored separators
        self.assertIn(".cost-table .formula-end{border-right:2px solid", self.html)
        self.assertIn(".cost-table .price-end{border-right:2px solid", self.html)
        self.assertIn(".cost-table .cost-end{border-right:2px solid", self.html)
        # colored price/cost result text (mockup palette) + zero muting + footer sums
        self.assertIn(".cost-table td.sys-price,.cost-table td.sys-cost{color:#a76700}", self.html)
        self.assertIn(".cost-table td.cur-cost{color:#e63716", self.html)
        self.assertIn(".cost-table td.sim-cost{color:#4b45bf", self.html)
        self.assertIn(".cost-table td.zero{color:#8b94a3}", self.html)
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
        self.assertIn("const mainText=state.view.qty?formatQty(cell.qty):ratio(cell.ratio);", self.html)
        self.assertIn('<span class="ratio-main">${mainText}</span>', self.html)
        self.assertIn('<div class="qty-line">${ratio(cell.ratio)}</div>', self.html)

    def test_compare_cell_bar_is_flush_to_cell_edges(self) -> None:
        self.assertIn(".barLine{position:absolute;left:0;right:0;bottom:0;height:4px;margin:0", self.html)
        self.assertIn("border-radius:0;overflow:hidden}.barLine i{display:block;height:100%;border-radius:0", self.html)
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
        self.assertIn('type="checkbox" name="targetVersion"', self.html)
        self.assertIn("设为目标", self.html)
        self.assertIn("input[name=\"targetVersion\"]", self.html)
        self.assertIn("targetVersions()", self.html)
        self.assertIn("state.targetKeys.add(key)", self.html)
        self.assertIn("state.targetKeys.delete(key)", self.html)
        self.assertNotIn("state.targetKey=event.target.dataset.key", self.html)

    def test_multi_target_compare_uses_single_base(self) -> None:
        self.assertIn("targetVersions().some((target)=>target.key===version.key)", self.html)
        self.assertIn("function normalizeTargets()", self.html)
        self.assertIn("function rowStatusForTarget(row,rows,target)", self.html)
        self.assertIn("const priority=['replace','add','del','change','same','history'];", self.html)
        self.assertIn("targets.map((target)=>targetLabel(target)).join('、')", self.html)

    def test_version_card_set_row_is_compact_single_row_without_max_ratio(self) -> None:
        self.assertIn('<div class="set-row">', self.html)
        self.assertIn("设为基准", self.html)
        self.assertNotIn("设为基准版本", self.html)
        self.assertNotIn("设为目标版本", self.html)
        self.assertNotIn("最大占比", self.html)

    def test_spec_column_toggled_via_view_menu(self) -> None:
        self.assertIn('id="viewOptions"', self.html)
        self.assertIn('data-view="spec"', self.html)
        self.assertIn("${state.view.spec?'<th class=\"col-spec\">规格型号</th>':''}", self.html)
        self.assertNotIn("toggleSpecBtn", self.html)

    def test_toolbar_grouped_into_hover_dropdowns(self) -> None:
        # 快速选择/下载/视图 三个悬浮下拉；筛选 pill 保持外露
        self.assertIn(".dropdown:hover .drop-menu,.dropdown.open .drop-menu{display:block}", self.html)
        for mode in ("all", "none", "invert", "default", "activeDefault"):
            self.assertIn(f'data-quick-select="{mode}"', self.html)
        self.assertIn("全不选", self.html)
        self.assertIn("反选", self.html)
        self.assertIn("只选默认BOM", self.html)
        self.assertIn("function applyQuickSelect(", self.html)
        self.assertIn('id="compareFilters"', self.html)

    def test_download_menu_has_human_sheet_option(self) -> None:
        self.assertIn("只下载「配方表_人眼版」", self.html)
        self.assertIn('id="downloadHumanBtn"', self.html)
        self.assertIn("?sheet=human", self.html)
        self.assertIn("配方查询_人眼版_${state.fileId}.xlsx", self.html)

    def test_view_options_persist_with_qty_pct_guard(self) -> None:
        self.assertIn("const VIEW_KEY='formula_display_options';", self.html)
        self.assertIn("const VIEW_DEFAULTS={spec:true,qty:true,pct:true,arrow:true,delta:true,newTag:true,bar:true};", self.html)
        self.assertIn("if(!state.view.qty&&!state.view.pct){", self.html)
        self.assertIn("至少保留一项", self.html)
        for key in ("spec", "qty", "pct", "arrow", "delta", "newTag", "bar"):
            self.assertIn(f'data-view="{key}"', self.html)

    def test_arrow_and_delta_are_independent_toggles(self) -> None:
        # 箭头(↑↓)与±基准数值拆成两个开关；旧 localStorage 的 arrow 值迁移到 delta
        self.assertIn("显示↑↓箭头", self.html)
        self.assertIn("显示±基准", self.html)
        self.assertIn("state.view.arrow?(up?'↑':'↓'):''", self.html)
        self.assertIn("if('arrow' in saved&&!('delta' in saved))saved.delta=saved.arrow;", self.html)

    def test_compare_excel_export_via_backend_xlsx(self) -> None:
        # 对比表导出改为后端真 xlsx（伪xls在Excel/WPS只认内联、移动端连内联都丢，2026-07-05 拍板）
        self.assertIn("/v1/recipes/compare/export", self.html)
        self.assertIn("function buildComparePayload(", self.html)
        self.assertIn("const EXPORT_ST_ORDER={replace:0,add:1,del:2,change:3,same:4,history:5};", self.html)
        self.assertIn("filter_label:", self.html)
        self.assertIn("view:state.view,", self.html)
        self.assertIn("code_warn:isSpecialItemCode(row.itemCode),", self.html)
        self.assertNotIn("downloadHtmlAsXls", self.html)
        self.assertNotIn("mso-data-placement", self.html)
        self.assertNotIn("application/vnd.ms-excel", self.html)
        self.assertNotIn("barBg", self.html)

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

    def test_compare_blank_cells_and_unchanged_flags_render_empty(self) -> None:
        self.assertIn("if(!cell)return '<td class=\"version-col\"></td>';", self.html)
        self.assertIn("let flag='';", self.html)
        self.assertNotIn('<span class="empty">—</span>', self.html)
        self.assertNotIn('let flag=\'<span class="flag neutral">—</span>\';', self.html)

    def test_single_recipe_still_renders_compare_detail(self) -> None:
        self.assertNotIn("请至少选择 2 个配方参与对比", self.html)
        self.assertNotIn("if(selected.length<2)", self.html)

    def test_raw_and_compare_downloads_both_use_server_workbooks(self) -> None:
        self.assertIn("function downloadRawResult()", self.html)
        self.assertIn("/v1/recipes/download/${state.fileId}", self.html)
        self.assertIn("配方比例对比表_${filenamePart(queryInput.value.trim())}.xlsx", self.html)


if __name__ == "__main__":
    unittest.main()
