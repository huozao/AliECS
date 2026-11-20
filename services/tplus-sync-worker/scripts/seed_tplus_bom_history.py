import hashlib
import json
from decimal import Decimal
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DATABASE_URL = "postgresql://app:ZXl3Seu44iuuF2@postgres:5432/app"

def calculate_bom_business_hash(raw: dict) -> str:
    code = str(raw.get("Code") or raw.get("code") or "").strip()
    version = str(raw.get("Version") or raw.get("version") or "").strip()
    try:
        produce_qty = str(Decimal(str(raw.get("ProduceQuantity") or 100)).normalize())
    except Exception:
        produce_qty = "100"
    
    children_raw = raw.get("BOMChildDTOs") or raw.get("BOMChilds") or []
    normalized_children = []
    for ch in children_raw:
        inv = ch.get("Inventory") or {}
        c_code = str(ch.get("Code") or inv.get("Code") or "").strip()
        u = ch.get("Unit") or {}
        u_name = str(ch.get("UnitName") or (u.get("Name") if isinstance(u, dict) else u) or "kg").strip()
        req_qty = ch.get("RequiredQuantity") or ch.get("Quantity") or 0
        try:
            qty_str = str(Decimal(str(req_qty)).normalize())
        except Exception:
            qty_str = str(req_qty)
        normalized_children.append({
            "code": c_code,
            "qty": qty_str,
            "unit": u_name
        })
    normalized_children.sort(key=lambda x: x["code"])

    payload = {
        "code": code,
        "version": version,
        "produce_quantity": produce_qty,
        "children": normalized_children
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

def main():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT record_key, record_hash, raw_json, last_sync_run_id, missing_since, last_seen_at FROM tplus_bom_records")
            rows = cur.fetchall()
            print(f"Total records in tplus_bom_records: {len(rows)}")

            inserted_cnt = 0
            updated_records_cnt = 0

            for r in rows:
                raw = r["raw_json"] or {}
                code = str(raw.get("Code") or raw.get("code") or "").strip()
                name = str(raw.get("Name") or raw.get("name") or "").strip()
                version = str(raw.get("Version") or raw.get("version") or "").strip()
                tplus_id = raw.get("ID") or raw.get("id")
                if tplus_id is not None:
                    try:
                        tplus_id = int(tplus_id)
                    except Exception:
                        tplus_id = None

                b_hash = calculate_bom_business_hash(raw)
                is_active = (r["missing_since"] is None)
                archived_at = r["missing_since"]
                created_at = r["last_seen_at"]

                # 插入 tplus_bom_history
                insert_sql = """
                    INSERT INTO tplus_bom_history (
                        bom_code, bom_name, bom_version, tplus_bom_id,
                        revision, business_hash, raw_json, change_type,
                        diff_summary, notes, is_active, source_sync_run_id,
                        created_at, archived_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        1, %s, %s, 'INITIAL',
                        '[]'::jsonb, '系统增量升级初始基线版本', %s, %s,
                        %s, %s
                    ) RETURNING id;
                """
                cur.execute(insert_sql, (
                    code, name, version, tplus_id,
                    b_hash, Jsonb(raw), is_active, r["last_sync_run_id"],
                    created_at, archived_at
                ))
                hist_id = cur.fetchone()["id"]
                inserted_cnt += 1

                # 更新 tplus_bom_records 回连 history_id 与 business_hash
                update_sql = """
                    UPDATE tplus_bom_records
                    SET business_hash = %s,
                        current_revision = 1,
                        history_id = %s
                    WHERE record_key = %s;
                """
                cur.execute(update_sql, (b_hash, hist_id, r["record_key"]))
                updated_records_cnt += 1

            conn.commit()
            print(f"Successfully seeded tplus_bom_history: {inserted_cnt} rows inserted.")
            print(f"Successfully updated tplus_bom_records: {updated_records_cnt} rows linked.")

if __name__ == "__main__":
    main()
