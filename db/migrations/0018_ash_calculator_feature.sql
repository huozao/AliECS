-- 0018: 新增「工具」分类功能入口 —— 分段灰分 / 填料半定量计算器
-- 幂等：code 唯一，冲突跳过，可安全重复执行。
INSERT INTO features(code,title,description,url,category,required_permission,status,sort_order) VALUES
('ash_calculator','分段灰分/填料计算器','分段灼烧灰分与炭黑/填料半定量计算，支持导出PDF报告','/tools/ash-calc/','工具',NULL,'active',130)
ON CONFLICT (code) DO NOTHING;
