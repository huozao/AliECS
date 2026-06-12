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


if __name__ == "__main__":
    unittest.main()
