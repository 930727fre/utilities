#!/bin/sh
set -e

DATE=$(TZ=Asia/Taipei date +%Y-%m-%d)
START=$(date +%s)
STEP="init"

notify() {
    wget -qO- "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=$1" > /dev/null || true
}

trap 'notify "Backup+FAILED+at+${STEP}+on+${DATE}"' EXIT

echo "[$(date)] Starting backup"

STEP="sqlite"
sqlite3 /flashcard/flashcard.db ".backup /tmp/flashcard.db"

STEP="rclone+copy"
rclone copy /tmp/flashcard.db "r2:${R2_BUCKET}/flashcard/${DATE}/"

SIZE=$(du -h /tmp/flashcard.db | cut -f1)
rm /tmp/flashcard.db

STEP="rclone+prune"
rclone delete "r2:${R2_BUCKET}/" --min-age 30d --rmdirs

ELAPSED=$(( $(date +%s) - START ))

trap - EXIT
notify "Backup+done+${DATE}+%7C+${SIZE}+%7C+${ELAPSED}s"

echo "[$(date)] Done"
