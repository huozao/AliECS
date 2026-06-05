ALTER TABLE integration_reconciliation_diffs
    ADD COLUMN IF NOT EXISTS resolution_json JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE features
SET title = '原材料库存',
    description = '原材料库存查询',
    url = '/inventory/raw-materials/',
    status = 'active',
    sort_order = 10,
    updated_at = NOW()
WHERE code = 'raw_inventory';

UPDATE features
SET title = '成品库存',
    description = '成品库存查询',
    url = '/inventory/finished-goods/',
    status = 'active',
    sort_order = 20,
    updated_at = NOW()
WHERE code = 'finished_inventory';

UPDATE features
SET title = '系统配方',
    description = '配方检索、BOM 同步与成本核算',
    url = '/formula/',
    status = 'active',
    sort_order = 30,
    updated_at = NOW()
WHERE code = 'formula_query';

UPDATE features
SET sort_order = 40, updated_at = NOW()
WHERE code = 'new_model_form';

UPDATE features
SET sort_order = 50, updated_at = NOW()
WHERE code = 'schedule_form';

UPDATE features
SET sort_order = 60, updated_at = NOW()
WHERE code = 'pending_return_alert';

UPDATE features
SET sort_order = 70, updated_at = NOW()
WHERE code = 'naming_form';

UPDATE features
SET sort_order = 80, updated_at = NOW()
WHERE code = 'qc_form';

UPDATE features
SET sort_order = 90, updated_at = NOW()
WHERE code = 'density_calculator';

UPDATE features
SET sort_order = 100, updated_at = NOW()
WHERE code = 'midea_requirement';
