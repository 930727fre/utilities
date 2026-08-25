# immich

Self-hosted Google Photos replacement — family iPhone/Android photo backup with proper timeline browsing, face recognition, object detection, map view, and album sharing. Replaces Nextcloud after Nextcloud's Photos UX (even patched with libheif/ffmpeg for thumbnails) proved insufficient for a photo-heavy family use case (Imagick edge cases with certain JPG/PNG files, no smart album, basic map).

**Photos + video only** — Immich doesn't handle generic files (PDF/zip/docs). Nextcloud is being retired; a replacement for generic-file storage will be picked separately later.

## Architecture

Three containers:

- **immich-server** — API + web UI + background workers (thumbnail gen, metadata extraction, sidecar sync). libvips inside handles image decode for HEIC/PNG/JPEG/etc, ffmpeg for video thumbs.
- **redis** — job queue (Bull), ephemeral cache. No backup needed.
- **database** — Postgres 14 + pgvecto-rs. Immich's schema uses vector columns even when ML worker isn't running, so we keep the vector-capable Postgres image; the columns stay empty without ML producing embeddings.

**ML worker deliberately absent** — family use case is "photo backup + basic browsing", not "AI search". Skipping the `immich-machine-learning` container drops face recognition / object search / CLIP semantic search / duplicate detection, but the trade-off buys: one fewer container, no GPU / persistenced-socket dance, ~500-1000 MB less idle RAM, ~4 GB less disk (no ML model weight downloads). Timeline / thumbnails / upload / album / EXIF-based search / map view all still work.

**After first boot**, disable ML features in the admin UI so Immich stops retrying the absent ML endpoint: **Administration → Settings → Machine Learning → toggle everything off**. Otherwise logs get spammed with "ML unreachable" errors.

To re-enable ML later: add the `immich-machine-learning` service back to compose (with `gpus: all` for CUDA acceleration if desired), re-enable in admin UI, ML pipeline picks up from scratch.

Data at `./data/{upload,postgres}/`, gitignored. Backup via `utilities/backup` — needs `pg_dump` special case in backup.sh (postgres live files can't be safely tarred), TBD in a follow-up commit.

**Port 8082** on host (native Immich port is 2283 inside container). LAN-only exposure — no CF Tunnel route. Family uses native iOS/Android app.

## Setup

### 1. Secrets (Bitwarden + shell export)

```bash
export IMMICH_DB_PASSWORD="$(openssl rand -base64 32)"
# DB_USERNAME + DB_DATABASE_NAME default to "postgres" / "immich" —
# no need to override unless you want.
```

Shell-side we use `IMMICH_DB_PASSWORD` to namespace against any other service that also uses a `DB_PASSWORD`. Compose interpolation maps it to `DB_PASSWORD` internally (which is the fixed name Immich's server binary reads).

### 2. Bring up

```bash
cd ~/utilities/immich
docker compose up -d
docker compose logs -f immich-server
# First boot takes 1-2 min: postgres initdb + Immich schema migrations
# + ML worker downloads model weights (~200 MB). Look for
# "Immich Server is listening on port 2283" then Ctrl+C.
```

### 3. First login (admin setup)

Browser → `http://<host-lan-ip>:8082` → Immich shows an "Initial Admin Registration" screen (there's no default admin, first visitor becomes admin). Fill in your email + password.

### 4. Create family user accounts

Admin panel → **Administration → Users → Create user**. For each family member: name, email, password, storage quota (optional; unlimited by default at this scale).

Hand out passwords out-of-band.

### 5. Family app setup

Each family member on their phone:

1. Install **Immich** app (iOS App Store / Google Play)
2. Add server URL: `http://<host-lan-ip>:8082`
3. Log in with their credentials
4. Enable **Auto Upload** — Immich preserves original format (HEIC / HEVC unchanged, no Safari-style transcode). Choose which albums to sync (usually "All Photos" or "Camera Roll").
5. First background sync will queue all their photos. Depending on library size + Wi-Fi, hours to days for a large iPhone library.

## Ops

**Restart / update:**
```bash
docker compose pull
docker compose up -d
```
Immich handles its own DB schema migrations on startup. But **read the release notes** before major-version bumps — Immich is actively developed and occasionally breaks backwards compatibility.

**Postgres CLI:**
```bash
docker compose exec database psql -U postgres -d immich
```

**Storage usage:**
```bash
du -sh data/{upload,postgres,model-cache}
```

**ML jobs status:** Admin panel → Jobs. Manually trigger face-detection re-scan, thumbnail regeneration, etc.

## Backup (deferred)

Handled by `utilities/backup` (restic to friend's NAS). Immich uses postgres so backup.sh needs a special case:

1. Before restic snapshot: `docker exec immich_postgres pg_dump -U postgres -d immich > /tmp/immich-postgres.sql`
2. Include the .sql dump in the tarball
3. Exclude `postgres/` live data dir from the copy (would be inconsistent + huge)
4. `upload/` gets copied straight (photos are immutable once written; restic dedup handles the growing library efficiently)

TBD — follow-up commit will wire this into `utilities/backup/backup.sh`.

## Disk-watchdog (deferred)

`homelab/disk-watchdog` currently freezes Nextcloud uploads via OCS quota API. That whole branch will be reworked or removed once Nextcloud is gone; Immich has its own per-user storage quota via `/api/user/admin/{id}` — same pattern (iterate non-admin users, set quota to 0 on freeze, restore on thaw).

Until wired, unset `NEXTCLOUD_ADMIN_PASSWORD` in your shell so the watchdog skips the (now-dead) Nextcloud branch.

## Nextcloud migration (one-off)

Nextcloud is being retired. Family's Nextcloud photos were test uploads only, safe to discard:

```bash
cd ~/utilities/nextcloud
docker compose down                            # stop 1 container
cd ..
sudo rm -rf nextcloud/                         # data/ is root-owned
unset NEXTCLOUD_ADMIN_USER NEXTCLOUD_ADMIN_PASSWORD NEXTCLOUD_TRUSTED_DOMAINS
# Bitwarden: mark Nextcloud secrets deprecated / delete
```

Family uninstalls Nextcloud app, installs Immich app. Photos re-upload from scratch (they were all recent test uploads — nothing lost that matters).

## Generic file storage (deferred)

Immich is photo/video only. Nextcloud was doubling as generic-file storage for the user's own PDFs / zips / docs. Replacement not yet picked — options:

- **filebrowser** — small web UI, LAN-only, single container. User's own uploads (not iPhone Photos) don't hit the Safari transcode issue that motivated the Nextcloud pivot in the first place.
- **rsync + host filesystem** — user just uses host directories, no UI. Backed up as part of existing `utilities/backup` if we add the path.
- **Syncthing** — P2P sync between user's devices, no central server.

Decision pending. For now no replacement — user can put personal files under `~/homelab/data/` or similar host path if they need to.
