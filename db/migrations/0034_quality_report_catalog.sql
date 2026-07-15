CREATE TABLE IF NOT EXISTS quality_report_daily_sequences (
  report_date DATE PRIMARY KEY,
  last_value INTEGER NOT NULL CHECK (last_value BETWEEN 1 AND 999)
);

CREATE TABLE IF NOT EXISTS quality_report_catalog_items (
  id BIGSERIAL PRIMARY KEY,
  catalog_type TEXT NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  parent_code TEXT,
  description TEXT,
  sort_order INTEGER NOT NULL DEFAULT 100,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(catalog_type, code)
);

CREATE INDEX IF NOT EXISTS idx_quality_report_catalog
  ON quality_report_catalog_items(catalog_type, active, sort_order, id);

CREATE TABLE IF NOT EXISTS quality_subjects (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'custom' CHECK (source = 'custom'),
  subject_type TEXT NOT NULL CHECK (subject_type IN ('raw_material', 'finished_product', 'custom_product')),
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  specification TEXT,
  material_category_code TEXT,
  material_subcategory_code TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE SEQUENCE IF NOT EXISTS quality_custom_subject_code_seq START 1;

ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS system_code TEXT;
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS external_report_no TEXT;
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS subject_source TEXT NOT NULL DEFAULT 'tplus';
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS subject_type TEXT NOT NULL DEFAULT 'finished_product';
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS report_source_code TEXT NOT NULL DEFAULT 'self_inspection';
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS document_type_code TEXT NOT NULL DEFAULT 'test_report';
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS material_category_code TEXT;
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS material_subcategory_code TEXT;
ALTER TABLE quality_reports ADD COLUMN IF NOT EXISTS test_item_codes TEXT[] NOT NULL DEFAULT '{}';

WITH ranked AS (
  SELECT id,
         to_char((created_at AT TIME ZONE 'Asia/Shanghai')::date, 'YYYYMMDD') ||
         lpad(row_number() OVER (
           PARTITION BY (created_at AT TIME ZONE 'Asia/Shanghai')::date
           ORDER BY id
         )::text, 3, '0') AS generated_code
  FROM quality_reports
  WHERE system_code IS NULL
)
UPDATE quality_reports q
SET system_code = ranked.generated_code
FROM ranked
WHERE q.id = ranked.id;

INSERT INTO quality_report_daily_sequences(report_date, last_value)
SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date,
       MAX(right(system_code, 3)::integer)
FROM quality_reports
WHERE system_code ~ '^[0-9]{11}$'
GROUP BY (created_at AT TIME ZONE 'Asia/Shanghai')::date
ON CONFLICT(report_date) DO UPDATE SET
  last_value = GREATEST(quality_report_daily_sequences.last_value, EXCLUDED.last_value);

ALTER TABLE quality_reports ALTER COLUMN system_code SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_reports_system_code
  ON quality_reports(system_code);

ALTER TABLE quality_reports DROP CONSTRAINT IF EXISTS quality_reports_subject_source_check;
ALTER TABLE quality_reports ADD CONSTRAINT quality_reports_subject_source_check
  CHECK (subject_source IN ('tplus', 'custom'));
ALTER TABLE quality_reports DROP CONSTRAINT IF EXISTS quality_reports_subject_type_check;
ALTER TABLE quality_reports ADD CONSTRAINT quality_reports_subject_type_check
  CHECK (subject_type IN ('raw_material', 'finished_product', 'custom_product'));

INSERT INTO quality_report_catalog_items(catalog_type, code, name, parent_code, sort_order) VALUES
('report_source','third_party','委外第三方检测',NULL,10),
('report_source','self_inspection','内部自检',NULL,20),
('report_source','supplier','供应商提供',NULL,30),
('report_source','customer','客户提供或指定',NULL,40),
('report_source','regulatory','监管或认证机构',NULL,50),
('report_source','other','其他来源',NULL,99),
('document_type','test_report','检测报告',NULL,10),
('document_type','coa','COA／批次质量证明',NULL,20),
('document_type','sds','SDS／MSDS',NULL,30),
('document_type','tds','TDS／技术数据表',NULL,40),
('document_type','certificate','合格证／认证证书',NULL,50),
('document_type','declaration','符合性声明',NULL,60),
('document_type','other','其他文档',NULL,99),
('test_item','rohs','RoHS',NULL,10),
('test_item','reach_svhc','REACH／SVHC',NULL,20),
('test_item','pops','POPs',NULL,30),
('test_item','pfas','PFAS',NULL,40),
('test_item','food_contact_cn','食品接触 GB 4806',NULL,50),
('test_item','food_contact_eu','食品接触 EU 10/2011',NULL,51),
('test_item','food_contact_fda','食品接触 FDA',NULL,52),
('test_item','halogen','卤素',NULL,60),
('test_item','phthalates','邻苯二甲酸酯',NULL,70),
('test_item','heavy_metals','重金属',NULL,80),
('test_item','voc','VOC／TVOC',NULL,90),
('test_item','coa_batch','批次理化指标／COA',NULL,100),
('test_item','physical','物理机械性能',NULL,110),
('test_item','color','颜色／色差',NULL,120),
('test_item','mfi','熔融指数',NULL,130),
('test_item','density','密度',NULL,140),
('test_item','moisture','水分',NULL,150),
('test_item','ash','灰分',NULL,160),
('test_item','flame','阻燃／UL94',NULL,170),
('test_item','weathering','老化／耐候',NULL,180),
('test_item','composition','成分／含量／纯度',NULL,190),
('test_item','other','其他检测项目',NULL,999),
('material_category','pigment','色粉／颜料',NULL,10),
('material_category','resin','树脂',NULL,20),
('material_category','additive','助剂',NULL,30),
('material_category','filler','填充材料',NULL,40),
('material_category','masterbatch','母粒／浓缩料',NULL,50),
('material_category','finished','成品／改性材料',NULL,60),
('material_category','other','其他',NULL,999),
('material_subcategory','organic_pigment','有机颜料','pigment',10),
('material_subcategory','inorganic_pigment','无机颜料','pigment',20),
('material_subcategory','titanium_dioxide','钛白粉','pigment',30),
('material_subcategory','carbon_black','炭黑','pigment',40),
('material_subcategory','dye','染料／荧光增白剂','pigment',50),
('material_subcategory','effect_pigment','珠光／金属／效果颜料','pigment',60),
('material_subcategory','pp','PP','resin',10),
('material_subcategory','pe','PE（HDPE／LDPE／LLDPE）','resin',20),
('material_subcategory','abs','ABS','resin',30),
('material_subcategory','ps','PS／HIPS','resin',40),
('material_subcategory','pc','PC','resin',50),
('material_subcategory','pmma','PMMA','resin',60),
('material_subcategory','pa','PA6／PA66','resin',70),
('material_subcategory','pet_pbt','PET／PBT','resin',80),
('material_subcategory','pom','POM','resin',90),
('material_subcategory','asa_san','ASA／SAN','resin',100),
('material_subcategory','tpu_tpe','TPU／TPE／TPEE','resin',110),
('material_subcategory','eva_poe','EVA／POE','resin',120),
('material_subcategory','pvc','PVC','resin',130),
('material_subcategory','biodegradable','PLA／PBS／PBAT 等降解树脂','resin',140),
('material_subcategory','recycled_resin','再生树脂','resin',150),
('material_subcategory','antioxidant','抗氧剂','additive',10),
('material_subcategory','light_stabilizer','光稳定剂／紫外吸收剂','additive',20),
('material_subcategory','flame_retardant','阻燃剂','additive',30),
('material_subcategory','lubricant','润滑剂／脱模剂','additive',40),
('material_subcategory','dispersant','分散剂','additive',50),
('material_subcategory','compatibilizer','相容剂／偶联剂','additive',60),
('material_subcategory','plasticizer','增塑剂','additive',70),
('material_subcategory','antistatic','抗静电剂','additive',80),
('material_subcategory','nucleating','成核剂','additive',90),
('material_subcategory','processing_aid','加工助剂／发泡剂','additive',100),
('material_subcategory','calcium_carbonate','碳酸钙','filler',10),
('material_subcategory','barium_sulfate','硫酸钡','filler',20),
('material_subcategory','talc','滑石粉','filler',30),
('material_subcategory','mica','云母','filler',40),
('material_subcategory','glass_fiber','玻璃纤维／玻璃微珠','filler',50),
('material_subcategory','silica','二氧化硅','filler',60),
('material_subcategory','kaolin','高岭土','filler',70),
('material_subcategory','ath_mdh','氢氧化铝／氢氧化镁','filler',80),
('material_subcategory','color_masterbatch','色母粒','masterbatch',10),
('material_subcategory','additive_masterbatch','功能母粒','masterbatch',20),
('material_subcategory','filler_masterbatch','填充母粒','masterbatch',30),
('material_subcategory','modified_plastic','改性塑料／配混料','finished',10),
('material_subcategory','finished_product','成品','finished',20),
('material_subcategory','sample','样品／试验料','finished',30)
ON CONFLICT(catalog_type, code) DO UPDATE SET
  name = EXCLUDED.name,
  parent_code = EXCLUDED.parent_code,
  sort_order = EXCLUDED.sort_order,
  active = TRUE,
  updated_at = NOW();

COMMENT ON COLUMN quality_reports.system_code IS '系统自动生成的 YYYYMMDDNNN 唯一编号';
COMMENT ON COLUMN quality_reports.external_report_no IS '第三方、供应商或原始文件上的报告编号';
COMMENT ON TABLE quality_report_catalog_items IS '质检报告可扩展分类字典，页面不得硬编码分类';
