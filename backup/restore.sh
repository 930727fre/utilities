#!/bin/sh
# Interactive restore. First picks the KIND of restore (restic-managed
# tool data vs a single secrets-vault blob), then dispatches:
#
#   * Tool data: pick source repo (NAS/MEGA), pick tool, pick snapshot,
#     wipe & replace the tool's live data dir.
#   * Secrets file: pick source tier (NAS/MEGA), pick file from the
#     remote listing, download to /tmp for offline unsealing (never
#     drops back into data/ — you decrypt / import elsewhere).
#
# UX: numbered menus, Enter/EOF cancels at every prompt.

set -eu
cd "$(dirname "$0")"

# Restic repo URLs — MUST match docker-compose.yml's
# RESTIC_REPOSITORY_{NAS,MEGA}. Hardcoded here so restore.sh can pick
# a source before spawning the container. If you rename a repo path,
# update both files.
REPO_NAS="rclone:nas:restic/"
REPO_MEGA="rclone:mega:restic/"

# Secrets-vault remote paths — MUST match backup.sh's rclone copy
# destinations. Same "keep in sync" note as above.
SECRETS_NAS="nas:secrets/"
SECRETS_MEGA="mega:secrets/"

# ── helper: numeric-index prompt with bounds check ───────────────────
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

# ── 0. What KIND of restore? ─────────────────────────────────────────
echo ""
echo "What to restore?"
echo ""
echo "  1  Tool data (restic snapshot — flashcard / jellyfin / immich / crucial-docs / …)"
echo "  2  Secrets vault file (single blob — bw export / hardware-encrypted archive / …)"
echo ""
pick_index "Which? (index; Enter to cancel): " 2
KIND="$PICKED"

# ── Branch: SECRETS-VAULT restore ────────────────────────────────────
if [ "$KIND" = "2" ]; then
    echo ""
    echo "Restore secrets blob from which tier?"
    echo ""
    echo "  1  NAS   ($SECRETS_NAS)"
    echo "  2  MEGA  ($SECRETS_MEGA)"
    echo ""
    pick_index "Which? (index; Enter to cancel): " 2
    case "$PICKED" in
        1) SECRETS_REMOTE="$SECRETS_NAS" ; SECRETS_LABEL="NAS"  ;;
        2) SECRETS_REMOTE="$SECRETS_MEGA"; SECRETS_LABEL="MEGA" ;;
    esac

    echo ""
    echo "Listing files in $SECRETS_LABEL secrets/..."
    FILES=$(docker compose run --rm --no-TTY backup \
        rclone lsf "$SECRETS_REMOTE" --files-only 2>/dev/null | tr -d '\r' | sort)
    [ -z "$FILES" ] && { echo "ERROR: no files at $SECRETS_REMOTE" >&2; exit 1; }

    echo ""
    echo "Files in $SECRETS_LABEL secrets/:"
    echo ""
    printf '%s\n' "$FILES" | awk '{printf "  %3d  %s\n", NR, $0}'
    echo ""

    TOTAL=$(printf '%s\n' "$FILES" | wc -l)
    pick_index "Which file? (index; Enter to cancel): " "$TOTAL"
    FILE=$(printf '%s\n' "$FILES" | sed -n "${PICKED}p")

    # Download to a fresh temp dir so it's obvious where the file went;
    # user handles unseal / decrypt from there. Never dropped back into
    # data/secrets-vault — that's the source-of-truth, restore shouldn't
    # touch it.
    OUT=$(mktemp -d)
    echo ""
    echo "Downloading $FILE → $OUT/ ..."
    docker compose run --rm -v "$OUT:/restore-out" backup \
        rclone copy "${SECRETS_REMOTE}${FILE}" /restore-out/

    # rclone container writes as root; chown back so the user can
    # actually read the file without sudo.
    sudo chown -R "$(id -u):$(id -g)" "$OUT"

    echo ""
    echo "✓ Downloaded"
    echo "  → $OUT/$FILE"
    echo ""
    echo "  Unseal with whatever tool + key the blob was sealed with"
    echo "  (age --decrypt for bw exports, 7z / hardware key for the archive, etc.)"
    exit 0
fi

# ── Branch: RESTIC tool restore (existing flow) ──────────────────────

# ── 1. Which source repo ─────────────────────────────────────────────
# NAS is primary (holds every tool including immich). MEGA is the
# offsite mirror (everything except tools in MEGA_EXCLUDE).
echo ""
echo "Restore from which repo?"
echo ""
echo "  1  NAS   ($REPO_NAS, primary — every tool)"
echo "  2  MEGA  ($REPO_MEGA, offsite mirror — no immich)"
echo ""
pick_index "Which? (index; Enter to cancel): " 2
case "$PICKED" in
    1) REPO="$REPO_NAS"  ; REPO_LABEL="NAS"  ;;
    2) REPO="$REPO_MEGA" ; REPO_LABEL="MEGA" ;;
esac

# ── 2. Which tool ────────────────────────────────────────────────────
# Enumerate distinct tags from all snapshots in the chosen repo.
echo ""
echo "Listing tools in $REPO_LABEL repo..."
TOOLS=$(docker compose run --rm --no-TTY -e RESTIC_REPOSITORY="$REPO" backup \
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

# ── 3. Which snapshot ────────────────────────────────────────────────
# List snapshots for that tag, newest first. Format: "<short_id>  <time>".
echo ""
echo "Listing snapshots for $TOOL in $REPO_LABEL..."
SNAPS=$(docker compose run --rm --no-TTY -e RESTIC_REPOSITORY="$REPO" backup \
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

# ── 4. Where does the restored data belong ───────────────────────────
# Per-tool restore destination. Mirrors the mount source in
# docker-compose.yml — must stay in sync when adding tools.
case "$TOOL" in
    jellyfin)     TARGET_SRC="../../homelab/jellyfin/config" ;;
    crucial-docs) TARGET_SRC="./data/crucial-docs" ;;
    *)            TARGET_SRC="../${TOOL}/data" ;;
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

# ── 5. Restore to a host-visible temp dir, then wipe-and-move ────────
TMP=$(mktemp -d)
# sudo rm: the backup container runs as root, so files restic writes
# into $TMP (via the -v mount below) are root-owned on the host. Plain
# `rm -rf` from a non-root shell then fails with "Permission denied"
# and leaves cruft in /tmp. sudo covers both restored files and any
# nested dirs.
trap 'sudo rm -rf "$TMP"' EXIT

echo ""
echo "Restoring to staging: $TMP  (from $REPO_LABEL)"
docker compose run --rm -v "$TMP:/restore-out" -e RESTIC_REPOSITORY="$REPO" backup \
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

# crucial-docs is user-facing (you `cp` files in by hand, you browse
# them, no service consumes it). Chown back to the host user so you
# can read the restored certs/transcripts/etc without needing sudo
# forever after. Every other tool stays root-owned so its container /
# systemd unit can reassign as needed.
if [ "$TOOL" = "crucial-docs" ]; then
    sudo chown -R "$(id -u):$(id -g)" "$TARGET"
fi

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
    jellyfin)     echo "    docker compose -f ../../homelab/jellyfin/docker-compose.yml up -d --build" ;;
    crucial-docs) echo "    (no service — crucial-docs is a passive data folder, nothing to rebuild)" ;;
    *)            echo "    docker compose -f ../${TOOL}/docker-compose.yml up -d --build" ;;
esac
