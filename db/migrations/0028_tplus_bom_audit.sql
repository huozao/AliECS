-- T+ BOM 审核权限（与写入分离，为将来提交人≠审核人留口）
INSERT INTO permissions(code, name, description) VALUES
('tplus.bom.audit', 'T+ BOM 审核', '审核 T+ 物料清单（调用 T+ bom/Audit）')
ON CONFLICT (code) DO NOTHING;

WITH admin_role AS (SELECT id FROM roles WHERE code = 'admin')
INSERT INTO role_permissions(role_id, permission_id)
SELECT admin_role.id, p.id
FROM admin_role, permissions p
WHERE p.code = 'tplus.bom.audit'
ON CONFLICT DO NOTHING;
