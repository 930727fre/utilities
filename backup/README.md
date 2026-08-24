# backup

Daily backup of tool data directories to a friend's Tailscale NAS at 04:00 Asia/Taipei via **restic** (content-addressable dedup + encryption).

For each configured tool, the entire `<tool>/data/` directory is snapshotted (SQLite files via `sqlite3 .backup`, everything else copied as-is) into a staging area, then `restic backup`ed into the shared repo. Restic dedups against every prior snapshot chunk-by-chunk, so 90-day retention across 5 tools costs roughly base-repo-size + delta chunks, not `snapshots × tarball_size`.

Repo layout on NAS: `nas:restic/` (plain WebDAV path, restic encrypts contents). Retention: 90 daily snapshots per tool (`--group-by tag --keep-daily 90 --prune`).

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

## Setup — rclone.conf

Restic uses rclone as its backend (`RESTIC_REPOSITORY=rclone:nas:restic/`); rclone reads `${HOME}/rclone.conf` on the host, bind-mounted read-only into the container. **This file is shared with `homelab/rclone`**; if you haven't set it up yet, see the setup section in `homelab/rclone/README.md`.

This container uses the plain `[nas]` remote (not `[nas-crypt]`) — restic does its own AES-256 encryption, layering rclone-crypt on top would just add CPU for no security gain.

## Setup — RESTIC_PASSWORD

Restic encrypts the entire repo with a password of your choosing. **Losing this password = losing all snapshots.** There's no recovery mechanism (same failure mode as rclone crypt).

Generate one and store in Bitwarden:

```bash
openssl rand -base64 32
```

Then export in shell before `docker compose up`:

```bash
export RESTIC_PASSWORD='<the value from Bitwarden>'
```

First `docker compose up` triggers an implicit `restic init` inside `backup.sh` (the check is `restic snapshots >/dev/null 2>&1 || restic init`). Idempotent — later runs skip init.

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

Inspect the repo:

```bash
# All snapshots
docker compose run --rm backup restic snapshots

# Snapshots for one tool
docker compose run --rm backup restic snapshots --tag jellyfin

# Diff two snapshots (spot what changed)
docker compose run --rm backup restic diff <snap_id_a> <snap_id_b>

# Repo stats (see how well dedup is working)
docker compose run --rm backup restic stats

# Mount the repo as a FUSE filesystem to browse historical files
# without a full restore (needs privileged mode; ad-hoc peek):
docker compose run --rm --privileged backup \
    sh -c 'mkdir -p /mnt/r && restic mount /mnt/r'
```

## Restore

Interactive script on the host — pick a tool + snapshot from numbered menus. **Stop dependent services for the tool first** — the script writes to the live path.

```bash
./restore.sh
```

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
