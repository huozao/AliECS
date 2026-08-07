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

    def test_page_has_lab_space_datasets_and_filters(self) -> None:
        self.assertIn("标准型号色彩空间", self.html)
        self.assertIn('id="datasetLive"', self.html)
        self.assertIn('id="datasetMock"', self.html)
        self.assertIn('id="resinFilter"', self.html)
        self.assertIn('id="dosageFilter"', self.html)
        self.assertIn('id="statusFilter"', self.html)

    def test_live_dataset_reads_backend_and_mock_stays_lazy(self) -> None:
        self.assertIn("/v1/formula/colors", self.html)
        self.assertIn("aliecs_auth_token", self.html)
        self.assertIn("await import('./mock-data.js')", self.html)
        # 参考示例必须是惰性加载，否则默认视图会被模拟数据污染。
        self.assertNotIn("import {MOCK_RECIPE_COLORS} from './mock-data.js'", self.html)
        self.assertIn("state={dataset:'live'", self.html)

    def test_tolerance_boxes_render_from_internal_control_intervals(self) -> None:
        self.assertIn("function toleranceRange", self.html)
        self.assertIn("function rebuildTolerance", self.html)
        self.assertIn("function boxesOverlap", self.html)
        self.assertIn('id="toggleTolerance"', self.html)
        self.assertIn('id="toleranceOpacity"', self.html)
        self.assertIn('id="toggleOverlapOnly"', self.html)
        self.assertIn("showTolerance:true", self.html)
        self.assertIn("new THREE.EdgesGeometry(geometry)", self.html)

    def test_tplus_match_status_is_surfaced(self) -> None:
        self.assertIn("code_missing", self.html)
        self.assertIn('id="matchWarning"', self.html)
        self.assertIn('id="detailParentCode"', self.html)
        self.assertIn('id="detailParentName"', self.html)
        self.assertIn("T+ 当前有效物料清单", self.html)

    def test_lab_coordinates_keep_uniform_scale(self) -> None:
        self.assertIn("const SCALE=.23", self.html)
        self.assertIn("lab[1]*SCALE", self.html)
        self.assertIn("-lab[2]*SCALE", self.html)
        self.assertIn("state.sliceEnabled?state.sliceL-50:lab[0]-50", self.html)

    def test_lab_axes_use_directional_colors_and_labels(self) -> None:
        self.assertIn("function addGradientAxis", self.html)
        self.assertIn("官方 CIELAB 方向", self.html)
        self.assertIn("−a* 绿", self.html)
        self.assertIn("+a* 红", self.html)
        self.assertIn("−b* 蓝", self.html)
        self.assertIn("+b* 黄", self.html)
        # a*b* 俯视要把 up 轴换成 −b*，否则俯视时黄蓝方向会左右颠倒。
        self.assertIn("applyView([0,0,-1],[0,1,0])", self.html)

    def test_ab_grid_has_key_value_scale_labels(self) -> None:
        self.assertIn("gridLabelGroup=new THREE.Group()", self.html)
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

    def test_view_controls_live_in_a_top_panel_not_over_the_canvas(self) -> None:
        # 浮层压在画布上会挡住三维图；改为顶部横幅面板，展开时挤压画布高度。
        self.assertNotIn('<details id="viewSettings"', self.html)
        self.assertNotIn("<summary>视图设置</summary>", self.html)
        self.assertIn('id="toggleSettings"', self.html)
        self.assertIn('id="settingsPanel"', self.html)
        self.assertIn("显示设置", self.html)
        for control in ("toggleReference", "resetCamera", "topCamera", "frontCamera",
                        "sideCamera", "focusSelected", "togglePan", "toggleTolerance",
                        "toleranceMagnify", "referenceMode", "toggleAllLabels", "labelFocal"):
            self.assertIn(f'id="{control}"', self.html)

    def test_settings_panel_sits_between_toolbar_and_workspace(self) -> None:
        toolbar = self.html.index('class="toolbar"')
        panel = self.html.index('id="settingsPanel"')
        workspace = self.html.index('class="workspace"')
        self.assertLess(toolbar, panel)
        self.assertLess(panel, workspace)

    def test_settings_panel_groups_are_labelled(self) -> None:
        for group in ("观察视角", "显示开关", "容差盒", "参考色域", "标签"):
            self.assertIn(group, self.html)

    def test_label_preferences_can_be_saved_as_default(self) -> None:
        self.assertIn("aliecs_formula_colors_view_prefs", self.html)
        self.assertIn('id="saveViewPrefs"', self.html)
        self.assertIn('id="resetViewPrefs"', self.html)
        self.assertIn("function savePrefs", self.html)
        self.assertIn("function loadPrefs", self.html)
        self.assertIn("function applyPrefsToControls", self.html)

    def test_only_label_preferences_persist(self) -> None:
        """容差盒/参考色域等不进 localStorage，否则下次打开会莫名其妙是隐藏状态。"""
        prefs = self.html[self.html.index("function savePrefs"):self.html.index("function loadPrefs")]
        self.assertIn("labelFields", prefs)
        self.assertIn("showAllLabels", prefs)
        self.assertIn("labelFocal", prefs)
        for leaked in ("showTolerance", "showReference", "toleranceMagnify", "referenceMode"):
            self.assertNotIn(leaked, prefs)

    def test_hover_highlights_tolerance_overlapping_models(self) -> None:
        self.assertIn("function labelHighlightSet", self.html)
        self.assertIn("boxesOverlap(focus,item)", self.html)
        # 重叠判定必须走真实比例，放大后的盒会得出错误结论。
        self.assertIn("function boxesOverlap", self.html)
        self.assertIn("toleranceRange(a,axis)", self.html)
        # 孤立型号没有重叠邻居，此时不该把全场压暗。
        self.assertIn("return set.size>1?set:null", self.html)

    def test_highlight_dims_unrelated_tolerance_boxes_too(self) -> None:
        self.assertIn("const highlight=labelHighlightSet()", self.html)
        self.assertIn("dimmed=highlight&&!highlight.has(item.id)", self.html)

    def test_unique_search_hit_flies_the_camera_to_it(self) -> None:
        self.assertIn("state.points.length===1", self.html)
        self.assertIn("selectPoint(state.points[0]);focusSelected()", self.html)

    def test_manual_refresh_button_reenqueues_wecom_sync(self) -> None:
        self.assertIn('id="refreshData"', self.html)
        self.assertIn("刷新数据", self.html)
        self.assertIn("/v1/formula/colors/refresh", self.html)
        self.assertIn("method:'POST'", self.html)
        self.assertIn("updateDatasetMetaUi()", self.html)
    def test_camera_controls_replace_orbit_controls(self) -> None:
        self.assertIn("camera-controls@2.10.1", self.html)
        self.assertIn("CameraControls.install({THREE})", self.html)
        self.assertNotIn("OrbitControls.js", self.html)
        # camera-controls 的 update 必须吃 delta，否则阻尼与过渡动画不会推进。
        self.assertIn("controls.update(clock.getDelta())", self.html)
        self.assertIn("controls.fitToBox(box,true", self.html)

    def test_touch_gestures_are_explicit_and_pannable(self) -> None:
        # 缺 touch-action:none 时双指手势会先被浏览器当成页面缩放吃掉。
        self.assertIn("touch-action:none", self.html)
        self.assertIn("controls.touches.two=CameraControls.ACTION.TOUCH_DOLLY_TRUCK", self.html)
        self.assertIn("function setPanMode", self.html)
        self.assertIn('id="togglePan"', self.html)

    def test_point_radius_stays_smaller_than_tolerance_box(self) -> None:
        # 球半径 .14 世界单位 ≈ .61 Lab 单位；容差盒最长边通常 .4 Lab 单位。
        self.assertIn("new THREE.SphereGeometry(.14", self.html)
        self.assertIn("new THREE.SphereGeometry(.5,10,8)", self.html)
        # 点半径正比于相机距离 = 屏幕恒定大小，凑近时自动让出容差盒。
        self.assertIn("controls.distance/SCREEN_CONSTANT_DISTANCE", self.html)
        self.assertIn("function updatePointScale", self.html)

    def test_views_frame_the_data_instead_of_whole_lab_space(self) -> None:
        # 41 个型号只占 Lab 全空间一小块，固定视距会让数据缩成一团。
        self.assertIn("function dataBox", self.html)
        self.assertIn("controls.setLookAt(eye.x,eye.y,eye.z", self.html)
        self.assertIn("box.expandByPoint(positionFor(item))", self.html)

    def test_tolerance_magnification_never_leaks_into_judgement(self) -> None:
        self.assertIn('id="toleranceMagnify"', self.html)
        self.assertIn("toleranceMagnify:1", self.html)
        # 放大只作用于渲染；重叠判定与容差内判定必须走默认 magnify=1。
        self.assertIn("function toleranceRange(item,axis,magnify=1)", self.html)
        self.assertIn("toleranceRange(a,axis)", self.html)
        self.assertIn('`容差盒 ×${state.toleranceMagnify}`', self.html)

    def test_delta_view_uses_anchor_relative_coordinates(self) -> None:
        self.assertIn('id="deltaView"', self.html)
        self.assertIn("function labToWorld", self.html)
        self.assertIn("function buildDeltaAxes", self.html)
        self.assertIn("const DELTA_RANGE=2,DELTA_SCALE=SCALE*25", self.html)
        # Δ 视图是判色依据，必须真实比例；进入时强制把放大系数打回 1。
        self.assertIn("state.toleranceMagnify=1;$('toleranceMagnify').value='1'", self.html)
        self.assertIn("deltaAxisGroup.visible=delta", self.html)

    def test_close_up_hides_global_axis_decoration(self) -> None:
        self.assertIn("CLOSE_UP_DISTANCE", self.html)
        self.assertIn("function syncSceneScale", self.html)
        self.assertIn("axisGroup.visible=!delta&&!closeUp", self.html)

    def test_reference_opacity_can_reach_100_percent(self) -> None:
        self.assertIn('id="referenceOpacity" type="range" min="10" max="100"', self.html)
        self.assertIn("Math.min(1,state.referenceOpacity+.1)", self.html)

    def test_product_colors_can_be_hidden_independently(self) -> None:
        self.assertIn('id="toggleProducts"', self.html)
        self.assertIn("showProducts:true", self.html)
        self.assertIn("pointMesh.visible=showProducts", self.html)
        self.assertIn("trajectoryGroup.visible=showProducts", self.html)
        self.assertIn("if(state.showProducts){", self.html)
        self.assertIn("state.showProducts=!state.showProducts", self.html)
        # 关掉标准色点后容差盒仍需可点选，否则详情面板会失去入口。
        self.assertIn("state.showTolerance&&toleranceGroup.visible", self.html)

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

    def test_detail_fields_drive_a_dom_label_layer(self) -> None:
        self.assertGreaterEqual(self.html.count('data-label-field="'), 11)
        self.assertIn("labelFields:new Set(['formula','resin','dosage'])", self.html)
        self.assertIn("function rebuildLabels", self.html)
        self.assertIn("function syncLabels", self.html)
        self.assertIn("checkbox.onchange", self.html)
        # 标签改成 DOM 层：canvas sprite 每块要建 192×72 纹理，全量显示时会吃爆显存。
        self.assertIn('class="label-layer"', self.html)
        self.assertIn("el.className='point-label'", self.html)
        self.assertNotIn("selectedLabelCanvas", self.html)
        self.assertNotIn("function updateSelectedLabel", self.html)

    def test_label_targets_hide_with_product_points(self) -> None:
        # 隐藏标准色点后其 DOM 标签也必须一起消失，否则会追着一个隐形的点漂浮。
        self.assertIn("if(!state.showProducts||!state.labelFields.size)return[]", self.html)

    def test_label_opacity_uses_cosine_fade_from_the_orbit_target(self) -> None:
        # 余弦缓动：焦点附近与远端都平缓，过渡集中在中段，比线性自然。
        self.assertIn("controls.getTarget(_focalV3)", self.html)
        self.assertIn(".5-.5*Math.cos(normalized*Math.PI)", self.html)
        self.assertIn("function labelOpacityBase", self.html)
        self.assertIn("function labelHighlightSet", self.html)
        # 三态：hover/选中全亮，非高亮压到 20% 以下，其余按距离淡化。
        self.assertIn("Math.min(base,.2)", self.html)

    def test_label_layer_only_repaints_when_the_camera_moved(self) -> None:
        # camera-controls 的 update() 返回是否有相机变化，是最可靠的节流信号。
        self.assertIn("const cameraMoved=controls.update(clock.getDelta())", self.html)
        self.assertIn("if(cameraMoved||labelsDirty)", self.html)

    def test_all_labels_can_be_shown_at_once(self) -> None:
        self.assertIn('id="toggleAllLabels"', self.html)
        self.assertIn("showAllLabels:false", self.html)
        self.assertIn("state.showAllLabels=", self.html)
        # 全量显示时不再限制点数：DOM 标签没有纹理开销。
        self.assertIn("if(state.showAllLabels)return state.points", self.html)

    def test_label_fade_distance_is_adjustable(self) -> None:
        self.assertIn('id="labelFocal" type="range" min="2" max="60"', self.html)
        self.assertIn("labelFocal:12", self.html)
        self.assertIn('id="labelFocalValue"', self.html)

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
        # 标准型号是主角，参考色域默认关闭，避免体素方块遮住容差盒。
        self.assertIn("showReference:false", self.html)
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
        self.assertIn("const hitGeometry=new THREE.SphereGeometry(.5", self.html)
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
