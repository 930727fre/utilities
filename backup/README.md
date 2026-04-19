# backup

Daily backup of flashcard and TwelveReader data to Cloudflare R2 at 04:00.

Uses `sqlite3 .backup` for a safe hot copy, then `rclone copy` to R2. Snapshots older than 30 days are pruned automatically.

R2 layout:
```
flashcard/YYYY-MM-DD/flashcard.db
twelvereader/YYYY-MM-DD/twelve.db
twelvereader/YYYY-MM-DD/books/
```

## R2 API Token

Use an **Account API token** (not User API token) with **Admin Read & Write** permission. User API tokens show "Object Read & Write" in the UI but silently deny writes at the S3 API level.

## Notes

- Both data directories are mounted read-write because SQLite WAL mode requires creating a `.db-shm` file alongside the database even for read operations. `sqlite3 .backup` does not modify the source database.

## Deploy

**bash**
```bash
RCLONE_CONFIG_R2_TYPE=s3 \
RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
RCLONE_CONFIG_R2_ACCESS_KEY_ID=xxx \
RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=xxx \
RCLONE_CONFIG_R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com \
R2_BUCKET=your_bucket \
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
