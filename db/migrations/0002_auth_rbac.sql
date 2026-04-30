CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  is_admin BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS roles (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS permissions (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_roles (
  user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
  role_id BIGINT REFERENCES roles(id) ON DELETE CASCADE,
  PRIMARY KEY(user_id, role_id)
);

CREATE TABLE IF NOT EXISTS role_permissions (
  role_id BIGINT REFERENCES roles(id) ON DELETE CASCADE,
  permission_id BIGINT REFERENCES permissions(id) ON DELETE CASCADE,
  PRIMARY KEY(role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS features (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  url TEXT,
  category TEXT,
  required_permission TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  sort_order INTEGER NOT NULL DEFAULT 100,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGSERIAL PRIMARY KEY,
  actor_username TEXT,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  detail JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO roles (code,name,description) VALUES
('admin','系统管理员','系统最高权限'),('manager','管理人员','管理业务配置'),('operator','操作人员','业务录入操作'),('viewer','只读用户','仅查看')
ON CONFLICT (code) DO NOTHING;

INSERT INTO permissions (code,name,description) VALUES
('admin.access','后台访问','可进入管理后台'),('admin.users.manage','用户管理','可管理用户'),('admin.roles.manage','角色管理','可管理角色'),('admin.features.manage','功能管理','可管理功能入口'),
('formula.read','配方查询读取','读取配方查询'),('formula.write','配方写入','维护配方'),('midea.requirement.read','美的需求读取','读取需求'),('inventory.raw.read','原材料库存读取','读取原材料库存'),('inventory.finished.read','成品库存读取','读取成品库存'),
('production.schedule.read','排产读取','查看排产'),('production.schedule.write','排产写入','维护排产'),('personal.access','个人板块访问','访问个人板块')
ON CONFLICT (code) DO NOTHING;

WITH admin_role AS (SELECT id FROM roles WHERE code='admin')
INSERT INTO role_permissions(role_id, permission_id)
SELECT admin_role.id, p.id FROM admin_role, permissions p
ON CONFLICT DO NOTHING;

INSERT INTO features(code,title,description,url,category,required_permission,status,sort_order) VALUES
('new_model_form','新品型号录入表','新品型号登记','https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4b7094','业务录入','production.schedule.write','active',10),
('schedule_form','排产登记表','排产信息登记','https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_e3792e','业务录入','production.schedule.write','active',20),
('pending_return_alert','待处理+退货提醒','待处理与退货提醒','https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4501d0','业务录入','production.schedule.read','active',30),
('naming_form','产品命名登记','产品命名录入','https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_a577fc','业务录入','formula.write','active',40),
('qc_form','检测数据登记表','检测数据登记','https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_b669cf','质检','formula.read','active',50),
('density_calculator','配方密度计算器','配方密度工具','https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_bac993','质检','formula.read','active',60),
('formula_query','配方查询','配方检索入口',NULL,'业务查询','formula.read','reserved',70),
('midea_requirement','美的需求','需求查询入口',NULL,'业务查询','midea.requirement.read','reserved',80),
('raw_inventory','原材料库存','原材料库存入口',NULL,'业务查询','inventory.raw.read','reserved',90),
('finished_inventory','成品库存','成品库存入口',NULL,'业务查询','inventory.finished.read','reserved',100),
('personal_section','个人板块','个人工具入口','https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_0c521a','个人','personal.access','active',110),
('admin_ui','Admin UI','管理后台入口','/admin/','系统','admin.access','active',120)
ON CONFLICT (code) DO NOTHING;
