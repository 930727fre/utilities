# backup

Daily backup of flashcard data to Cloudflare R2 at 04:00 Asia/Taipei.

Uses `sqlite3 .backup` for a safe hot copy, then `rclone copy` to R2. Snapshots older than 30 days are pruned automatically.

R2 layout:
```
flashcard/YYYY-MM-DD/flashcard.db
```

## R2 API Token

Use a **User API token** with **Admin Read & Write** permission. "Object Read & Write" silently denies writes at the S3 API level.

## Notes

- The flashcard data directory is mounted read-write because SQLite WAL mode requires creating a `.db-shm` file alongside the database even for read operations. `sqlite3 .backup` does not modify the source database.
- marker-pipeline outputs are downloaded zips, not persistent state — not backed up here. transcribe / keyboard outputs are similarly reproducible from source (YouTube URL re-pull, vocab list re-edit) and intentionally excluded.

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

Pull a snapshot from R2 directly into the live `flashcard.db` slot. **Stop all dependent services first** (flashcard, etc.) — the script writes to the live path.

List available snapshots:

```bash
docker compose run --rm backup rclone lsd r2:${R2_BUCKET}/flashcard/
```

Before pulling, manually remove the existing `flashcard.db` on the host (or move it aside for rollback):

```bash
cd flashcard/data
mv flashcard.db flashcard.db.bak
```

If a service ever crashed mid-write, you may also see `flashcard.db-wal` and `flashcard.db-shm` siblings — delete those too. Under clean shutdown they don't exist, so usually you won't see them.

Pull a specific date:

```bash
docker compose run --rm backup /pull.sh 2026-05-21
```

The script will:

1. Refuse if any of `flashcard.db`, `flashcard.db-wal`, `flashcard.db-shm` still exists.
2. Prompt for your host UID and GID — get them by running `id -u && id -g` on the host in another terminal. The pulled file is chowned to those values so the flashcard service can read/write it.
3. Download `r2:${R2_BUCKET}/flashcard/2026-05-21/flashcard.db` to `flashcard/data/flashcard.db`.
4. Run `PRAGMA integrity_check` to confirm the file is a valid SQLite database.

Restart your services after the pull completes.
