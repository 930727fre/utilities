# backup

Daily backup of tool data directories (`flashcard`, `free2speak`, `jellyfin`, `marker-pipeline`, `keyboard`, `immich`) at 04:00 Asia/Taipei via **restic** (content-addressable dedup + encryption). Fans out to **two independent offsite tiers**:

| Tier | Path | Retention | Notes |
|---|---|---|---|
| NAS | `rclone:nas:restic/` (friend's Tailscale WebDAV) | infinite for most tools; 90 daily for immich only | Primary. `forget --prune` scoped to tools listed in `NAS_FORGET_TOOLS`. |
| MEGA | `rclone:mega:restic/` (mega.nz free tier, 50 GB) | infinite (all tools) | Second offsite. No `forget`; deltas are tiny (~100 MB/day worst case), multi-year runway on free tier. |

Per-tool NAS retention: immich is the only tool whose data (photos + postgres dump) grows fast enough to warrant a bounded window. SQLite/config tools produce tiny deltas — restic dedup makes infinite retention costs asymptotic to base-repo-size + a rounding error per year.

Both repos share `RESTIC_PASSWORD` (one keychain entry, two storage locations), but chunk keyspaces are separate — no `restic copy` between them; each backup pass pushes staging to both.

For each configured tool, the entire `<tool>/data/` directory is snapshotted (SQLite files via `sqlite3 .backup`, everything else copied as-is) into a staging area, then `restic backup`ed. Restic dedups against every prior snapshot chunk-by-chunk, so retention costs roughly base-repo-size + delta chunks, not `snapshots × tarball_size`.

### MEGA opt-out (`MEGA_EXCLUDE`)

`MEGA_EXCLUDE` in `docker-compose.yml` is a space-separated tool list that skips the MEGA push (NAS still gets it). Currently: `immich` — the photo library is already covered by phone-side originals + host + NAS (3-2-1 satisfied), so paying MEGA space + upload time for a fourth copy is wasteful. Any new tool that lands with a comparable independent-copy story goes here too. Default is empty (include all tools).

## Adding a tool

1. Add a volume mount in `docker-compose.yml` — must use the exact path `/tools/<tool>/data` inside the container:
   ```yaml
   volumes:
     - type: bind
       source: ../newtool/data
       target: /tools/newtool/data
       bind:
         create_host_path: false
   ```
2. Add the tool name to the `TOOLS` env var.
3. If the tool's host path isn't `../<tool>/data` (e.g. `keyboard`'s data lives at `keyboard/backend/data`), add a `case` branch in `restore.sh` so the restore target resolves correctly.
4. Rebuild: `docker compose up -d --build`.

`*.db` files are snapshotted via `sqlite3 .backup` (safe while the source app is running); `.db-wal` / `.db-shm` skipped as regenerable WAL artifacts. Everything else is plain copied into staging.

### Immich exception (Postgres)

Immich uses Postgres (not SQLite). backup.sh has an immich-specific pre-copy step that runs `pg_dump` against `127.0.0.1:5433` (Immich's compose publishes Postgres on loopback specifically for this) and writes `immich-postgres.sql` into the staging dir. The live `postgres/` data dir is excluded from the file copy (`COPY_EXCLUDE=/postgres/`) since raw-copying WAL-in-flight files corrupts them. Restic tarball ends up with `upload/` (photos) + `immich-postgres.sql` (DB snapshot).

**Restore is manual on the Immich side**: `restore.sh` puts files back (including the `.sql`), but you then have to:

```bash
cd ~/utilities/immich
docker compose down
sudo rm -rf data/postgres              # nuke stale schema
docker compose up -d database          # fresh init
sleep 20
docker compose exec -T database psql -U postgres -d immich < data/immich-postgres.sql
docker compose up -d                   # bring server back up
```

Not automated because it's rare + destructive; explicit steps prevent accidents.

## Setup — rclone.conf

Restic uses rclone as its backend (`rclone:<remote>:restic/`); rclone reads `${HOME}/rclone.conf` on the host, bind-mounted read-only into the container. **This file is shared with `homelab/rclone`**; if you haven't set it up yet, see the setup section in `homelab/rclone/README.md`.

Two remotes are required:

- **`[nas]`** — plain WebDAV to friend's Tailscale NAS. Not crypt (restic does its own AES-256; layering rclone-crypt on top would just add CPU for no security gain). Set up per `homelab/rclone/README.md`.
- **`[mega]`** — MEGA.nz account. One-time on the host:
  ```bash
  cd ~/homelab/rclone   # any repo with the rclone image works
  docker compose run --rm rclone config
  # n → mega → user: <MEGA email> → pass: <MEGA password>
  ```
  MEGA CLI login uses email + password directly (no OAuth). Password gets `obscure`d into `rclone.conf` — it's reversible obfuscation, not encryption, so treat `~/rclone.conf` accordingly.

## Setup — RESTIC_PASSWORD

Restic encrypts each repo with a password of your choosing; the same `RESTIC_PASSWORD` unlocks both NAS and MEGA repos here. **Losing this password = losing all snapshots on BOTH repos.** There's no recovery mechanism (same failure mode as rclone crypt).

Generate one and store in Bitwarden:

```bash
openssl rand -base64 32
```

Then export in shell before `docker compose up`:

```bash
export RESTIC_PASSWORD='<the value from Bitwarden>'
```

First `docker compose up` triggers an implicit `restic init` per repo inside `backup.sh` (the check is `restic --repo <URL> snapshots >/dev/null 2>&1 || restic --repo <URL> init`). Idempotent — later runs skip init.

## Deploy

```bash
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
export RESTIC_PASSWORD='...'
docker compose up -d --build
```

`network_mode: host` is set so rclone reaches `tailscale0` — Docker's default bridge netns can't see the host's Tailscale interface.

## Test

Run the backup script immediately without waiting for 04:00:

```bash
docker compose run --rm backup /backup.sh
```

Inspect either repo (`-e RESTIC_REPOSITORY=...` picks which one; falls back to `RESTIC_REPOSITORY_NAS` if you export that at the shell first):

```bash
# All snapshots on NAS
docker compose run --rm -e RESTIC_REPOSITORY=rclone:nas:restic/ backup restic snapshots

# Same on MEGA
docker compose run --rm -e RESTIC_REPOSITORY=rclone:mega:restic/ backup restic snapshots

# Snapshots for one tool
docker compose run --rm -e RESTIC_REPOSITORY=rclone:nas:restic/ backup restic snapshots --tag jellyfin

# Repo stats (see how well dedup is working)
docker compose run --rm -e RESTIC_REPOSITORY=rclone:nas:restic/ backup restic stats

# Mount the repo as a FUSE filesystem to browse historical files
# without a full restore (needs privileged mode; ad-hoc peek):
docker compose run --rm --privileged -e RESTIC_REPOSITORY=rclone:nas:restic/ backup \
    sh -c 'mkdir -p /mnt/r && restic mount /mnt/r'
```

## Restore

Interactive script on the host — pick a source repo (NAS / MEGA), pick a tool, pick a snapshot, done. **Stop dependent services for the tool first** — the script writes to the live path.

```bash
./restore.sh
```

Menu prompts:

1. Source repo (`NAS` or `MEGA`)
2. Tool (enumerated from that repo's snapshot tags)
3. Snapshot (newest first)
4. Confirm

NAS is the go-to for a routine restore (90-day window, tighter to the machine). Fall back to MEGA when NAS is unreachable or gone.

All supported tools restore via **wipe & replace** of the tool's whole `data/` directory (or, for jellyfin, its `config/`). Files are restored to a staging temp dir first and moved into place at the end, so a restic-side error doesn't leave the target half-wiped.

Restored files are owned by root (rclone runs in a container as root). If the consuming service runs as a non-root user, `chown -R <uid>:<gid> <tool>/data/` afterward. Restart your services after the restore completes.

For advanced restore (single file, browse historical state via FUSE, restore to alternate location), fall back to `docker compose run --rm backup restic ...` directly — see the restic docs.

## Migration from the old tarball setup

Previous version of this backup used `rclone copyto` tarballs to `nas-crypt:backups/<tool>/YYYY-MM-DD/data.tar.gz`. After switching to restic:

- Old tarballs at `nas-crypt:backups/` are NOT auto-expired anymore (nothing in this code touches that path). They'll sit at the NAS until manually purged:
  ```bash
  # From homelab/rclone (has [nas-crypt] configured):
  docker compose run --rm rclone purge "nas-crypt:backups/"
  ```
  Do this once you're comfortable that the restic repo is healthy and you've verified a test restore.

- Add `RESTIC_PASSWORD` to your shell rc / password manager — the old R2 env vars (already removed in the previous migration) don't need any handling this round.
