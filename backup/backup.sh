#!/bin/sh
set -e

DATE=$(TZ=Asia/Taipei date +%Y-%m-%d)
START=$(date +%s)
STEP="init"

notify() {
    wget -qO- "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=$1" > /dev/null || true
}

trap 'notify "Backup+FAILED+at+${STEP}"' EXIT

echo "[$(date)] Starting backup for tools: ${TOOLS}"

# ── One-time repo init on first run ─────────────────────────────────
# `restic snapshots` returns non-zero when the repo doesn't exist yet.
# Check that specifically rather than `restic init || true` — the
# latter would swallow real init errors (wrong password after a rekey,
# NAS unreachable, permission denied).
STEP="restic:init"
if ! restic snapshots >/dev/null 2>&1; then
    echo "[$(date)] restic repository missing; initializing..."
    restic init
fi

# ── Per-tool snapshot ──────────────────────────────────────────────
# Staging dir is the same path each iteration; restic tag (--tag $TOOL)
# is what distinguishes snapshots. Restore step in restore.sh knows to
# strip the /tmp/staging/ prefix when restoring back to /tools/<tool>/data.
STAGING="/tmp/staging"

for TOOL in $TOOLS; do
    STEP="${TOOL}:setup"
    DATA="/tools/${TOOL}/data"

    if [ ! -d "$DATA" ]; then
        echo "Error: ${DATA} not mounted (check docker-compose volumes for tool '${TOOL}')" >&2
        exit 1
    fi

    # Per-tool copy-step exclusion (POSIX ERE via grep -E). Default
    # '^$' never matches, so nothing is excluded unless a tool is
    # explicitly listed. Only affects the plain copy — sqlite-safe .db
    # backup always runs so critical DB state is never at risk here.
    case "$TOOL" in
        jellyfin)
            # metadata/ = TMDB/TVDB poster + backdrop cache (~1.4 GB,
            # regenerable via library scan). log/ = server logs.
            # Skipping both keeps library.db + config + users + plugins
            # + watch state — the irreplaceable bits.
            COPY_EXCLUDE='/metadata/|/log/'
            ;;
        *)
            COPY_EXCLUDE='^$'
            ;;
    esac

    STEP="${TOOL}:sqlite"
    rm -rf "$STAGING"
    mkdir -p "$STAGING"
    find "$DATA" -type f -name "*.db" 2>/dev/null | while IFS= read -r db; do
        rel=${db#$DATA/}
        target="$STAGING/$rel"
        mkdir -p "$(dirname "$target")"
        sqlite3 "$db" ".backup '$target'"
    done

    STEP="${TOOL}:copy"
    find "$DATA" -type f ! -name "*.db" ! -name "*.db-wal" ! -name "*.db-shm" 2>/dev/null \
        | grep -Ev "$COPY_EXCLUDE" \
        | while IFS= read -r f; do
        rel=${f#$DATA/}
        target="$STAGING/$rel"
        mkdir -p "$(dirname "$target")"
        cp "$f" "$target"
    done

    STEP="${TOOL}:restic+backup"
    echo "[$(date)] ${TOOL}: sending to restic repo..."
    # --host utilities-backup: static hostname so `restic snapshots`
    # groups cleanly (container hostname is random-ish otherwise).
    # --tag $TOOL: primary snapshot filter dimension for restore.
    restic backup "$STAGING" \
        --tag "$TOOL" \
        --host "utilities-backup"

    rm -rf "$STAGING"
done

# ── Retention ──────────────────────────────────────────────────────
# 90 daily snapshots per tool. --group-by tag applies the keep policy
# WITHIN each tool's snapshot set (else 5 tools × 1/day would compete
# for the same 90 slots and each tool would only keep ~18 days).
# --prune reclaims disk immediately. At our scale (single-digit GB
# repo, hundreds of snapshots) prune-every-run is fine; for larger
# repos you'd split forget (every run) + prune (weekly).
STEP="restic+forget"
restic forget --group-by tag --keep-daily 90 --prune

ELAPSED=$(( $(date +%s) - START ))
TOOLS_ENC=$(echo "$TOOLS" | tr ' ' '+')

trap - EXIT
notify "Backup+done+%7C+${TOOLS_ENC}+%7C+${ELAPSED}s"

echo "[$(date)] Done"
