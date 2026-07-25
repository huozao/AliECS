-- 0043: 新增「工具」分类功能入口 —— 浮力法密度计算器
-- 注意：code `density_calculator` 已被「配方密度计算器」（质检分类，指向企微表格）占用，此处另起 code。
-- 幂等：code 唯一，冲突跳过，可安全重复执行。
INSERT INTO features(code,title,description,url,category,required_permission,status,sort_order) VALUES
('density_buoyancy_calc','浮力法密度计算器','按空气中与液体中重量计算样品密度，支持水/乙醇/自定义介质与导出PDF报告','/tools/density-calc/','工具',NULL,'active',135)
ON CONFLICT (code) DO NOTHING;
