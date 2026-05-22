#!/bin/sh
set -e

DATE=$(TZ=Asia/Taipei date +%Y-%m-%d)
START=$(date +%s)
STEP="init"

notify() {
    wget -qO- "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=$1" > /dev/null || true
}

trap 'notify "Backup+FAILED+at+${STEP}+on+${DATE}"' EXIT

echo "[$(date)] Starting backup for tools: ${TOOLS}"

for TOOL in $TOOLS; do
    STEP="${TOOL}:setup"
    DATA="/tools/${TOOL}/data"
    STAGING="/tmp/${TOOL}-staging"
    TARBALL="/tmp/${TOOL}.tar.gz"

    if [ ! -d "$DATA" ]; then
        echo "Error: ${DATA} not mounted (check docker-compose volumes for tool '${TOOL}')" >&2
        exit 1
    fi

    rm -rf "$STAGING" "$TARBALL"
    mkdir -p "$STAGING"

    STEP="${TOOL}:sqlite"
    find "$DATA" -type f -name "*.db" 2>/dev/null | while IFS= read -r db; do
        rel=${db#$DATA/}
        target="$STAGING/$rel"
        mkdir -p "$(dirname "$target")"
        sqlite3 "$db" ".backup '$target'"
    done

    STEP="${TOOL}:copy"
    find "$DATA" -type f ! -name "*.db" ! -name "*.db-wal" ! -name "*.db-shm" 2>/dev/null | while IFS= read -r f; do
        rel=${f#$DATA/}
        target="$STAGING/$rel"
        mkdir -p "$(dirname "$target")"
        cp "$f" "$target"
    done

    STEP="${TOOL}:tar"
    tar -czf "$TARBALL" -C "$STAGING" .

    STEP="${TOOL}:rclone+copy"
    rclone copyto "$TARBALL" "r2:${R2_BUCKET}/${TOOL}/${DATE}/data.tar.gz"

    SIZE=$(du -h "$TARBALL" | cut -f1)
    echo "[$(date)] ${TOOL}: ${SIZE} uploaded"

    rm -rf "$STAGING" "$TARBALL"
done

STEP="rclone+prune"
rclone delete "r2:${R2_BUCKET}/" --min-age 30d --rmdirs

ELAPSED=$(( $(date +%s) - START ))
TOOLS_ENC=$(echo "$TOOLS" | tr ' ' '+')

trap - EXIT
notify "Backup+done+${DATE}+%7C+${TOOLS_ENC}+%7C+${ELAPSED}s"

echo "[$(date)] Done"
