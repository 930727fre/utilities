#!/bin/sh
# secrets-vault push script — mirrors /data/ to nas:secrets/ + mega:secrets/
# via rclone copy. No encryption applied here: every file in /data/ is
# expected to already be sealed (either by age wrap for future bw exports,
# or the user's own hardware-encrypted archive for manually-dropped files).
#
# Weekly full byte-level verification on Sundays via `rclone check --download`.
# Rest of the week: plain copy only (fast, idempotent, adds only new/changed
# files since last run).
set -e

START=$(date +%s)
STEP="init"

notify() {
    # jq @uri handles UTF-8 emoji + newlines correctly, matching the
    # convention used by utilities/backup + homelab/{disk-watchdog,transcribe}.
    ENCODED=$(printf '%s' "$1" | jq -sRr @uri)
    wget -qO- "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=${ENCODED}" > /dev/null || true
}

# Format: {emoji} | SecretsVault | {details}
# Matches the homelab notification convention.
trap 'notify "❌ | SecretsVault | FAILED at ${STEP}"' EXIT

echo "[$(date)] Starting secrets-vault push"

# Count non-placeholder files being pushed. .gitkeep is a scaffold artifact
# and doesn't represent actual vault content; excluding it keeps the
# reported file count truthful.
FILECOUNT=$(find /data -type f ! -name '.gitkeep' 2>/dev/null | wc -l)

STEP="rclone+copy:nas"
echo "[$(date)] Pushing to NAS ($FILECOUNT files)..."
rclone copy /data/ nas:secrets/ --exclude ".gitkeep"

STEP="rclone+copy:mega"
echo "[$(date)] Pushing to MEGA ($FILECOUNT files)..."
rclone copy /data/ mega:secrets/ --exclude ".gitkeep"

# ── Weekly full verification (Sundays) ──────────────────────────────
# `rclone check --download` pulls every remote object and byte-compares
# it against the local copy. Catches bit-rot at either tier. Runtime
# is bounded by the total size of /data/, which is expected to stay
# small (Bitwarden exports ~50 KB each, user-provided sealed archives
# updated infrequently).
CHECK_STATUS=""
if [ "$(date +%u)" = "7" ]; then
    STEP="rclone+check:nas"
    echo "[$(date)] Weekly full verify — NAS..."
    rclone check --download /data/ nas:secrets/ --exclude ".gitkeep"

    STEP="rclone+check:mega"
    echo "[$(date)] Weekly full verify — MEGA..."
    rclone check --download /data/ mega:secrets/ --exclude ".gitkeep"

    CHECK_STATUS="Weekly full verify OK (NAS + MEGA)"
fi

ELAPSED=$(( $(date +%s) - START ))

MSG="✅ | SecretsVault | ${ELAPSED}s
${FILECOUNT} file(s) → nas:secrets/ + mega:secrets/"
[ -n "$CHECK_STATUS" ] && MSG="${MSG}
${CHECK_STATUS}"

trap - EXIT
notify "$MSG"

echo "[$(date)] Done"
