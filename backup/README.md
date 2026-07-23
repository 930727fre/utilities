# backup

Daily backup of tool data directories to Cloudflare R2 at 04:00 Asia/Taipei.

For each configured tool, the entire `<tool>/data/` directory is snapshotted (SQLite files via `sqlite3 .backup`, everything else copied as-is), tarred, gzipped, and uploaded. Snapshots older than 30 days are pruned automatically.

R2 layout:
```
<tool>/YYYY-MM-DD/data.tar.gz
```

## Adding a tool

To back up a new tool's `data/` directory:

1. Add a volume mount in `docker-compose.yml` — must use the exact path `/tools/<tool>/data` inside the container:
   ```yaml
   volumes:
     - ../flashcard/data:/tools/flashcard/data
     - ../free2speak/data:/tools/free2speak/data   # new
   ```
2. Add the tool name to the `TOOLS` env var (space-separated):
   ```yaml
   environment:
     - TOOLS=flashcard free2speak
   ```
3. Rebuild: `docker compose up -d --build`.

`*.db` files are snapshotted via `sqlite3 .backup` (safe while the source app is running). `*.db-wal` and `*.db-shm` are skipped — they're regenerable WAL artifacts. Everything else is plain copied.

## R2 API Token

Use a **User API token** with **Admin Read & Write** permission. "Object Read & Write" silently denies writes at the S3 API level.

## Notes

- Data directories are mounted read-write because SQLite WAL mode requires creating a `.db-shm` file alongside the database even for read operations. `sqlite3 .backup` does not modify the source database.
- marker-pipeline outputs are downloaded zips, not persistent state — not backed up here. keyboard outputs are similarly reproducible from source (vocab list re-edit) and intentionally excluded.
- transcribe is a partial backup: only `data/archive/` (canonical SRT store) goes up as `transcribe-archive`. `data/bt/` videos are large and re-fetchable from the original torrents; `data/artifact/` shares inodes with `data/bt/` via bt_filter's hardlinks (not a separate state); `_sources/` is intermediate pipeline cache. Archive is the only irrecoverable slice — each SRT there cost a Sonnet annotation pass (~$0.05), so re-annotating a full library would be tens of dollars.

## Deploy

**bash**
```bash
RCLONE_CONFIG_R2_TYPE=s3 \
RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
RCLONE_CONFIG_R2_ACCESS_KEY_ID=xxx \
RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=xxx \
RCLONE_CONFIG_R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com \
R2_BUCKET=your_bucket \
TELEGRAM_BOT_TOKEN=xxx \
TELEGRAM_CHAT_ID=xxx \
docker compose up -d --build
```

**PowerShell**
```powershell
$env:RCLONE_CONFIG_R2_TYPE="s3"
$env:RCLONE_CONFIG_R2_PROVIDER="Cloudflare"
$env:RCLONE_CONFIG_R2_ACCESS_KEY_ID="xxx"
$env:RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="xxx"
$env:RCLONE_CONFIG_R2_ENDPOINT="https://<account_id>.r2.cloudflarestorage.com"
$env:R2_BUCKET="your_bucket"
$env:TELEGRAM_BOT_TOKEN="xxx"
$env:TELEGRAM_CHAT_ID="xxx"
docker compose up -d --build
```

## Test

Run the backup script immediately without waiting for 04:00:

```bash
docker compose run --rm backup /backup.sh
```

Verify the R2 bucket:

```bash
docker compose run --rm backup rclone ls r2:${R2_BUCKET}
```

## Restore

Interactive script on the host — pick a tool + snapshot from numbered menus. **Stop dependent services for the tool first** — the script writes to the live path.

```bash
./restore.sh
```

Two modes depending on which tool you pick:

- `flashcard` / `free2speak` → **wipe & replace** the tool's whole `data/` directory
- `transcribe-archive` → **pick one title** and restore just that title into `arr/data/archive/`

Restored files are owned by root (rclone runs in a container as root). If the consuming service runs as a non-root user, `chown -R <uid>:<gid> <tool>/data/` afterward. Restart your services after the restore completes.
