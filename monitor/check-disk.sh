#!/bin/sh
# Disk-fill alert for the homelab data root. Runs every 10 min via
# crond. Sends a Telegram ping when /data (bind-mounted from
# ../../homelab/data) is at DISK_THRESHOLD_PCT% or higher.
#
# NO dedup — every over-threshold tick fires. Rationale: at 90%
# threshold, the disk really is close to catastrophe and the nag is
# the point. Every-10-min alerts are tolerable-annoying by design;
# user acts on them, they stop.
set -eu

MOUNT=/data
THRESHOLD="${DISK_THRESHOLD_PCT:-90}"

# df output line: "Filesystem 1K-blocks Used Available Use% Mounted-on"
# Use% column has a trailing '%'; strip and coerce to int for comparison.
PCT=$(df "$MOUNT" | tail -1 | awk '{gsub(/%/,"",$5); print $5+0}')

if [ "$PCT" -lt "$THRESHOLD" ]; then
    exit 0
fi

USED=$(df -h "$MOUNT" | tail -1 | awk '{print $3}')
TOTAL=$(df -h "$MOUNT" | tail -1 | awk '{print $2}')
TEXT="⚠️ homelab disk ${PCT}% full (${USED}/${TOTAL}) — cleanup needed"

curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${TEXT}" > /dev/null || true

echo "[$(date -u +%FT%TZ)] alerted: ${PCT}% used (${USED}/${TOTAL})"
