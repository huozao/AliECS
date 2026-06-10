-- 企业微信智能表格 doc 级登记（来源：本机 peifangpaichan/smartsheet_registry.json，2026-05-15 已验证 profile 归属）。
-- worker 每轮全量会对这些 docid 调 get_sheet 展开同步其下全部 sheet（source_type='smartsheet_doc'，sheet_id 留空）。
-- 幂等：ON CONFLICT 按 (provider, env_profile, external_doc_id, external_sheet_id) 更新名称与状态。
-- 手动执行（不会被 migrate.sh 自动跑）：psql -U app -d app -f wecom_doc_sources.sql

INSERT INTO external_sources(provider, env_profile, source_name, source_type, external_doc_id, external_sheet_id, source_url, document_name, sheet_name, status, updated_at)
VALUES
  -- COMPANY_A（6 个文档）
  ('wecom', 'COMPANY_A', '系统对接智能表格', 'smartsheet_doc', 'dcstsb4qAkWAdtJXO7IVl0ZOOmr012qaxIMUcB05QiHFzCFE4Ic2_ov-_GIzhGRCNuLOTMh0pvSnD9d5wV_8zCfw', '', 'https://doc.weixin.qq.com/smartsheet/s3_AHAAXxTrAKECNkXnbX5OARAuNKrKM_a', '系统对接智能表格', '', 'active', NOW()),
  ('wecom', 'COMPANY_A', '生产任务排期', 'smartsheet_doc', 'dcR8ljX3Jcc_JNCUvOhgORDKngBKGseZNjDkLDOeKuIpMcJd-XQIdRoga5kIwmjVr1mxMBj41G7IeK5OjUARAcEg', '', 'https://doc.weixin.qq.com/smartsheet/s3_AHAAXxTrAKECNh4S19kwAQnuJ7Bh4_a', '生产任务排期', '', 'active', NOW()),
  ('wecom', 'COMPANY_A', '销售配方', 'smartsheet_doc', 'dcdX_2Dw-8T-dIPwYOTnnwYvD9WrUkjEO2_pssU7Ovs2_LcN6qyu0jRcOoM6mwV3FswaMJ6d2TbA7CmBHqrZrgZA', '', 'https://doc.weixin.qq.com/smartsheet/s3_AHAAXxTrAKECNOcqIjgBLQqGttmRg_a', '销售配方', '', 'active', NOW()),
  ('wecom', 'COMPANY_A', '经典语录', 'smartsheet_doc', 'dcvnKJ16Ku2nd3_hr5ckND5vJpXR82MaxkozprKnuHnEmW_OYc8LXU1UKvyyc93Ql3tFokOF59crAKdvHCyrSckg', '', 'https://doc.weixin.qq.com/smartsheet/s3_AHAAXxTrAKECNN4FR5oxFS40ZZh9T_a', '经典语录', '', 'active', NOW()),
  ('wecom', 'COMPANY_A', '点餐表', 'smartsheet_doc', 'dc4FKvxJ2NQSr09HapGMfNjFNTF4quoj-4OZrzVP-cxputwmm93HjozegNQ69cLlNJ9zj6HzVNIV6LuOgitSU84w', '', 'https://doc.weixin.qq.com/smartsheet/s3_AHAAXxTrAKECNHjQ37McsSqOkHZ5F_a', '点餐表', '', 'active', NOW()),
  -- registry 中 doc_name 为空（含 选单录单/公开的生产记录表 两个 sheet），起描述性名
  ('wecom', 'COMPANY_A', '选单与生产记录', 'smartsheet_doc', 'dcIaiO310zScyhZ03WmOwMC8sG_mACVtS8t8mcw1IQNvlzmgZgPZj68r1ISis2NXr7Cq0jaWFnMcr5PPCoz4H8mQ', '', '', '选单与生产记录', '', 'active', NOW()),
  -- COMPANY_B（6 个文档）
  ('wecom', 'COMPANY_B', '色粉使用记录表', 'smartsheet_doc', 'dc3XjaI5HXMMsChskgZz5NJpEl_AXVao9nTMMDO24YZYe-kYOSCm7I7QKpCq1NoDwSCUyX2GKdi5EGCqgK0Kj0AA', '', 'https://doc.weixin.qq.com/smartsheet/s3_ACcA4BQeAKQCN0KzcV5UrRh0NV25w_a', '色粉使用记录表', '', 'active', NOW()),
  ('wecom', 'COMPANY_B', '案例表1', 'smartsheet_doc', 'dcstUeCnf6ZKuC0UvGAYVijkrIlkXwdv1SqKcTV1TrWOWe6Nr5_3WPs_L8U2VPca0mzwHl-rQOAWJWB1L0ViPf3g', '', 'https://doc.weixin.qq.com/smartsheet/s3_ACcA4BQeAKQCNeXdy0roiSEWNZDNB_a', '案例表1', '', 'active', NOW()),
  ('wecom', 'COMPANY_B', '案例表3', 'smartsheet_doc', 'dcPu9Vpg5oV6A8mfGoXeELMFvvf0zSAUpvMimW7_j7DXn2PVk4XHzmXFP72HnhemuBipfbNp37oC1-TN1DvQ2Y9w', '', 'https://doc.weixin.qq.com/smartsheet/s3_ACcA4BQeAKQCNdYo51skNTdeId1Gc_a', '案例表3', '', 'active', NOW()),
  ('wecom', 'COMPANY_B', '案例表4', 'smartsheet_doc', 'dc5XhpUpLkivEnuFHzcpVrke97JTdjYQXXSJgKhAHmLmULGVMQKYty6zsKmXf0mHaVNrbml-8LeS5M190T6LwGqg', '', 'https://doc.weixin.qq.com/smartsheet/s3_ACcA4BQeAKQCNbud8YsCCRdaZO1hm_a', '案例表4', '', 'active', NOW()),
  -- registry 真名也是"案例表4"（含点检计划/点检明细），加后缀区分
  ('wecom', 'COMPANY_B', '案例表4(点检)', 'smartsheet_doc', 'dcMYrbhrvi4LUs-cLSFmA5-AOPF9g7XX3Z567tLQi7I6aKtpvSLWueP7IbZfejC3vaVWbUboBDKeFGpYhaonw8NQ', '', 'https://doc.weixin.qq.com/smartsheet/s3_ACcA4BQeAKQCNOhkVeUDyS7K0yJF5_a', '案例表4(点检)', '', 'active', NOW()),
  ('wecom', 'COMPANY_B', '测试', 'smartsheet_doc', 'dcz0fCEddim4HT1rtsWgLgIM7lQFDw7NSd1X4hvfqyPSO4wP0tUsFS8dknedIKNDXdOX6QUScrkNKNd0RWkURr1g', '', 'https://doc.weixin.qq.com/smartsheet/s3_ACcA4BQeAKQCNcZHidTg1TKaThVyZ_a', '测试', '', 'active', NOW())
ON CONFLICT(provider, env_profile, external_doc_id, external_sheet_id)
DO UPDATE SET
  source_name = EXCLUDED.source_name,
  source_type = EXCLUDED.source_type,
  source_url = EXCLUDED.source_url,
  document_name = EXCLUDED.document_name,
  status = 'active',
  updated_at = NOW();

-- 备注：registry 中另有 manual_link 文档 s3_AHAAXxTrAKECNUdzDUxzETZ0E06P0_a（四川矗鑫贸易），
-- 两个 profile 均 invalid_docid（s3 分享链接 ID 非 API docid），无法经 API 同步，故不登记。
