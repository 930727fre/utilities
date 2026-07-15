#!/bin/sh
# Sync one bt wrapper up to Google Drive (encrypted via rclone crypt).
#
# Usage: ./backup.sh "<wrapper_name>"
#
# Wrapper is the directory name inside utilities/transcribe/data/bt/.
# Remote destination is gdrive-crypt:transcribe/<wrapper>/ — see
# README.md for the rclone.conf setup that defines gdrive-crypt.
#
# Runs synchronously with progress output. For a large wrapper
# (season/complete-series pack) this can take hours on a consumer
# uplink; wrap in `tmux` / `screen` if you want to detach.
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 <wrapper_name>" >&2
    exit 1
fi
WRAPPER="$1"

cd "$(dirname "$0")"

# --checksum verifies content on both sides after transfer (mtime alone
# is unreliable across ephemeral rclone containers). Costs extra Drive
# API calls but well worth it for a "did my backup actually work?" test.
docker compose run --rm rclone sync \
    "/bt/${WRAPPER}" \
    "gdrive-crypt:transcribe/${WRAPPER}" \
    --progress \
    --stats-one-line \
    --checksum
