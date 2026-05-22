#!/bin/sh
set -e

[ -z "$1" ] && { echo "Usage: $0 <YYYY-MM-DD>" >&2; exit 1; }
DATE="$1"

case "$DATE" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) echo "Error: DATE must be YYYY-MM-DD (got: $DATE)" >&2; exit 1 ;;
esac

DEST="/flashcard/flashcard.db"

for f in /flashcard/flashcard.db /flashcard/flashcard.db-wal /flashcard/flashcard.db-shm; do
    if [ -e "$f" ]; then
        echo "Error: $f exists. Delete all of flashcard.db, flashcard.db-wal, flashcard.db-shm manually before pulling." >&2
        exit 1
    fi
done

echo ""
echo "To align ownership of the pulled file, this script needs your host UID and GID."
echo "In another terminal on the host, run:"
echo ""
echo "  id -u && id -g"
echo ""
printf "Host UID: "
read HOST_UID
printf "Host GID: "
read HOST_GID

case "$HOST_UID" in
    ''|*[!0-9]*) echo "Error: UID must be a non-empty integer (got: '$HOST_UID')" >&2; exit 1 ;;
esac
case "$HOST_GID" in
    ''|*[!0-9]*) echo "Error: GID must be a non-empty integer (got: '$HOST_GID')" >&2; exit 1 ;;
esac

echo ""
echo "[$(date)] Pulling r2:${R2_BUCKET}/flashcard/${DATE}/flashcard.db -> ${DEST} (live slot)"
rclone copyto "r2:${R2_BUCKET}/flashcard/${DATE}/flashcard.db" "$DEST"

if [ ! -f "$DEST" ]; then
    echo "Error: snapshot ${DATE} not found in R2" >&2
    exit 1
fi

INTEGRITY=$(sqlite3 "$DEST" "PRAGMA integrity_check;")
if [ "$INTEGRITY" != "ok" ]; then
    echo "Error: integrity check failed: $INTEGRITY" >&2
    exit 1
fi

chown "${HOST_UID}:${HOST_GID}" "$DEST"

SIZE=$(du -h "$DEST" | cut -f1)
echo "[$(date)] Done. ${DEST} (${SIZE}), owner ${HOST_UID}:${HOST_GID}"
