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
echo "[$(date)] NAS  repo: ${RESTIC_REPOSITORY_NAS}"
echo "[$(date)] MEGA repo: ${RESTIC_REPOSITORY_MEGA} (excluding: ${MEGA_EXCLUDE:-none})"

# ── One-time repo init on first run ─────────────────────────────────
# `restic snapshots` returns non-zero when the repo doesn't exist yet.
# Check that specifically rather than `restic init || true` — the
# latter would swallow real init errors (wrong password after a rekey,
# remote unreachable, permission denied).
for REPO_NAME in NAS MEGA; do
    STEP="restic:init:$(echo "$REPO_NAME" | tr '[:upper:]' '[:lower:]')"
    eval "REPO_URL=\$RESTIC_REPOSITORY_$REPO_NAME"
    if ! restic --repo "$REPO_URL" snapshots >/dev/null 2>&1; then
        echo "[$(date)] restic repository ${REPO_NAME} missing; initializing..."
        restic --repo "$REPO_URL" init
    fi
done

# ── Per-tool snapshot ──────────────────────────────────────────────
# Staging dir is the same path each iteration; restic tag (--tag $TOOL)
# is what distinguishes snapshots. Restore step in restore.sh knows to
# strip the /tmp/staging/ prefix when restoring back to /tools/<tool>/data.
STAGING="/tmp/staging"

# Track per-repo tool coverage for the summary notification.
NAS_TOOLS_DONE=""
MEGA_TOOLS_DONE=""
MEGA_TOOLS_SKIPPED=""

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
        immich)
            # Live postgres files can't be safely raw-copied (WAL /
            # mid-transaction corruption risk). We handle the DB via
            # the pg_dump pass below; exclude the live data dir from
            # the copy pass here so it doesn't sneak into the tarball.
            COPY_EXCLUDE='/postgres/'
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

    # Immich-specific: dump Postgres via pg_dump over the loopback
    # port Immich compose publishes (127.0.0.1:5433). Fails LOUD —
    # a snapshot without the DB is nearly useless for Immich (all
    # metadata / face embedding refs / album structure live in the DB),
    # better to abort the whole tick than ship a half backup.
    if [ "$TOOL" = "immich" ]; then
        STEP="${TOOL}:pgdump"
        if [ -z "$IMMICH_DB_PASSWORD" ]; then
            echo "Error: immich in TOOLS but IMMICH_DB_PASSWORD not set" >&2
            exit 1
        fi
        echo "[$(date)] immich: pg_dump →  $STAGING/immich-postgres.sql"
        PGPASSWORD="$IMMICH_DB_PASSWORD" pg_dump \
            -h 127.0.0.1 -p 5433 -U postgres -d immich \
            --no-owner --clean --if-exists \
            > "$STAGING/immich-postgres.sql"
    fi

    STEP="${TOOL}:copy"
    find "$DATA" -type f ! -name "*.db" ! -name "*.db-wal" ! -name "*.db-shm" 2>/dev/null \
        | grep -Ev "$COPY_EXCLUDE" \
        | while IFS= read -r f; do
        rel=${f#$DATA/}
        target="$STAGING/$rel"
        mkdir -p "$(dirname "$target")"
        cp "$f" "$target"
    done

    # ── Push staging to both repos (MEGA opt-out via MEGA_EXCLUDE) ──
    # Staging is built once, both backups reuse it. --host is fixed so
    # `restic snapshots` groups cleanly (container hostname is random).
    # --tag $TOOL is the primary restore filter dimension.

    STEP="${TOOL}:restic+backup:nas"
    echo "[$(date)] ${TOOL}: pushing to NAS repo..."
    restic --repo "$RESTIC_REPOSITORY_NAS" backup "$STAGING" \
        --tag "$TOOL" \
        --host "utilities-backup"
    NAS_TOOLS_DONE="${NAS_TOOLS_DONE}${NAS_TOOLS_DONE:+ }${TOOL}"

    # MEGA opt-out: MEGA_EXCLUDE is a space-separated list. Match on
    # whole-word membership so partial names ('mich' vs 'immich') don't
    # collide.
    SKIP_MEGA=0
    for excl in $MEGA_EXCLUDE; do
        [ "$excl" = "$TOOL" ] && { SKIP_MEGA=1; break; }
    done

    if [ "$SKIP_MEGA" = "1" ]; then
        echo "[$(date)] ${TOOL}: skipping MEGA (in MEGA_EXCLUDE)"
        MEGA_TOOLS_SKIPPED="${MEGA_TOOLS_SKIPPED}${MEGA_TOOLS_SKIPPED:+ }${TOOL}"
    else
        STEP="${TOOL}:restic+backup:mega"
        echo "[$(date)] ${TOOL}: pushing to MEGA repo..."
        restic --repo "$RESTIC_REPOSITORY_MEGA" backup "$STAGING" \
            --tag "$TOOL" \
            --host "utilities-backup"
        MEGA_TOOLS_DONE="${MEGA_TOOLS_DONE}${MEGA_TOOLS_DONE:+ }${TOOL}"
    fi

    rm -rf "$STAGING"
done

# ── Retention (NAS only) ────────────────────────────────────────────
# 90 daily snapshots per tool on NAS. --group-by tag applies the keep
# policy WITHIN each tool's snapshot set (else 5 tools × 1/day would
# compete for the same 90 slots and each tool would only keep ~18 days).
# --prune reclaims disk immediately.
#
# MEGA repo has NO forget — retention is intentionally infinite.
# Deltas are tiny (~100 MB/day worst case) against a 50 GB free tier,
# multi-year runway. Revisit if MEGA usage climbs past ~40 GB.
STEP="restic+forget:nas"
restic --repo "$RESTIC_REPOSITORY_NAS" \
    forget --group-by tag --keep-daily 90 --prune

ELAPSED=$(( $(date +%s) - START ))

# ── Telegram summary ────────────────────────────────────────────────
# URL-encoded: '+' for space, '%0A' for newline, '%7C' for '|'.
NAS_ENC=$(echo "$NAS_TOOLS_DONE" | tr ' ' '+')
MEGA_ENC=$(echo "$MEGA_TOOLS_DONE" | tr ' ' '+')
SKIP_ENC=$(echo "$MEGA_TOOLS_SKIPPED" | tr ' ' '+')

MSG="Backup+done+%7C+${ELAPSED}s%0ANAS:+${NAS_ENC}%0AMEGA:+${MEGA_ENC}"
[ -n "$MEGA_TOOLS_SKIPPED" ] && MSG="${MSG}+(skipped+${SKIP_ENC})"

trap - EXIT
notify "$MSG"

echo "[$(date)] Done"
