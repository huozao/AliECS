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
        self.assertIn("--cost-input-width:88px", self.html)
        self.assertIn("width:var(--cost-input-width)", self.html)
        self.assertIn("max-width:var(--cost-input-width)", self.html)

    def test_current_price_time_is_input_hover_tooltip(self) -> None:
        self.assertIn('class="price-cell"${priceTooltip}', self.html)
        self.assertIn('data-tooltip="更新时间：${fmtPriceTime(t)}"', self.html)
        self.assertIn(".price-cell[data-tooltip]:hover::after", self.html)


if __name__ == "__main__":
    unittest.main()
