from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_0016_migration_registers_rebuild_without_reusing_old_numbers():
    migration = ROOT / "db" / "migrations" / "0016_couple_memory_rebuild.sql"

    sql = migration.read_text(encoding="utf-8")

    assert "0016_couple_memory_rebuild" in sql
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in sql
    assert "ADD COLUMN IF NOT EXISTS thumbnail_url" in sql
    assert "ADD COLUMN IF NOT EXISTS exif" in sql
    assert "idx_share_links_token" in sql
    assert "ON CONFLICT(version) DO NOTHING" in sql


def test_local_compose_exposes_couple_storage_and_immich_env():
    compose = (ROOT / "local" / "docker-compose.local.yml").read_text(encoding="utf-8")

    assert "STORAGE_DRIVER:" in compose
    assert "LOCAL_UPLOAD_DIR:" in compose
    assert "MAX_UPLOAD_MB:" in compose
    assert "WEBDOCK_PHOTO_BASE_URL:" in compose
    assert "IMMICH_ENABLED:" in compose
    assert "IMMICH_PROXY_MODE:" in compose
    assert "uploads:/app/uploads" in compose
    assert "uploads:" in compose


def test_runtime_example_uses_persistent_upload_dir():
    runtime = (ROOT / "deploy" / "ecs" / "runtime.env.example").read_text(encoding="utf-8")

    assert "LOCAL_UPLOAD_DIR=/app/uploads" in runtime
    assert "IMMICH_ENABLED=false" in runtime
    assert "WEBDOCK_PHOTO_BASE_URL=http://host.docker.internal:11800" in runtime


def test_detail_page_hides_immich_picker_when_disabled_and_can_search_when_enabled():
    html = (ROOT / "services" / "public-web" / "memories" / "detail.html").read_text(encoding="utf-8")

    assert 'id="immichCard"' in html
    assert "/v1/immich/status" in html
    assert "/v1/immich/assets?" in html
    assert "immichCard').classList.add('hidden')" in html
    assert "data-immich-bind" in html


def test_detail_page_has_in_place_memory_editor_and_batch_media_picker():
    html = (ROOT / "services" / "public-web" / "memories" / "detail.html").read_text(encoding="utf-8")

    assert 'id="mediaPickerDialog"' in html
    assert 'id="pickerSource"' in html
    assert "data-picker-asset" in html
    assert "data-photo-del" in html
    assert "data-cover-local" in html
    assert "data-cover-immich" in html
    assert "cover_photo_url" in html
    assert "绑定已选" in html
    assert 'value="all">全部照片（默认）' in html
    assert "renderPickerAlbumOptions" in html


def test_couple_dashboard_immich_picker_supports_all_user_albums():
    html = (ROOT / "services" / "public-web" / "couple" / "index.html").read_text(encoding="utf-8")

    assert 'value="all">全部照片（默认）' in html
    assert 'value="personal">个人库（全部）' in html
    assert "renderImmichAlbumOptions" in html
    assert "source.startsWith('album:')" in html
    assert "await api('/v1/immich/albums')" in html


def test_map_reuses_bound_immich_gps_when_memory_has_no_coordinates():
    router = (ROOT / "services" / "backend-api" / "app" / "routers" / "couple.py").read_text(encoding="utf-8")

    assert "THEN m.latitude ELSE COALESCE(asset.latitude, local_geo.latitude)" in router
    assert "FROM couple_memory_assets cma" in router
    assert '"coordinate_source": row[8]' in router
    assert "config = _user_immich_config(user_id)" in router


def test_memory_photo_count_includes_immich_bindings():
    router = (ROOT / "services" / "backend-api" / "app" / "routers" / "couple.py").read_text(encoding="utf-8")

    assert "SELECT COUNT(*) FROM couple_memory_assets cma WHERE cma.memory_id = m.id" in router
    assert 'get("/v1/immich/assets/{asset_id}/original")' in router
