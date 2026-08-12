-- 0048: Clash 配置合成器 —— 第三方机场订阅源
-- 设计成多行而非单行配置：机场跑路是常态，换机场时需要能先加新的、验证通过再删旧的。
-- url 含机场分配的 token，属敏感数据，只存库不进仓库。
-- 自建节点定义不在这张表里，走环境变量 CLASH_SELF_NODES_B64（SOPS 渲染）。
-- 幂等：IF NOT EXISTS，可安全重复执行。
CREATE TABLE IF NOT EXISTS clash_profile_providers (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  url         TEXT NOT NULL,
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
