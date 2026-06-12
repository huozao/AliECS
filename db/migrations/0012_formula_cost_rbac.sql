INSERT INTO roles(code, name, description) VALUES
('chairman', '董事长', '董事长角色'),
('general_manager_a', '总经理A', '总经理A角色'),
('general_manager_b', '总经理B', '总经理B角色'),
('sales_a', '销售A', '销售A角色'),
('sales_b', '销售B', '销售B角色'),
('tech_a', '技术A', '技术A角色'),
('tech_b', '技术B', '技术B角色'),
('finance_a', '财务A', '财务A角色'),
('finance_b', '财务B', '财务B角色'),
('warehouse_a', '库管A', '库管A角色'),
('warehouse_b', '库管B', '库管B角色')
ON CONFLICT(code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description;

INSERT INTO permissions(code, name, description) VALUES
('formula.cost.calculate', '配方成本核算', '可使用系统配方成本核算和核算导出')
ON CONFLICT(code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description;

INSERT INTO role_permissions(role_id, permission_id)
SELECT r.id, p.id
FROM roles r, permissions p
WHERE r.code = 'admin' AND p.code = 'formula.cost.calculate'
ON CONFLICT DO NOTHING;
