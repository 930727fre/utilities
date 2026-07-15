#!/bin/sh
# Pull one bt wrapper down from Google Drive (decrypted on the fly).
#
# Usage: ./restore.sh "<wrapper_name>"
#
# Restore is per-wrapper by design — you rarely want to restore
# everything at once (defeats the "offload for disk space" purpose).
#
# Uses `rclone copy` (matches backup.sh) — skips any files already
# present locally with matching size + mtime, so an interrupted restore
# can be resumed by re-running.
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 <wrapper_name>" >&2
    echo "       (see wrapper list with: docker compose run --rm rclone lsd gdrive-crypt:transcribe/)" >&2
    exit 1
fi
WRAPPER="$1"

cd "$(dirname "$0")"

if [ ! -f config/rclone.conf ]; then
    if [ -d config/rclone.conf ]; then
        echo "ERROR: config/rclone.conf exists as a DIRECTORY (docker auto-created)." >&2
        echo "  Run: rmdir config/rclone.conf" >&2
    else
        echo "ERROR: config/rclone.conf not found. See README for setup." >&2
    fi
    exit 1
fi

docker compose run --rm rclone copy \
    "gdrive-crypt:transcribe/${WRAPPER}" \
    "/bt/${WRAPPER}" \
    --progress \
    --stats-one-line
