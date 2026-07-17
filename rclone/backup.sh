#!/bin/sh
# Push bt wrappers up to Google Drive (AES-encrypted via rclone crypt).
#
# Usage:
#   ./backup.sh                    # bulk: everything under /bt/ → gdrive-crypt:transcribe/
#   ./backup.sh "<wrapper name>"   # single wrapper
#
# Uses `rclone copy` (NOT `sync`) intentionally:
#   - Skips files already on remote with matching size + mtime — idempotent,
#     safe to re-run daily / on demand
#   - NEVER deletes remote content, so a `delete_torrent` on the UI (which
#     nukes /bt/<wrapper>/) leaves the offsite backup untouched
#
# For a large wrapper (season / complete-series pack) this can take hours
# on a consumer uplink; wrap in tmux / screen if you want to detach.
set -eu

cd "$(dirname "$0")"

# Fail-fast: docker compose auto-creates empty directories for missing
# bind-mount sources, which then surface as "is a directory" errors
# inside the container. Verify the config is a real file before running.
if [ ! -f config/rclone.conf ]; then
    if [ -d config/rclone.conf ]; then
        echo "ERROR: config/rclone.conf exists as a DIRECTORY (docker auto-created)." >&2
        echo "  Run: rmdir config/rclone.conf" >&2
        echo "  Then scp your rclone.conf from your laptop into place." >&2
    else
        echo "ERROR: config/rclone.conf not found." >&2
        echo "  Run rclone config on your laptop, then:" >&2
        echo "    scp ~/.config/rclone/rclone.conf <this host>:$(pwd)/config/rclone.conf" >&2
    fi
    exit 1
fi

if [ $# -ge 1 ]; then
    WRAPPER="$1"
    SRC="/bt/${WRAPPER}"
    DST="gdrive-crypt:transcribe/${WRAPPER}"
    echo "Backing up single wrapper: ${WRAPPER}"
else
    SRC="/bt"
    DST="gdrive-crypt:transcribe"
    echo "Backing up all wrappers in /bt/ (skipping any already uploaded)"
fi

# In-flight write check: any file with mtime < 1 minute means aria2c is
# still pumping pieces (or someone/something else is writing). Uploading
# that snapshot would ship half-empty / half-written bytes — no
# corruption, but wasted bandwidth (we'd re-upload the whole file next
# run once mtimes settle). Warn + prompt rather than silent-skip so the
# user knows what they're deciding.
BT_HOST=$(realpath ../transcribe/data/bt 2>/dev/null || echo "")
if [ -n "$BT_HOST" ] && [ -d "$BT_HOST" ]; then
    if [ $# -ge 1 ]; then
        scan_root="$BT_HOST/$WRAPPER"
    else
        scan_root="$BT_HOST"
    fi
    if [ -d "$scan_root" ]; then
        # For bulk, group by top-level wrapper name (relative to /bt).
        # For single, we already know the wrapper — just check "any hit".
        if [ $# -ge 1 ]; then
            hit=$(find "$scan_root" -mmin -1 -type f -print -quit 2>/dev/null)
            recent="$WRAPPER"
            [ -z "$hit" ] && recent=""
        else
            recent=$(find "$scan_root" -maxdepth 20 -mmin -1 -type f \
                -printf '%P\n' 2>/dev/null | cut -d/ -f1 | sort -u)
        fi
        if [ -n "$recent" ]; then
            echo ""
            echo "WARNING: files modified in the last minute — likely still downloading:"
            printf '%s\n' "$recent" | sed 's/^/  → /'
            echo ""
            echo "Uploading now would ship half-written bytes (wasted bandwidth, no corruption)."
            printf "Continue anyway? (y/N): "
            if ! IFS= read -r ANSWER; then
                echo ""
                echo "Cancelled."
                exit 0
            fi
            case "$ANSWER" in
                [yY]|[yY][eE][sS]) echo "Proceeding..." ;;
                *) echo "Cancelled."; exit 0 ;;
            esac
        fi
    fi
fi

docker compose run --rm rclone copy \
    "$SRC" \
    "$DST" \
    --progress \
    --stats-one-line
