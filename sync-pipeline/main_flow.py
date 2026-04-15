"""Future Sync 主流程骨架（阶段一可执行形态）。"""


def run_sync_once() -> dict[str, int]:
    raw_records = fetch_data()
    stage_records = normalize_records(raw_records)
    upserted_count = upsert_domain(stage_records)

    if not writeback_guard(stage_records):
        return {"raw": len(raw_records), "stage": len(stage_records), "upserted": upserted_count, "writeback": 0}

    writeback_count = apply_writeback(stage_records)
    return {
        "raw": len(raw_records),
        "stage": len(stage_records),
        "upserted": upserted_count,
        "writeback": writeback_count,
    }


def fetch_data() -> list[dict[str, str]]:
    return [{"external_id": "demo-1", "name": "demo"}]


def normalize_records(raw_records: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in raw_records:
        normalized.append(
            {
                "external_id": row.get("external_id", ""),
                "name": row.get("name", "").strip(),
            }
        )
    return normalized


def upsert_domain(stage_records: list[dict[str, str]]) -> int:
    return len(stage_records)


def writeback_guard(stage_records: list[dict[str, str]]) -> bool:
    _ = stage_records
    return False


def apply_writeback(stage_records: list[dict[str, str]]) -> int:
    _ = stage_records
    return 0
