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

docker compose run --rm rclone copy \
    "$SRC" \
    "$DST" \
    --progress \
    --stats-one-line
