#!/bin/sh
set -e

DATE=$(date +%Y-%m-%d)

echo "[$(date)] Starting backup"

# flashcard
sqlite3 /flashcard/flashcard.db ".backup /tmp/flashcard.db"
rclone copy /tmp/flashcard.db "r2:${R2_BUCKET}/flashcard/${DATE}/"
rm /tmp/flashcard.db

# twelvereader
sqlite3 /twelvereader/twelve.db ".backup /tmp/twelve.db"
rclone copy /tmp/twelve.db "r2:${R2_BUCKET}/twelvereader/${DATE}/"
rm /tmp/twelve.db
rclone copy /twelvereader/books/ "r2:${R2_BUCKET}/twelvereader/${DATE}/books/"

# prune snapshots older than 30 days
rclone delete "r2:${R2_BUCKET}/" --min-age 30d --rmdirs

echo "[$(date)] Done"
