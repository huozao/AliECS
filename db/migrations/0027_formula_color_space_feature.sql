INSERT INTO features(code, title, description, url, category, required_permission, status, sort_order)
VALUES (
    'formula_color_space',
    '配方色彩空间',
    '探索配方在不同树脂和添加比例下的实测 Lab 颜色与色差轨迹',
    '/formula/colors/',
    '业务查询',
    'formula.read',
    'active',
    34
)
ON CONFLICT (code) DO UPDATE SET
    title = EXCLUDED.title,
    description = EXCLUDED.description,
    url = EXCLUDED.url,
    category = EXCLUDED.category,
    required_permission = EXCLUDED.required_permission,
    status = EXCLUDED.status,
    sort_order = EXCLUDED.sort_order,
    updated_at = NOW();
