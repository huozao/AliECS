-- 0023: 新增公开「AI 文件中转」入口，跳转到独立 Gokapi 服务。
-- 主站只展示入口，不接入 Gokapi 上传、下载、登录、管理或 API。
INSERT INTO features(code,title,description,url,category,required_permission,status,sort_order) VALUES
('ai_file_transfer','AI 文件中转','上传临时文件，生成公开下载链接。','https://files.hydwang.xyz','工具',NULL,'active',140)
ON CONFLICT (code) DO UPDATE SET
  title = EXCLUDED.title,
  description = EXCLUDED.description,
  url = EXCLUDED.url,
  category = EXCLUDED.category,
  required_permission = EXCLUDED.required_permission,
  status = EXCLUDED.status,
  sort_order = EXCLUDED.sort_order,
  updated_at = NOW();
