# backup

Daily backup of tool data directories to a friend's Tailscale NAS at 04:00 Asia/Taipei, encrypted at rest via `rclone crypt`.

For each configured tool, the entire `<tool>/data/` directory is snapshotted (SQLite files via `sqlite3 .backup`, everything else copied as-is), tarred, gzipped, and uploaded. Snapshots older than 90 days are pruned automatically.

NAS layout (encrypted; only rclone with the crypt password sees this tree):
```
nas-crypt:backups/<tool>/YYYY-MM-DD/data.tar.gz
```

`backups/` sits alongside `homelab/rclone`'s `Movies/` + `TV/` on the same NAS remote — different top-level, no collision.

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
2. Add the tool name to the `TOOLS` env var (space-separated).
3. If the tool's host path isn't `../<tool>/data` (e.g. `keyboard`'s data lives at `keyboard/backend/data`), add a `case` branch in `restore.sh` so the restore target resolves correctly.
4. Rebuild: `docker compose up -d --build`.

`*.db` files are snapshotted via `sqlite3 .backup` (safe while the source app is running). `*.db-wal` and `*.db-shm` are skipped — they're regenerable WAL artifacts. Everything else is plain copied.

## Notes

- Data directories are mounted read-write because SQLite WAL mode requires creating a `.db-shm` file alongside the database even for read operations. `sqlite3 .backup` does not modify the source database.
- transcribe / homelab-side data (jobs.json, downloads, artifact/) intentionally NOT covered here — user manages those manually.

## Setup — rclone.conf

The container reads NAS + crypt config from `${HOME}/rclone.conf` on the host, bind-mounted read-only into the container. **This file is shared with `homelab/rclone`** — same crypt key/salt encrypts both the media library and these tool backups, so one Bitwarden entry protects everything. The file lives outside both repos on purpose: it's a host-wide secret, not a project artifact.

If `~/rclone.conf` doesn't exist yet, create it once (from either `homelab/rclone/config/rclone.conf` if you had one there previously, or by running `rclone config` interactively):

```bash
cp path/to/existing/rclone.conf ~/rclone.conf
chmod 600 ~/rclone.conf
```

It needs two remotes:

- **`nas`** — plain WebDAV pointing at the friend's Tailscale endpoint
- **`nas-crypt`** — crypt remote wrapping the NAS-side top-level directory, e.g. `remote = nas:backup-crypt-data/`. Everything both stacks (media + tool-data backups) write goes under this dir on the NAS.

Container-create dies loudly with `bind source path does not exist` if `~/rclone.conf` isn't there — that's the intended safeguard against silently shipping empty tarballs to nowhere.

Container-create will fail loudly with `bind source path does not exist` if `config/rclone.conf` isn't there — that's the intended behavior (better than shipping an empty tarball to nowhere).

## Deploy

```bash
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
docker compose up -d --build
```

`network_mode: host` is set on the container so rclone can reach `tailscale0` — Docker's default bridge netns can't see the host's Tailscale interface.

## Test

Run the backup script immediately without waiting for 04:00:

```bash
docker compose run --rm backup /backup.sh
```

Verify the NAS:

```bash
docker compose run --rm backup rclone lsf --dirs-only nas-crypt:backups/
docker compose run --rm backup rclone ls nas-crypt:backups/<tool>/
```

## Restore

Interactive script on the host — pick a tool + snapshot from numbered menus. **Stop dependent services for the tool first** — the script writes to the live path.

```bash
./restore.sh
```

All supported tools restore via **wipe & replace** of the tool's whole `data/` directory (or, for jellyfin, its `config/`).

Restored files are owned by root (rclone runs in a container as root). If the consuming service runs as a non-root user, `chown -R <uid>:<gid> <tool>/data/` afterward. Restart your services after the restore completes.
