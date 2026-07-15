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
