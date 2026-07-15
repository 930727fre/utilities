#!/bin/sh
# Pull one bt wrapper down from Google Drive (decrypted on the fly).
#
# Usage: ./restore.sh "<wrapper_name>"
#
# Target is utilities/transcribe/data/bt/<wrapper>/. After the sync
# finishes, transcribe's scan tick will pick up the wrapper within
# ~30 s: no `.aria2` control file → sentinel absent → mtime settled
# → filter_wrapper runs → archive tier attaches the annotated SRTs
# from /archive/ (assuming you had them from a prior processing pass).
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 <wrapper_name>" >&2
    exit 1
fi
WRAPPER="$1"

cd "$(dirname "$0")"

docker compose run --rm rclone sync \
    "gdrive-crypt:transcribe/${WRAPPER}" \
    "/bt/${WRAPPER}" \
    --progress \
    --stats-one-line \
    --checksum
