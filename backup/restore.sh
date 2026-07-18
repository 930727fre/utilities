#!/bin/sh
# Interactive restore from R2. Picks a tool, picks a snapshot date,
# then:
#   flashcard / free2speak → wipe-and-restore the entire data dir
#   transcribe-archive     → pick one title, sudo cp into archive dir
#
# UX modeled after rclone/restore.sh: no CLI args, numbered menus,
# Enter/EOF cancels at every prompt.

set -eu
cd "$(dirname "$0")"

# ── helper: numeric-index prompt with bounds check ───────────────────
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
    printf '%s' "$a"
}

# ── 1. Which tool ────────────────────────────────────────────────────
echo "Listing tools on R2..."
TOOLS=$(docker compose run --rm backup rclone lsf --dirs-only \
    "r2:${R2_BUCKET}/" 2>/dev/null | sed 's|/$||' | sort)

[ -z "$TOOLS" ] && { echo "ERROR: no tools on remote" >&2; exit 1; }

echo ""
echo "Tools with snapshots on R2:"
echo ""
# Show the restore mode next to each tool so the choice is unambiguous
# before the user picks. Mode is derived from tool name — anything
# other than transcribe-archive falls through to wipe-and-replace.
printf '%s\n' "$TOOLS" | awk '{
    mode = ($0 == "transcribe-archive") ? "→ pick title" : "→ wipe & replace"
    printf "  %3d  %-20s %s\n", NR, $0, mode
}'
echo ""

TOTAL=$(printf '%s\n' "$TOOLS" | wc -l)
IDX=$(pick_index "Which tool? (index; Enter to cancel): " "$TOTAL")
TOOL=$(printf '%s\n' "$TOOLS" | sed -n "${IDX}p")

# ── 2. Which snapshot ────────────────────────────────────────────────
echo ""
echo "Listing snapshots for $TOOL..."
DATES=$(docker compose run --rm backup rclone lsf --dirs-only \
    "r2:${R2_BUCKET}/${TOOL}/" 2>/dev/null | sed 's|/$||' | \
    grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort -r)

[ -z "$DATES" ] && { echo "ERROR: no snapshots for $TOOL" >&2; exit 1; }

echo ""
echo "Snapshots for $TOOL (newest first):"
echo ""
printf '%s\n' "$DATES" | awk '{printf "  %3d  %s\n", NR, $0}'
echo ""

TOTAL=$(printf '%s\n' "$DATES" | wc -l)
IDX=$(pick_index "Which snapshot? (index; Enter to cancel): " "$TOTAL")
DATE=$(printf '%s\n' "$DATES" | sed -n "${IDX}p")

# ── 3. Download the tarball to a host-visible temp dir ───────────────
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo ""
echo "Downloading r2:${R2_BUCKET}/${TOOL}/${DATE}/data.tar.gz ..."
docker compose run --rm -v "$TMP:/dl" backup rclone copyto \
    "r2:${R2_BUCKET}/${TOOL}/${DATE}/data.tar.gz" \
    /dl/archive.tar.gz >/dev/null

# ── 4a. transcribe-archive → single-title flow ───────────────────────
if [ "$TOOL" = "transcribe-archive" ]; then
    tar -tzf "$TMP/archive.tar.gz" 2>/dev/null | \
        awk '/^\.\/[^\/]+\/$/ { print substr($0, 3, length($0) - 3) }' | \
        sort -u > "$TMP/titles.txt"

    TOTAL=$(wc -l < "$TMP/titles.txt")
    [ "$TOTAL" -eq 0 ] && { echo "ERROR: snapshot has no title dirs" >&2; exit 1; }

    echo ""
    echo "Titles in $TOOL/$DATE:"
    echo ""
    awk '{printf "  %3d  %s\n", NR, $0}' "$TMP/titles.txt"
    echo ""

    IDX=$(pick_index "Which title? (index; Enter to cancel): " "$TOTAL")
    TITLE=$(sed -n "${IDX}p" "$TMP/titles.txt")

    ARCHIVE_ROOT=$(realpath ../transcribe/data/archive 2>/dev/null || echo "")
    [ -z "$ARCHIVE_ROOT" ] || [ ! -d "$ARCHIVE_ROOT" ] && \
        { echo "ERROR: ../transcribe/data/archive not found" >&2; exit 1; }

    tar -xzf "$TMP/archive.tar.gz" -C "$TMP" "./${TITLE}"

    echo ""
    echo "→ Restoring: $TITLE"
    echo ""
    echo "Contents:"
    find "$TMP/${TITLE}" -type f | sed "s|$TMP/|  |"
    echo ""

    DEST="$ARCHIVE_ROOT/$TITLE"
    if [ -e "$DEST" ]; then
        echo "WARNING: '$TITLE' already exists in live archive."
        printf "Overwrite? (y/N): "
        if ! IFS= read -r OW; then echo ""; echo "Cancelled."; exit 0; fi
        case "$OW" in
            [yY]|[yY][eE][sS]) sudo rm -rf "$DEST" ;;
            *) echo "Cancelled."; exit 0 ;;
        esac
    fi

    sudo cp -r "$TMP/${TITLE}" "$ARCHIVE_ROOT/"
    # Match sibling archive dirs (all root-owned by mirror_to_archive).
    sudo chown -R root:root "$DEST"

    echo ""
    echo "✓ Restored '$TITLE' from ${TOOL}/${DATE}"
    echo ""
    echo "  Archive entry only — enables Stage 0 archive attach for future"
    echo "  re-downloads. If the live /artifact/Movies files were also"
    echo "  deleted, Jellyfin still won't show this until re-downloaded."
    exit 0
fi

# ── 4b. flashcard / free2speak → wipe-and-restore full data dir ──────
TARGET=$(realpath "../${TOOL}/data" 2>/dev/null || echo "")
[ -z "$TARGET" ] || [ ! -d "$TARGET" ] && \
    { echo "ERROR: ../${TOOL}/data not found" >&2; exit 1; }

echo ""
echo "→ Full-snapshot restore of $TOOL from $DATE"
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

# Wipe target contents (not the dir itself — that's a bind-mount source
# in the backup / consuming service's compose file, don't disturb it).
sudo find "$TARGET" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

sudo tar -xzf "$TMP/archive.tar.gz" -C "$TARGET"

# Quick sanity check on any .db files — same as the pre-consolidation
# pull.sh, delegates to the container's sqlite3 binary so we don't
# require host-side sqlite install.
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
echo "✓ Restored ${TOOL} from ${DATE}"
echo ""
echo "  If the consuming service was up during the wipe, restart it now:"
echo "    docker compose -f ../${TOOL}/docker-compose.yml restart"
