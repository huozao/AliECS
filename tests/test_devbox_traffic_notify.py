from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_devbox_traffic_route_is_idempotent_and_keeps_target_in_center():
    migration = (ROOT / "db/migrations/0063_devbox_traffic_notify.sql").read_text(encoding="utf-8")
    assert "source_key = 'devbox-traffic'" in migration
    assert "WHERE NOT EXISTS" in migration
    assert '"receive_id":"oc_84d1130542509e374f7ea20c13d11ca4"' in migration
    assert "notify_routes" in migration
