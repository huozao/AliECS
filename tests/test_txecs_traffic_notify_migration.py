from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_txecs_traffic_migration_clones_existing_source_and_routes_without_secret():
    sql = (ROOT / "db/migrations/0061_txecs_traffic_notify.sql").read_text(encoding="utf-8")
    assert "source_key = 'txecs-disk'" in sql
    assert "'txecs-traffic'" in sql
    assert "INSERT INTO notify_routes" in sql
    assert "target_json" in sql
    assert "token_sha256" in sql
    assert "NOT EXISTS" in sql
    assert "NOTIFY_TOKEN" not in sql
