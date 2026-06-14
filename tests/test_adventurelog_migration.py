from __future__ import annotations

from pathlib import Path


def test_memory_to_adventure_basic():
    from scripts.adventurelog.transform import memory_to_adventure

    row = {
        "id": 42,
        "title": "春分礼物",
        "content": "在公园散步。",
        "place_name": "中央公园",
        "lat": 31.23,
        "lng": 121.47,
        "memory_date": "2026-03-20",
        "visibility": "private",
        "tags": ["纪念日", "礼物"],
    }

    adventure = memory_to_adventure(row)

    assert adventure["name"] == "春分礼物"
    assert adventure["latitude"] == 31.23
    assert adventure["longitude"] == 121.47
    assert adventure["location"] == "中央公园"
    assert adventure["visit_date"] == "2026-03-20"
    assert adventure["is_public"] is False
    assert adventure["external_ref"] == "aliecs-memory:42"
    assert "春分礼物" not in adventure["external_ref"]


def test_memory_without_coords_is_skipped():
    from scripts.adventurelog.transform import has_coords

    assert has_coords({"lat": None, "lng": None}) is False
    assert has_coords({"latitude": 31.23, "longitude": 121.47}) is True


class FakeMemorySource:
    def __init__(self, memories, photos_by_memory=None):
        self.memories = memories
        self.photos_by_memory = photos_by_memory or {}

    def iter_memories_with_coords(self):
        return iter(self.memories)

    def photos_for_memory(self, memory_id):
        return list(self.photos_by_memory.get(memory_id, []))


class FakeAdventureLogClient:
    def __init__(self, existing_refs=None):
        self.existing_refs = set(existing_refs or [])
        self.created = []
        self.attached = []

    def list_existing_refs(self):
        return set(self.existing_refs)

    def create_adventure(self, payload):
        self.created.append(payload)
        return {"id": 9000 + len(self.created)}

    def attach_immich_asset(self, adventure_id, asset_id):
        self.attached.append((adventure_id, asset_id))


def test_migrate_dry_run_creates_nothing(tmp_path: Path):
    from scripts.adventurelog.migrate_memories import run_migration

    source = FakeMemorySource(
        [
            {
                "id": 1,
                "title": "A",
                "content": "",
                "place_name": "P",
                "latitude": 1.0,
                "longitude": 2.0,
                "visibility": "private",
                "tags": [],
            }
        ]
    )
    client = FakeAdventureLogClient()

    result = run_migration(source=source, client=client, dry_run=True, report_path=tmp_path / "report.md")

    assert client.created == []
    assert result.created == 0
    assert result.dry_run is True
    assert "dry-run" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_migrate_skips_existing_external_ref(tmp_path: Path):
    from scripts.adventurelog.migrate_memories import run_migration

    source = FakeMemorySource(
        [
            {
                "id": 1,
                "title": "A",
                "latitude": 1.0,
                "longitude": 2.0,
                "visibility": "shareable",
            }
        ]
    )
    client = FakeAdventureLogClient(existing_refs={"aliecs-memory:1"})

    result = run_migration(source=source, client=client, dry_run=False, report_path=tmp_path / "report.md")

    assert client.created == []
    assert result.skipped_existing == 1


def test_migrate_attaches_immich_assets_and_reports_manual_photos(tmp_path: Path):
    from scripts.adventurelog.migrate_memories import run_migration

    source = FakeMemorySource(
        [
            {
                "id": 2,
                "title": "B",
                "latitude": 3.0,
                "longitude": 4.0,
                "visibility": "private",
            }
        ],
        {
            2: [
                {"storage_driver": "immich", "external_asset_id": "asset-1"},
                {"storage_driver": "webdock", "original_filename": "local.jpg"},
            ]
        },
    )
    client = FakeAdventureLogClient()

    result = run_migration(source=source, client=client, dry_run=False, report_path=tmp_path / "report.md")

    assert result.created == 1
    assert client.attached == [(9001, "asset-1")]
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "需人工处理照片" in report
    assert "local.jpg" in report
