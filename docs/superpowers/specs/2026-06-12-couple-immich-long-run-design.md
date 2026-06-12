# Couple Immich Long-Run Design

## Goal

Build Couple Memory as a thin, maintainable business layer on top of an independently maintained Immich photo system. Codex should be able to execute the rollout for a long time without stopping for ordinary technical choices, while stopping at explicit safety boundaries that could lose data, expose secrets, or break production access.

## Source Constraints

- Immich remains an upstream-managed application. Do not fork Immich, patch its source, or copy Immich code into AliECS.
- Use the official Immich Docker Compose deployment path for production. Immich documentation describes Docker Compose as the recommended production method.
- Serve Immich from a dedicated root subdomain such as `immich.hydwang.xyz`. Immich does not support being served from a sub-path such as `/immich`.
- Treat Immich database backups and uploaded media backups as separate requirements. Immich database backups do not contain photos or videos.
- Keep Couple Memory's existing product rule: Memory is the core object; photos are supporting material.
- Keep secrets only in `.env`, deployment host environment, or runtime-only secret stores. Do not commit real API keys, passwords, upload paths containing private names, original photos, logs, browser data, or generated backups.

Reference docs:

- https://docs.immich.app/install/docker-compose
- https://docs.immich.app/administration/reverse-proxy
- https://docs.immich.app/administration/backup-and-restore
- https://docs.immich.app/install/environment-variables
- https://docs.immich.app/features/mobile-backup

## Architecture

```text
Phones and computers
  |
  | Immich mobile app / Immich web
  v
Old laptop or NAS: Immich official Docker Compose
  |  - original media
  |  - thumbnails
  |  - EXIF and search indexes
  |  - Immich database
  |
  | private tunnel or internal network
  v
ECS: Nginx and AliECS backend-api
  |  - immich.hydwang.xyz reverse proxy
  |  - ImmichClient API adapter
  |  - Couple Memory tables and permissions
  v
https://hydwang.xyz/couple/
  - memories
  - anniversaries
  - bucket list
  - selected Immich photos attached to memories
```

## Responsibilities

### Immich

Immich owns:

- Original photos and videos.
- Mobile backup and desktop upload.
- Thumbnail generation.
- EXIF metadata, taken time, location, and search indexes.
- Native Immich albums and partner sharing.
- Immich application upgrades.

### Couple Memory

AliECS owns:

- Couple Space and member permissions.
- Memory narrative content.
- Anniversaries.
- Bucket list items.
- Share links for selected memories.
- Binding between a Memory and Immich assets or albums.
- Public/private presentation at `https://hydwang.xyz/couple/`.

AliECS must not become a full photo-management system. It only stores references and small metadata caches.

## Data Model

Prefer adding a dedicated binding table rather than overloading the current `photos` table for every Immich concept.

```sql
CREATE TABLE IF NOT EXISTS couple_memory_assets (
  id BIGSERIAL PRIMARY KEY,
  couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
  memory_id BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  provider TEXT NOT NULL DEFAULT 'immich',
  immich_asset_id TEXT,
  immich_album_id TEXT,
  original_filename TEXT,
  taken_at TIMESTAMPTZ,
  latitude DOUBLE PRECISION,
  longitude DOUBLE PRECISION,
  thumbnail_cache_key TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  selected_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT couple_memory_assets_provider_check CHECK (provider IN ('immich')),
  CONSTRAINT couple_memory_assets_asset_or_album_check CHECK (
    immich_asset_id IS NOT NULL OR immich_album_id IS NOT NULL
  )
);

CREATE INDEX IF NOT EXISTS idx_couple_memory_assets_memory_order
  ON couple_memory_assets(memory_id, sort_order, id);

CREATE INDEX IF NOT EXISTS idx_couple_memory_assets_provider_asset
  ON couple_memory_assets(provider, immich_asset_id);
```

The existing `photos` table can remain for current upload behavior and migration compatibility. New Immich work should read through `couple_memory_assets` first.

## Configuration

Add AliECS runtime variables:

```env
IMMICH_ENABLED=false
IMMICH_BASE_URL=https://immich.hydwang.xyz
IMMICH_API_KEY=
IMMICH_TIMEOUT_SECONDS=20
IMMICH_PROXY_MODE=backend
```

Rules:

- `IMMICH_ENABLED=false` is the safe default.
- `IMMICH_API_KEY` is required only when real Immich API calls are enabled.
- Tests must pass with fake Immich responses and without a real API key.
- Production deploy must fail fast if `IMMICH_ENABLED=true` and `IMMICH_API_KEY` is empty.

## API Boundaries

Create a small `ImmichClient` adapter with only the calls Couple needs:

- `ping()` verifies Immich connectivity.
- `get_asset(asset_id)` returns normalized asset metadata.
- `search_assets(query, taken_after, taken_before)` returns a limited result set for binding UI.
- `get_thumbnail(asset_id)` streams or redirects thumbnail content.
- `get_original(asset_id)` streams original content only for authenticated users with Couple access.

Do not expose raw Immich API keys to the browser. Browser requests go through AliECS backend routes.

Suggested AliECS routes:

- `GET /v1/immich/status`
- `GET /v1/immich/assets/search`
- `POST /v1/memories/{memory_id}/immich-assets`
- `GET /v1/memories/{memory_id}/immich-assets`
- `DELETE /v1/memories/{memory_id}/immich-assets/{binding_id}`
- `GET /v1/immich/assets/{asset_id}/thumbnail`

## UI Scope

The Couple UI should remain a memory product:

- Dashboard shows recent memories, map, gallery preview, anniversaries, and bucket list.
- Memory detail shows narrative content first, then attached Immich photos.
- Binding UI lets the user search/select Immich assets and attach them to a memory.
- Share pages show only assets bound to the shared memory.
- Immich full-library browsing remains in Immich.

## Long-Run Execution Model

Codex should run phases in order and write progress to `docs/ops/couple-immich-handoff.md` after every phase.

Continue automatically when:

- A local test fails because implementation is incomplete and the failure is inside the current planned scope.
- Real Immich credentials are missing but fake-client tests can verify AliECS behavior.
- Remote Immich is not reachable, but local code, tests, docs, and safe deployment artifacts can still be completed.
- A non-critical smoke check fails and a lower-level check proves the failure boundary.

Stop and ask the user only when:

- A command could overwrite or delete existing Immich media or database files.
- A backup check fails and the next step is upgrade, restore, migration, or destructive cleanup.
- `ssh aliecs` or `ssh webdock` fails repeatedly and no configured control path remains.
- Immich first-admin creation, API key creation, or mobile login requires human credentials.
- Nginx or DNS changes would affect existing production routes.
- Git status shows `.env`, original photos, logs, browser data, backups, or `_references` staged for commit.

## Phases

### Phase 0: Read-Only Audit

Purpose: prove current state before touching services.

Outputs:

- Git status for AliECS and webdock.
- SSH reachability for `aliecs` and `webdock`.
- Docker availability and disk space on old laptop.
- Existing Immich directory/service detection.
- Handoff note with risks and selected install path.

### Phase 1: Immich Base Service

Purpose: start Immich independently on the old laptop or NAS.

Outputs:

- Immich compose directory.
- Runtime `.env` on remote host only.
- Healthy Immich containers.
- Local `GET /api/server/ping` success.
- No committed secrets.

### Phase 2: Public Access

Purpose: make Immich reachable at a dedicated root subdomain.

Outputs:

- ECS Nginx reverse proxy for `immich.hydwang.xyz`.
- Large upload support and required forwarded headers.
- `/.well-known/immich` routing preserved where applicable.
- Public `GET /api/server/ping` success.

### Phase 3: AliECS Immich Adapter

Purpose: add a small, testable boundary between Couple and Immich.

Outputs:

- Runtime env examples.
- `ImmichClient`.
- Fake-client tests.
- Status endpoint.
- No browser exposure of API key.

### Phase 4: Couple Binding Model

Purpose: bind Immich assets to memories without importing Immich data ownership.

Outputs:

- Migration for `couple_memory_assets`.
- CRUD routes for binding assets.
- Permission checks tied to Couple Space membership.
- Tests for cross-space denial and missing asset behavior.

### Phase 5: Couple UI Integration

Purpose: make the feature usable from `https://hydwang.xyz/couple/`.

Outputs:

- Memory detail shows bound Immich assets.
- Binding UI can search and attach assets.
- Share page only shows assets bound to that memory.
- Existing WebDock/local photo path remains compatible.

### Phase 6: Maintenance Automation

Purpose: keep Immich easy to update without custom forks.

Outputs:

- Health script.
- Backup preflight script.
- Safe update script.
- Weekly update-check runbook.
- Upgrade does not run when backup preflight fails.

### Phase 7: Final Verification

Purpose: prove the rollout is safe and resumable.

Outputs:

- AliECS unit tests.
- webdock tests where touched.
- Compose config checks.
- Nginx config test where changed.
- Browser smoke for Couple pages if local or public target is reachable.
- Handoff note with completed work, blocked items, commands run, and next action.

## Verification Standard

No phase is complete until it has:

- A command or browser check proving the main behavior.
- A handoff update.
- A clear statement of remaining risk.
- No accidental secret or media staging.

## Prompting Codex

Use this design with the implementation plan in:

`docs/superpowers/plans/2026-06-12-couple-immich-long-run.md`

The operator prompt should tell Codex to execute continuously, follow the hard stop conditions, and update the handoff note after each phase.
