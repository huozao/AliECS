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
        self.assertIn("line.spec||''", self.html)
        self.assertIn('<td colspan="4">汇总</td>', self.html)
        self.assertIn(".cost-table tbody td{text-align:center", self.html)
        self.assertIn(".cost-table td.text-left{text-align:left", self.html)

    def test_cost_reset_button_has_visible_button_border(self) -> None:
        self.assertIn(".reset-link{border:1px solid", self.html)
        self.assertIn("background:#fffdf9", self.html)

    def test_cost_inputs_have_compact_fixed_width(self) -> None:
        self.assertIn("--cost-input-width:6.2ch", self.html)
        self.assertIn(".cost-table{min-width:1280px", self.html)
        self.assertIn("width:var(--cost-input-width)", self.html)
        self.assertIn("max-width:var(--cost-input-width)", self.html)

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

    def test_raw_download_still_uses_server_workbook_and_compare_download_is_client_table(self) -> None:
        self.assertIn("function downloadRawResult()", self.html)
        self.assertIn("/v1/recipes/download/${state.fileId}", self.html)
        self.assertIn("function downloadHtmlAsXls(", self.html)
        self.assertIn("对比的配方_比例对比表", self.html)
        self.assertIn("application/vnd.ms-excel", self.html)


if __name__ == "__main__":
    unittest.main()
