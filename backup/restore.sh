#!/bin/sh
# Interactive restic-based restore. Picks a tool, picks a snapshot,
# wipes and restores the tool's whole data dir.
#
# UX modeled after the previous tarball restore.sh: no CLI args,
# numbered menus, Enter/EOF cancels at every prompt.

set -eu
cd "$(dirname "$0")"

# ── helper: numeric-index prompt with bounds check ───────────────────
# Result goes in the global `PICKED` so calls aren't wrapped in `$()`
# (a subshell would swallow both the prompt output AND any `exit` for
# cancellation, leaving the parent with an empty capture).
PICKED=""
pick_index() {
    prompt="$1"
    max="$2"
    printf '%s' "$prompt"
    if ! IFS= read -r a; then
        echo ""; echo "Cancelled."; exit 0
    fi
    [ -z "$a" ] && { echo "Cancelled."; exit 0; }
    echo "$a" | grep -qE '^[0-9]+$' || {
        echo "ERROR: expected an index (got: $a)" >&2; exit 1;
    }
    if [ "$a" -lt 1 ] || [ "$a" -gt "$max" ]; then
        echo "ERROR: index $a out of range (1-$max)" >&2; exit 1;
    fi
    PICKED="$a"
}

# ── 1. Which tool ────────────────────────────────────────────────────
# Enumerate distinct tags from all snapshots in the repo.
echo "Listing tools in restic repo..."
TOOLS=$(docker compose run --rm --no-TTY backup \
    sh -c 'restic snapshots --json 2>/dev/null | jq -r ".[].tags[]" | sort -u' \
    2>/dev/null | tr -d '\r')

[ -z "$TOOLS" ] && { echo "ERROR: no snapshots in repo" >&2; exit 1; }

echo ""
echo "Tools with snapshots:"
echo ""
printf '%s\n' "$TOOLS" | awk '{printf "  %3d  %s\n", NR, $0}'
echo ""

TOTAL=$(printf '%s\n' "$TOOLS" | wc -l)
pick_index "Which tool? (index; Enter to cancel): " "$TOTAL"
TOOL=$(printf '%s\n' "$TOOLS" | sed -n "${PICKED}p")

# ── 2. Which snapshot ────────────────────────────────────────────────
# List snapshots for that tag, newest first. Format: "<short_id>  <time>".
echo ""
echo "Listing snapshots for $TOOL..."
SNAPS=$(docker compose run --rm --no-TTY backup \
    sh -c "restic snapshots --tag '$TOOL' --json 2>/dev/null | jq -r '.[] | \"\\(.short_id)  \\(.time)\"' | sort -r -k2" \
    2>/dev/null | tr -d '\r')

[ -z "$SNAPS" ] && { echo "ERROR: no snapshots for $TOOL" >&2; exit 1; }

echo ""
echo "Snapshots for $TOOL (newest first):"
echo ""
printf '%s\n' "$SNAPS" | awk '{printf "  %3d  %s\n", NR, $0}'
echo ""

TOTAL=$(printf '%s\n' "$SNAPS" | wc -l)
pick_index "Which snapshot? (index; Enter to cancel): " "$TOTAL"
LINE=$(printf '%s\n' "$SNAPS" | sed -n "${PICKED}p")
SNAP_ID=$(echo "$LINE" | awk '{print $1}')
SNAP_TIME=$(echo "$LINE" | awk '{print $2}' | cut -c1-19)

# ── 3. Where does the restored data belong ────────────────────────────
# Per-tool restore destination. Mirrors the mount source in
# docker-compose.yml — must stay in sync when adding tools.
case "$TOOL" in
    jellyfin) TARGET_SRC="../../homelab/jellyfin/config" ;;
    keyboard) TARGET_SRC="../keyboard/backend/data" ;;
    *)        TARGET_SRC="../${TOOL}/data" ;;
esac
TARGET=$(realpath "$TARGET_SRC" 2>/dev/null || echo "")
[ -z "$TARGET" ] || [ ! -d "$TARGET" ] && \
    { echo "ERROR: ${TARGET_SRC} not found" >&2; exit 1; }

echo ""
echo "→ Restore $TOOL from snapshot $SNAP_ID (taken $SNAP_TIME)"
echo ""
echo "  Target: $TARGET"
echo "  Existing contents will be WIPED and replaced."
echo ""
printf "Continue? (y/N): "
if ! IFS= read -r CONFIRM; then echo ""; echo "Cancelled."; exit 0; fi
case "$CONFIRM" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "Cancelled."; exit 0 ;;
esac

# ── 4. Restore to a host-visible temp dir, then wipe-and-move ────────
# Snapshot stores files under /tmp/staging/... (see backup.sh). We
# restore to a fresh temp dir and then `mv` the tree into $TARGET so
# any restic-side error doesn't leave $TARGET half-wiped.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo ""
echo "Restoring to staging: $TMP"
docker compose run --rm -v "$TMP:/restore-out" backup \
    restic restore "$SNAP_ID" --target /restore-out

# Where restic actually wrote the files (mirrors backup.sh's STAGING).
SRC="$TMP/tmp/staging"
if [ ! -d "$SRC" ]; then
    echo "ERROR: expected restored files under $SRC (restic output layout changed?)" >&2
    exit 1
fi

# Wipe target contents (not the dir itself — that's a bind-mount source
# in the tool's compose file, don't disturb the mountpoint).
sudo find "$TARGET" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
sudo cp -a "$SRC"/. "$TARGET/"

# Quick sanity check on any .db files — delegates to the container's
# sqlite3 binary so we don't require host-side sqlite install.
DB_HITS=$(find "$TARGET" -name "*.db" 2>/dev/null | wc -l)
if [ "$DB_HITS" -gt 0 ]; then
    echo ""
    echo "Running SQLite integrity check on $DB_HITS db file(s)..."
    if docker compose run --rm backup sh -c \
        "find /tools/${TOOL}/data -name '*.db' -exec sqlite3 {} 'PRAGMA integrity_check;' \; 2>&1" \
        | grep -v '^ok$' | grep -v '^\[' | grep .; then
        echo ""
        echo "ERROR: at least one db reported non-'ok' integrity" >&2
        exit 1
    fi
    echo "✓ All db files pass integrity_check"
fi

echo ""
echo "✓ Restored ${TOOL} from snapshot ${SNAP_ID} (${SNAP_TIME})"
echo ""
echo "  Rebuild the consuming service so it picks up restored files:"
case "$TOOL" in
    jellyfin) echo "    docker compose -f ../../homelab/jellyfin/docker-compose.yml up -d --build" ;;
    keyboard) echo "    docker compose -f ../keyboard/docker-compose.yml up -d --build" ;;
    *)        echo "    docker compose -f ../${TOOL}/docker-compose.yml up -d --build" ;;
esac
