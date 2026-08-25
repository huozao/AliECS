from __future__ import annotations

import unittest
from pathlib import Path


FORMULA_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "formula" / "index.html"
COMPARE_CORE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "formula" / "compare-core.js"
COST_CORE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "formula" / "cost-core.js"


class FormulaFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = FORMULA_PAGE.read_text(encoding="utf-8")
        # PR#185 把对比核心逻辑抽到 compare-core.js（页面薄壳化）；涉及算法的断言查两个文件的合集。
        self.core = COMPARE_CORE.read_text(encoding="utf-8")
        # PR#(本次) 把成本核算的纯计算抽到 cost-core.js（利润口径、对比矩阵、本地重算）。
        self.core_cost = COST_CORE.read_text(encoding="utf-8")
        self.bundle = self.html + self.core

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
        self.assertIn(":is(.cost-table,.cost-matrix-table) tbody td{text-align:center", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.text-left{text-align:left", self.html)

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
        self.assertIn(":is(.cost-table,.cost-matrix-table) .subtle-source{display:block", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) .source-dot{display:inline-block", self.html)

    def test_cost_spec_column_narrow_with_ellipsis_and_hover_title(self) -> None:
        self.assertIn(".cost-table col.spec{width:96px}", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.spec-cell>span{display:block;max-width:90px;overflow:hidden;text-overflow:ellipsis", self.html)
        self.assertIn('<td class="text-left spec-cell${line.spec?\'\':\' muted\'}" title="${escapeHtml(line.spec||\'\')}"><span>', self.html)

    def test_cost_table_restyle_module_borders_and_colored_results(self) -> None:
        # group/sub module-end colored separators
        self.assertIn(":is(.cost-table,.cost-matrix-table) .formula-end{border-right:2px solid", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) .price-end{border-right:2px solid", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) .cost-end{border-right:2px solid", self.html)
        # colored price/cost result text (mockup palette) + zero muting + footer sums
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.sys-price,:is(.cost-table,.cost-matrix-table) td.sys-cost{color:#a76700}", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.cur-cost{color:#e63716", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.sim-cost{color:#4b45bf", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.zero{color:#8b94a3}", self.html)
        self.assertIn('<tfoot id="costFoot"></tfoot>', self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) .sum-cost{background:", self.html)

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

    def test_query_silently_hides_unavailable_cost_feature(self) -> None:
        self.assertIn("if(cost==='denied')showSuccess('配方查询已完成。');", self.html)
        self.assertIn("error.status=response.status", self.html)
        self.assertIn("costError&&costError.status===403", self.html)
        self.assertNotIn("当前账号没有成本核算权限", self.html)

    def test_compare_table_scrolls_only_after_fifteen_rows(self) -> None:
        self.assertIn(".compare-table-wrap{", self.html)
        self.assertIn("max-height:988px", self.html)
        self.assertIn("const MAX_VISIBLE_COMPARE_ROWS=15;", self.html)
        self.assertIn("function syncCompareTableViewport()", self.html)
        self.assertIn("rowHeight*MAX_VISIBLE_COMPARE_ROWS", self.html)
        self.assertIn("syncCompareTableViewport();", self.html)

    def test_query_compare_logic_has_bars_filters_and_code_warnings(self) -> None:
        self.assertIn("majorityPatterns(", self.bundle)
        self.assertIn("function isSpecialItemCode(", self.core)
        self.assertIn("function buildCompareMatrix(", self.bundle)
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
        self.assertIn("targetVersions()", self.html)
        self.assertIn("targetVersions(", self.core)
        self.assertIn("function normalizeTargets()", self.html)
        self.assertIn("rowStatusForTarget(row, rows, target", self.core)
        self.assertIn("priority = ['replace', 'add', 'del', 'change', 'same', 'history']", self.core)
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
        self.assertIn("VIEW_DEFAULTS = { code: true, spec: true, qty: true, pct: true, arrow: true, delta: true, newTag: true, bar: true, insight: true }", self.core)
        self.assertIn("if(!state.view.qty&&!state.view.pct){", self.html)
        self.assertIn("至少保留一项", self.html)
        for key in ("code", "spec", "qty", "pct", "arrow", "delta", "newTag", "bar", "insight"):
            self.assertIn(f'data-view="{key}"', self.html)

    def test_item_code_column_is_toggleable_and_keeps_the_code_warning(self) -> None:
        # 编码列可隐藏；隐藏后编码异常的红「!」挪到子件名称列，告警不随列一起消失
        self.assertIn("显示子件编码", self.html)
        self.assertIn("""${state.view.code?'<th class="col-code">子件编码</th>':''}""", self.html)
        self.assertIn("""!state.view.code&&isSpecialItemCode(row.itemCode)?' codeWarn':''""", self.html)

    def test_child_reverse_lookup_confirms_candidates_before_querying(self) -> None:
        # 按子件查不是直接出配方：先罗列候选子件，用户勾选确认后才反查
        self.assertIn('id="queryMode"', self.html)
        self.assertIn('value="child"', self.html)
        self.assertIn('id="childPanel"', self.html)
        self.assertIn("'/v1/recipes/children/search'", self.html)
        self.assertIn('data-child-match="any"', self.html)
        self.assertIn('data-child-match="all"', self.html)
        self.assertIn("async function searchChildren(keyword)", self.html)
        self.assertIn("childLookupBtn.onclick=()=>runQuery({query:queryInput.value.trim(),child_codes:[...state.childSelected],child_match:state.childMatch})", self.html)
        # 候选过多要说清是被截断了，不能让用户以为就这么些
        self.assertIn("state.childTruncated", self.html)

    def test_query_scope_is_single_source_for_query_cost_and_exports(self) -> None:
        # 反查模式下输入框里是候选关键字而不是配方编码；三个入口各读一次输入框，child_codes 必漏
        self.assertIn("queryScope:{query:'',child_codes:[],child_match:'any'}", self.html)
        self.assertIn("return{query:scope.query,child_codes:scope.child_codes,child_match:scope.child_match,", self.html)
        self.assertIn("query:state.queryScope.query,activeFilter:state.activeFilter", self.html)
        self.assertIn("filenamePart(state.queryScope.query)", self.html)
        # 只剩表单提交、反查按钮两处直接读输入框（链接回填是赋值）
        self.assertEqual(2, self.html.count("queryInput.value.trim()"))

    def test_query_form_grid_has_a_column_for_every_field(self) -> None:
        # 加了「查询方式」后 grid 仍是 3 列 → 第 4 个元素被挤到第二行，查询按钮换行
        self.assertIn("grid-template-columns:200px minmax(200px,1fr) 150px auto", self.html)
        self.assertEqual(3, self.html.count("<label", self.html.index('id="queryForm"'), self.html.index("</form>")))

    def test_insight_sidebar_is_toggleable_from_view_menu(self) -> None:
        self.assertIn('data-view="insight"', self.html)
        self.assertIn("显示变化重点", self.html)
        self.assertIn(".compare-layout.no-side{grid-template-columns:minmax(0,1fr)}", self.html)
        self.assertIn("function syncInsightVisibility()", self.html)

    def test_compare_filter_is_a_dropdown_on_the_toolbar_row(self) -> None:
        # 一排 pills 换成下拉，和视图/排序同一行；工具栏改 flex 换行
        self.assertIn('id="compareFilterDrop"', self.html)
        self.assertIn('id="compareFilterLabel"', self.html)
        self.assertIn("function syncFilterUi()", self.html)
        self.assertIn(".compare-toolbar{display:flex;flex-wrap:wrap", self.html)
        self.assertNotIn('<div id="compareFilters" class="pills">', self.html)

    def test_column_sort_is_driven_from_a_row_and_shared_with_export(self) -> None:
        # 点某一子件行 → 按该行给配方列排序；列序只有 selectedVersions() 一个来源
        self.assertIn("function cycleColSort(itemCode)", self.html)
        self.assertIn("CompareCore.sortCompareColumns(selected,{rows:buildCompareMatrix(),...state.colSort})", self.html)
        self.assertIn("CompareCore.buildComparePayload({colSort:state.colSort,", self.html)
        self.assertIn('data-col-metric="ratio"', self.html)
        self.assertIn('data-col-metric="qty"', self.html)
        self.assertIn("data-col-clear", self.html)
        # 换查询后基准子件要清掉，否则新配方里找不到它，看起来像排序坏了
        self.assertIn("state.colSort={...state.colSort,itemCode:'',dir:'desc'};", self.html)

    def test_compare_mode_default_select_all_runs_once_not_every_render(self) -> None:
        # 放在每次 renderCost 里会把用户点的「全不选」当场填回去
        self.assertIn("if(compare&&!state.costSelectionReady){", self.html)
        self.assertNotIn("if(compare&&!state.costSelectedKeys.size)state.costSelectedKeys=new Set(", self.html)
        self.assertIn("costSelectionReady:false", self.html)
        # 点了按钮但集合没变化时，计数让「没反应」和「没变化」能区分开
        self.assertIn('id="costSelectedNote"', self.html)
        self.assertIn("已选 ${state.costSelectedKeys.size} / ${recipes.length} 本", self.html)

    def test_empty_reverse_lookup_explains_why_instead_of_a_blank_panel(self) -> None:
        self.assertIn("if(!data.recipe_count&&state.queryScope.child_codes.length){", self.html)
        self.assertIn("互为替代品", self.html)
        self.assertIn("切到「任一命中」试试", self.html)

    def test_arrow_and_delta_are_independent_toggles(self) -> None:
        # 箭头(↑↓)与±基准数值拆成两个开关；旧 localStorage 的 arrow 值迁移到 delta
        self.assertIn("显示↑↓箭头", self.html)
        self.assertIn("显示±基准", self.html)
        self.assertIn("state.view.arrow?(up?'↑':'↓'):''", self.html)
        self.assertIn("if('arrow' in saved&&!('delta' in saved))saved.delta=saved.arrow;", self.html)

    def test_compare_excel_export_via_backend_xlsx(self) -> None:
        # 对比表导出改为后端真 xlsx（伪xls在Excel/WPS只认内联、移动端连内联都丢，2026-07-05 拍板）
        self.assertIn("/v1/recipes/compare/export", self.html)
        self.assertIn("function buildComparePayload(", self.bundle)
        self.assertIn("EXPORT_ST_ORDER = { replace: 0, add: 1, del: 2, change: 3, same: 4, history: 5 }", self.core)
        self.assertIn("filter_label:", self.core)
        self.assertIn("view:state.view,", self.html)
        self.assertIn("code_warn: isSpecialItemCode(row.itemCode", self.core)
        self.assertNotIn("downloadHtmlAsXls", self.bundle)
        self.assertNotIn("mso-data-placement", self.bundle)
        self.assertNotIn("application/vnd.ms-excel", self.bundle)
        self.assertNotIn("barBg", self.bundle)

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
        self.assertIn("配方比例对比表_${filenamePart(state.queryScope.query)}.xlsx", self.html)

    def test_cost_totals_renamed_and_carry_simulated_sales_and_profit(self) -> None:
        self.assertIn(">系统销售价格<", self.html)
        self.assertIn("label:'当前成本合价'", self.html)
        self.assertIn(">其他综合成本<", self.html)
        # 旧标签不能残留，否则同一个数字会有两种叫法
        self.assertNotIn(">当前合价<", self.html)
        self.assertIn('data-metric="simSales"', self.html)
        self.assertIn('data-metric="otherCost"', self.html)

    def test_three_cost_calibers_share_one_definition_and_carry_three_profit_metrics(self) -> None:
        # 单本卡片与多选对比汇总表共用 COST_CALIBERS；两处各写一份必然漂
        self.assertIn("const COST_CALIBERS=[", self.html)
        for label in ("系统合价", "当前成本合价", "模拟合价"):
            self.assertIn(f"label:'{label}'", self.html)
        for row in ("合价", "利润额", "毛利率", "成本加成率"):
            self.assertIn(f"'{row}'", self.html)
        # 售价统一取模拟销售价格（未手填回落系统销售价格），三个口径只换成本
        self.assertIn("const profitFor=(recipe,cost)=>CostCore.profitMetrics({salesPrice:simSalesPriceFor(recipe),cost,otherCost:otherCostFor(recipe)});", self.html)
        # 对比汇总表按口径分组，不再只出一行毛利率
        self.assertIn("const caliberRows=COST_CALIBERS.flatMap(", self.html)
        self.assertNotIn(">预估利润（毛利率）<", self.html)
        self.assertNotIn("预估利润（毛利率）</td>", self.html)
        # 窄屏下利润表自己横向滚动，页面 body 不横滚
        self.assertIn(".profit-scroll{overflow-x:auto", self.html)

    def test_simulated_sales_and_other_cost_persist_with_timestamps(self) -> None:
        # 与「当下价格」同一套记忆语义：值 + 时间戳两个 key，粒度分别是父件编码 / 父件编码::版本
        self.assertIn("const SIM_SALES_KEY='formula_sim_sales_prices'", self.html)
        self.assertIn("const SIM_SALES_TIME_KEY='formula_sim_sales_price_times'", self.html)
        self.assertIn("const OTHER_COST_KEY='formula_other_costs'", self.html)
        self.assertIn("const OTHER_COST_TIME_KEY='formula_other_cost_times'", self.html)
        self.assertIn("state.simSalesPrices[code]=value;state.simSalesTimes[code]=Date.now();", self.html)
        self.assertIn("state.otherCosts[key]=value;state.otherCostTimes[key]=Date.now();", self.html)

    def test_profit_uses_cost_core_and_shows_both_calibers_in_tooltip(self) -> None:
        self.assertIn('<script src="cost-core.js"></script>', self.html)
        self.assertIn("CostCore.profitMetrics(", self.html)
        self.assertIn("CostCore.profitTooltipText(", self.html)
        self.assertIn("毛利率 = ${head} ÷", self.core_cost)
        self.assertIn("加成率 = ${head} ÷", self.core_cost)
        # 可换行的 tooltip 变体：单行 nowrap 的 .price-cell 装不下算式
        self.assertIn("white-space:pre-line", self.html)
        self.assertIn(".calc-tip[data-tooltip]:hover::after", self.html)

    def test_cost_compare_mode_is_frontend_only_and_group_toggleable(self) -> None:
        self.assertIn('data-cost-mode="single"', self.html)
        self.assertIn('data-cost-mode="compare"', self.html)
        for group in ["recipeGroup", "systemGroup", "currentGroup", "simGroup"]:
            self.assertIn(f'data-cost-view="{group}"', self.html)
        self.assertIn("const COST_VIEW_KEY='formula_cost_view_options'", self.html)
        self.assertIn("CostCore.buildCostMatrix(", self.html)
        # 对比模式不额外请求后端：/v1/recipes/cost 一次就返回了全部 recipes
        self.assertEqual(1, self.html.count("await api('/v1/recipes/cost'"))

    def test_compare_mode_keeps_price_editable_but_simulated_quantity_readonly(self) -> None:
        # 模拟数量按子件编码全局存，各版本原数量不同，多列并排会自相矛盾，故对比模式只读
        matrix = self.html[self.html.index("function renderCostMatrix("):self.html.index("function selectVersion(")]
        self.assertIn('class="price-input"', matrix)
        self.assertNotIn("sim-qty-input", matrix)
        self.assertIn("模拟数量」同样是全局值", self.html)

    def test_cost_view_keeps_at_least_one_element_group(self) -> None:
        self.assertIn("COST_GROUP_KEYS.some((item)=>state.costView[item])", self.html)
        self.assertIn("至少要保留一组对比要素。", self.html)

    def test_cost_matrix_shares_single_view_visual_rules(self) -> None:
        # 矩阵表复用单本那份 CSS 定义，不复制一套——两处样式各写一份迟早会漂
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.cur-cost", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) td.ratio-cell::after", self.html)
        self.assertIn(":is(.cost-table,.cost-matrix-table) th,:is(.cost-table,.cost-matrix-table) td{border-right", self.html)
        matrix = self.html[self.html.index("function renderCostMatrix("):self.html.index("function selectVersion(")]
        for cls in ["sys-price", "sys-cost", "cur-cost", "sim-cost", "sim-ratio", "ratio-cell", "qty", "sum-formula", "sum-cost"]:
            self.assertIn(cls, matrix, msg=cls)
        # 配方之间的分隔要比组内分隔重，否则「组末」和「本末」看起来一样
        self.assertIn(".cost-matrix-table .recipe-end{border-right:3px", self.html)
        self.assertIn("recipe-end", matrix)
        # 比例条按每本配方自己的最大比例缩放
        self.assertIn("const scales=recipes.map(", matrix)

    def test_compare_sort_control_sits_after_filters_and_covers_four_keys(self) -> None:
        self.assertLess(self.html.index('data-filter="same"'), self.html.index('id="compareSortDrop"'))
        for key in ["default", "code", "name", "ratio", "family"]:
            self.assertIn(f'data-sort-key="{key}"', self.html)
        self.assertIn('data-sort-dir="asc"', self.html)
        self.assertIn('data-sort-dir="desc"', self.html)
        self.assertIn("const SORT_KEY='formula_compare_sort'", self.html)

    def test_compare_table_and_excel_share_one_sort(self) -> None:
        # 改这条之前：页面按 buildCompareMatrix 的编码序、Excel 按状态分组，同一份数据两种行序
        self.assertIn("CompareCore.sortCompareRows(", self.html)
        self.assertIn("sortKey:state.sort.sortKey,sortDir:state.sort.sortDir", self.html)
        self.assertIn("sortCompareRows(", self.core)
        self.assertIn("sortKey: args.sortKey, sortDir: args.sortDir", self.core)
        self.assertNotIn("(orderOf(a.st) - orderOf(b.st)) || a.itemCode.localeCompare", self.core)

    def test_ratio_sort_pins_zero_ratio_rows_last(self) -> None:
        # 不计比例的行 ratio 是 0 不是 NaN；只判 isFinite 会让它们在倒序时翻到最前
        self.assertIn("Math.abs(value) >= 0.000005", self.core)
        self.assertIn("const valued = rows.filter(hasRatio);", self.core)


if __name__ == "__main__":
    unittest.main()
