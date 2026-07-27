#!/bin/sh
# Hourly disk-fill alert for the homelab data root. Sends a Telegram
# ping when /data (bind-mounted from ../../homelab/data) hits
# DISK_THRESHOLD_PCT% or higher.
#
# Deduped per UTC day via a marker file in /tmp — a persistently full
# disk gets one alert per day, not 24. Container survives across cron
# invocations so /tmp state persists between runs. Container recreate
# clears the marker and a fresh alert fires on the next full-disk tick,
# which is also fine (recreate is rare and worth re-notifying about).
set -eu

MOUNT=/data
THRESHOLD="${DISK_THRESHOLD_PCT:-80}"

# df output line: "Filesystem 1K-blocks Used Available Use% Mounted-on"
# Use% column has a trailing '%'; strip and coerce to int for comparison.
PCT=$(df "$MOUNT" | tail -1 | awk '{gsub(/%/,"",$5); print $5+0}')

if [ "$PCT" -lt "$THRESHOLD" ]; then
    exit 0
fi

STATE_FILE="/tmp/disk-alert-$(date -u +%Y-%m-%d)"
if [ -f "$STATE_FILE" ]; then
    exit 0
fi

USED=$(df -h "$MOUNT" | tail -1 | awk '{print $3}')
TOTAL=$(df -h "$MOUNT" | tail -1 | awk '{print $2}')
TEXT="⚠️ homelab disk ${PCT}% full (${USED}/${TOTAL}) — cleanup needed"

curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${TEXT}" > /dev/null || true

touch "$STATE_FILE"

# Prune stale markers so /tmp doesn't accumulate one per day forever.
find /tmp -maxdepth 1 -name 'disk-alert-*' -mtime +7 -delete 2>/dev/null || true

echo "[$(date -u +%FT%TZ)] alerted: ${PCT}% used (${USED}/${TOTAL})"
