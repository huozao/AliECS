-- 0051: Clash 订阅节点快照 —— 服务端拉取结果
--
-- 为什么要存：机场按源 IP 封禁了家宽出口（2026-08-15 实测，含同一条宽带上的 webdock2），
-- 客户端拉不到任何订阅；backend-api 跑在 txecs 上，出口正常。拉取因此上移到服务端，
-- 客户端只读本地节点文件（proxy-provider 从 type: http 改成 type: file）。
--
-- 每个订阅源只保留最新一份，所以 provider_id 直接做主键、按它 upsert；不做历史归档，
-- 减少节点凭据在库里的留存面。
--
-- content 是机场返回的原始 YAML 全文，含节点凭据（vless uuid / reality 参数），
-- 与 clash_profile_providers.url 同级敏感，只存库不进仓库、不进日志。
--
-- 幂等：IF NOT EXISTS，可安全重复执行。
CREATE TABLE IF NOT EXISTS clash_profile_snapshots (
  provider_id    INTEGER PRIMARY KEY REFERENCES clash_profile_providers(id) ON DELETE CASCADE,
  content        TEXT        NOT NULL,
  node_count     INTEGER     NOT NULL,
  -- type/server/port 三元组的哈希，刻意不含节点名：机场把「剩余流量」「距离下次重置剩余」
  -- 这类信息伪装成节点混在 proxies 里，名字每天变，算进去会天天误报节点变更。
  fingerprint    TEXT        NOT NULL,
  -- subscription-userinfo 响应头原文（流量与套餐到期），仅供后台展示。
  userinfo       TEXT        NOT NULL DEFAULT '',
  -- 最近一次**成功**拉取的时间。可空：新增订阅源后第一次拉取就失败时，这里保持 NULL
  -- 表示"从未成功过"，与"拉过但今天失败了"是两种要区分对待的状态。
  fetched_at     TIMESTAMPTZ,
  -- 指纹最近一次**发生变化**的时间。后台据此提示"需要重新导入客户端配置"。
  changed_at     TIMESTAMPTZ,
  -- 拉取失败不覆盖上面的好数据，只记在这里，保证客户端始终能取到最后一份可用节点。
  last_error     TEXT        NOT NULL DEFAULT '',
  last_error_at  TIMESTAMPTZ
);
