from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "services" / "public-web" / "formula" / "colors" / "index.html"
MOCK_DATA = ROOT / "services" / "public-web" / "formula" / "colors" / "mock-data.js"
MIGRATION = ROOT / "db" / "migrations" / "0027_formula_color_space_feature.sql"


class FormulaColorSpaceFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = PAGE.read_text(encoding="utf-8")
        self.mock = MOCK_DATA.read_text(encoding="utf-8")

    def test_page_has_lab_space_modes_and_filters(self) -> None:
        self.assertIn("配方色彩空间", self.html)
        self.assertIn('data-mode="absolute"', self.html)
        self.assertIn('data-mode="relative"', self.html)
        self.assertIn('data-mode="trajectory"', self.html)
        self.assertIn('id="resinFilter"', self.html)
        self.assertIn('id="dosageFilter"', self.html)

    def test_lab_coordinates_keep_uniform_scale(self) -> None:
        self.assertIn("const SCALE=.23", self.html)
        self.assertIn("source[1]*SCALE", self.html)
        self.assertIn("-source[2]*SCALE", self.html)
        self.assertIn("state.sliceEnabled?state.sliceL-50:lab[0]-50", self.html)

    def test_lab_axes_use_directional_colors_and_labels(self) -> None:
        self.assertIn("function addGradientAxis", self.html)
        self.assertIn("官方 CIELAB 方向", self.html)
        self.assertIn("−a* 绿", self.html)
        self.assertIn("+a* 红", self.html)
        self.assertIn("−b* 蓝", self.html)
        self.assertIn("+b* 黄", self.html)
        self.assertIn("camera.up.set(0,0,-1)", self.html)
        self.assertIn("camera.position.set(0,55,0)", self.html)

    def test_ab_grid_has_key_value_scale_labels(self) -> None:
        self.assertIn("const gridLabelGroup", self.html)
        self.assertIn("[-100,-75,-50,-25,0,25,50,75,100]", self.html)
        self.assertNotIn("a* 投影轴", self.html)
        self.assertNotIn("b* 投影轴", self.html)
        self.assertIn("GROUND_Y=-50*SCALE", self.html)
        self.assertIn("major=value!==0&&Math.abs(value)%50===0", self.html)
        self.assertIn("half=major ? .72 : .38", self.html)
        self.assertIn("两个正交方向组成十字刻度", self.html)

    def test_l_axis_marks_five_key_lightness_values(self) -> None:
        self.assertIn("[0,25,50,75,100]", self.html)
        self.assertIn("`L* ${lightness}`", self.html)
        self.assertIn("(lightness-50)*SCALE", self.html)

    def test_mobile_layout_keeps_filters_in_two_columns_and_metrics_in_one_row(self) -> None:
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", self.html)
        self.assertIn("flex-wrap:nowrap", self.html)
        self.assertNotIn("<br/>现有产品点", self.html)

    def test_view_controls_are_collapsible(self) -> None:
        self.assertIn('<details id="viewSettings"', self.html)
        self.assertIn("<summary>视图设置</summary>", self.html)
        self.assertIn('id="toggleReference"', self.html)
        self.assertIn('id="resetCamera"', self.html)
        self.assertIn('id="topCamera"', self.html)

    def test_reference_opacity_can_reach_100_percent(self) -> None:
        self.assertIn('id="referenceOpacity" type="range" min="10" max="100"', self.html)
        self.assertIn("Math.min(1,state.referenceOpacity+.1)", self.html)

    def test_product_colors_can_be_hidden_independently(self) -> None:
        self.assertIn('id="toggleProducts"', self.html)
        self.assertIn("showProducts:true", self.html)
        self.assertIn("pointMesh.visible=showProducts", self.html)
        self.assertIn("trajectoryGroup.visible=showProducts", self.html)
        self.assertIn("if(!state.showProducts)return null", self.html)
        self.assertIn("state.showProducts=!state.showProducts", self.html)

    def test_reference_voxels_show_hover_and_pinned_lab_readout(self) -> None:
        self.assertIn('id="referenceLabReadout"', self.html)
        self.assertIn("function referenceHitAt", self.html)
        self.assertIn("function updateReferenceReadout", self.html)
        self.assertIn("mesh.userData={samples,space}", self.html)
        self.assertIn("state.referencePinned=referenceHitAt(event)", self.html)
        self.assertIn("a* ${round(sample.lab[1],1)}", self.html)
        self.assertIn("sample.space==='p3'?'P3':'sRGB'", self.html)

    def test_reference_gamut_supports_l_horizontal_slice(self) -> None:
        self.assertIn('id="toggleLSlice"', self.html)
        self.assertIn('id="sliceL"', self.html)
        self.assertIn("sliceEnabled:false", self.html)
        self.assertIn("Math.abs(lab[0]-state.sliceL)>2.5", self.html)
        self.assertIn("state.sliceEnabled?state.sliceL-50:lab[0]-50", self.html)
        self.assertIn("L*=${state.sliceL} 切面", self.html)

    def test_detail_fields_control_selected_product_3d_label(self) -> None:
        self.assertGreaterEqual(self.html.count('data-label-field="'), 11)
        self.assertIn("labelFields:new Set(['formula','resin','dosage'])", self.html)
        self.assertIn("function updateSelectedLabel", self.html)
        self.assertIn("function labelValue", self.html)
        self.assertIn("selectedLabel.position.copy(positionFor(state.selected))", self.html)
        self.assertIn("checkbox.onchange", self.html)

    def test_color_math_and_gamut_warning_are_present(self) -> None:
        self.assertIn("function deltaE76", self.html)
        self.assertIn("function deltaE00", self.html)
        self.assertIn("function labToSrgb", self.html)
        self.assertIn("outOfGamut", self.html)
        self.assertIn("超出 sRGB 色域", self.html)

    def test_scene_separates_reference_gamut_products_and_target(self) -> None:
        self.assertIn("referenceGroup", self.html)
        self.assertIn("labBounds", self.html)
        self.assertIn("targetMarker", self.html)
        self.assertIn("neighborLineGroup", self.html)
        self.assertIn("sRGB + Display-P3", self.html)

    def test_reference_gamut_uses_dense_toggleable_instanced_voxels(self) -> None:
        self.assertIn('id="toggleReference"', self.html)
        self.assertIn("showReference:true", self.html)
        self.assertIn("const GAMUT_STEPS=20", self.html)
        self.assertIn("new THREE.BoxGeometry(.44", self.html)
        self.assertIn("new THREE.InstancedMesh", self.html)
        self.assertIn("referenceGroup.visible=showReference", self.html)
        self.assertIn("state.showReference&&state.mode!=='relative'", self.html)

    def test_reference_gamut_supports_p3_comparison_styles_and_device_hint(self) -> None:
        self.assertIn("function displayP3ToLab", self.html)
        self.assertIn('value="overlay"', self.html)
        self.assertIn('value="difference"', self.html)
        self.assertIn('value="solid"', self.html)
        self.assertIn('value="surface"', self.html)
        self.assertIn('value="wireframe"', self.html)
        self.assertIn('id="referenceOpacity"', self.html)
        self.assertIn("color-gamut: p3", self.html)

    def test_product_points_have_larger_invisible_hit_targets_and_hover_feedback(self) -> None:
        self.assertIn("const hitGeometry=new THREE.SphereGeometry(1.28", self.html)
        self.assertIn("colorWrite:false", self.html)
        self.assertIn("raycaster.intersectObject(hitMesh)", self.html)
        self.assertIn("screenRadius=32", self.html)
        self.assertIn("hitAt(event,44)", self.html)
        self.assertIn("positionFor(item).clone().project(camera)", self.html)
        self.assertIn("state.hoveredId", self.html)
        self.assertIn("style.cursor='pointer'", self.html)
        self.assertIn("style.cursor=reference?'crosshair':'grab'", self.html)

    def test_target_lab_returns_ranked_nearby_products(self) -> None:
        self.assertIn("function locateTarget", self.html)
        self.assertIn("slice(0,5)", self.html)
        self.assertIn('id="nearestResults"', self.html)
        self.assertIn("data-neighbor-id", self.html)
        self.assertIn("近邻按 ΔE00 排序", self.html)

    def test_product_coverage_uses_explicit_lab_voxels_and_ranges(self) -> None:
        self.assertIn("function updateCoverage", self.html)
        self.assertIn('id="coverageCount"', self.html)
        self.assertIn('id="coverageSummary"', self.html)
        self.assertIn("10×12×12 Lab 网格", self.html)

    def test_mock_data_covers_formulas_resins_and_dosages(self) -> None:
        self.assertIn("ABS-757", self.mock)
        self.assertIn("PP-T30S", self.mock)
        self.assertIn("PA6-1013", self.mock)
        self.assertIn("HYD-RD-01", self.mock)
        self.assertIn("const DOSAGES = [0.1,0.25,0.5,0.75,1.0]", self.mock)
        self.assertIn("sample_lab:lab", self.mock)
        self.assertIn("baseline_lab", self.mock)

    def test_home_feature_contract(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("formula_color_space", sql)
        self.assertIn("/formula/colors/", sql)
        self.assertIn("formula.read", sql)


if __name__ == "__main__":
    unittest.main()
