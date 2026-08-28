#!/bin/sh
set -e

DATE=$(TZ=Asia/Taipei date +%Y-%m-%d)
START=$(date +%s)
STEP="init"

notify() {
    # jq @uri handles emoji + newline + special chars correctly. Alternative
    # would be hand-encoding \n as %0A and spaces as + like before, but that
    # breaks the moment we want UTF-8 characters (✅❌⚠️) in the message.
    ENCODED=$(printf '%s' "$1" | jq -sRr @uri)
    wget -qO- "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=${ENCODED}" > /dev/null || true
}

# Format: {emoji} | Backup | {details}
# Matches the homelab/disk-watchdog + homelab/transcribe notifier
# convention so all self-host notifications look uniform in the Telegram
# feed. Multi-line detail (NAS/MEGA tool lists, check status) follows on
# subsequent lines below the standard header.
trap 'notify "❌ | Backup | FAILED at ${STEP}"' EXIT

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

# ── Retention (both repos, uniform 90-day) ──────────────────────────
# Every tool gets `restic forget --tag $TOOL --keep-daily 90 --prune`
# applied on both repos (NAS + MEGA). No exemptions here — the
# "infinite retention" bucket lives in utilities/secrets-vault/ via
# rclone copy, which is the right primitive for pre-sealed monolithic
# blobs. Everything backed up through this container is either
# recoverable via other channels (immich → phone) or acceptable-to-
# lose-after-90-days (jellyfin watch state, flashcard scheduling,
# scanned docs).
#
# --tag scopes each forget to that tool's snapshots so the 90-slot
# budget isn't shared across tools (each keeps its own 90 dailies).
#
# NAS_TOOLS_DONE / MEGA_TOOLS_DONE were built during the backup loop —
# reusing them ensures we only forget on repos where the tool actually
# has snapshots (e.g. immich never lands on MEGA, so it's absent from
# MEGA_TOOLS_DONE — no forget call needed there).
#
# --retry-lock 30s: restic backup and forget both take exclusive
# repo locks. Through the rclone: backend, the just-completed backup's
# lock-file DELETE hasn't fully round-tripped to the remote by the
# time forget starts a fraction of a second later, so a naive forget
# fails with "waiting up to 0s for the lock". Waiting 30s handles this
# and any transient WebDAV / MEGA API slowness without meaningfully
# delaying the happy path (lock usually clears in <1s).
for FTOOL in $NAS_TOOLS_DONE; do
    STEP="restic+forget:nas:${FTOOL}"
    echo "[$(date)] NAS forget for tag '${FTOOL}' (keep-daily 90)..."
    restic --repo "$RESTIC_REPOSITORY_NAS" \
        forget --tag "$FTOOL" --keep-daily 90 --prune --retry-lock 30s
done

for FTOOL in $MEGA_TOOLS_DONE; do
    STEP="restic+forget:mega:${FTOOL}"
    echo "[$(date)] MEGA forget for tag '${FTOOL}' (keep-daily 90)..."
    restic --repo "$RESTIC_REPOSITORY_MEGA" \
        forget --tag "$FTOOL" --keep-daily 90 --prune --retry-lock 30s
done

# ── secrets-vault rclone copy branch ────────────────────────────────
# Non-restic branch: /secrets/ holds pre-sealed monolithic blobs
# (hardware-encrypted archives now; future age-wrapped bw exports too).
# Copied as-is to nas:secrets/ + mega:secrets/. No encryption applied
# here (sources supply their own seal). No retention pruning either —
# blobs accumulate forever. --exclude ".*" skips any dotfiles that
# might land in the directory (paranoia; there shouldn't be any).
STEP="rclone+copy:secrets:nas"
echo "[$(date)] rclone copy /secrets/ → nas:secrets/ ..."
rclone copy /secrets/ nas:secrets/ --exclude ".*"

STEP="rclone+copy:secrets:mega"
echo "[$(date)] rclone copy /secrets/ → mega:secrets/ ..."
rclone copy /secrets/ mega:secrets/ --exclude ".*"

SECRETS_COUNT=$(find /secrets -type f ! -name '.*' 2>/dev/null | wc -l)

# ── Weekly full integrity check (Sundays) ───────────────────────────
# One unified verify cadence: every Sunday, fully verify both restic
# repos AND both secrets-vault mirrors. No monthly/subset split —
# bandwidth is not the constraint here (04:00, home broadband, no
# quota concerns on either backend), simpler is better.
#
# restic check --read-data: downloads and decrypts every pack file,
# verifies HMAC → catches any bit-rot or password/crypto issue.
#
# rclone check --download: fetches every remote object in secrets/,
# byte-compares against the local /secrets/ tree → catches any bit-
# rot on either tier.
#
# Failures bubble to the outer EXIT trap → Telegram "Backup FAILED at
# {step}" so you notice the same way you notice a failed backup.
CHECK_STATUS=""
if [ "$(date +%u)" = "7" ]; then
    for REPO_NAME in NAS MEGA; do
        eval "REPO_URL=\$RESTIC_REPOSITORY_$REPO_NAME"
        STEP="restic+check-read:${REPO_NAME}"
        echo "[$(date)] $REPO_NAME weekly full check (--read-data)..."
        restic --repo "$REPO_URL" check --read-data --retry-lock 30s
    done

    STEP="rclone+check:secrets:nas"
    echo "[$(date)] Weekly full verify secrets — NAS..."
    rclone check --download /secrets/ nas:secrets/ --exclude ".*"

    STEP="rclone+check:secrets:mega"
    echo "[$(date)] Weekly full verify secrets — MEGA..."
    rclone check --download /secrets/ mega:secrets/ --exclude ".*"

    CHECK_STATUS="Weekly full verify OK (restic + secrets, both tiers)"
fi

ELAPSED=$(( $(date +%s) - START ))

# ── Telegram summary ────────────────────────────────────────────────
# jq @uri in notify() handles encoding — this string is authored as
# plain UTF-8 with actual newlines. Header follows the homelab
# {emoji} | {Service} | {details} convention.
MSG="✅ | Backup | ${ELAPSED}s
NAS:  ${NAS_TOOLS_DONE}
MEGA: ${MEGA_TOOLS_DONE}"
[ -n "$MEGA_TOOLS_SKIPPED" ] && MSG="${MSG} (skipped ${MEGA_TOOLS_SKIPPED})"
MSG="${MSG}
Secrets: ${SECRETS_COUNT} file(s) pushed"
[ -n "$CHECK_STATUS" ] && MSG="${MSG}
${CHECK_STATUS}"

trap - EXIT
notify "$MSG"

echo "[$(date)] Done"
