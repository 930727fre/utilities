#!/bin/sh
set -e

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: $0 <tool> <YYYY-MM-DD>" >&2
    exit 1
fi
TOOL="$1"
DATE="$2"

case "$DATE" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) echo "Error: DATE must be YYYY-MM-DD (got: $DATE)" >&2; exit 1 ;;
esac

DATA="/tools/${TOOL}/data"
TARBALL="/tmp/${TOOL}-restore.tar.gz"

if [ ! -d "$DATA" ]; then
    echo "Error: ${DATA} not mounted (check docker-compose volumes for tool '${TOOL}')" >&2
    exit 1
fi

if [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
    echo "Error: ${DATA} is not empty. Clear all contents manually before pulling." >&2
    echo "" >&2
    echo "Current contents:" >&2
    ls -la "$DATA" >&2
    exit 1
fi

echo ""
echo "To align ownership of restored files, this script needs your host UID and GID."
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
echo "[$(date)] Pulling r2:${R2_BUCKET}/${TOOL}/${DATE}/data.tar.gz"
rm -f "$TARBALL"
rclone copyto "r2:${R2_BUCKET}/${TOOL}/${DATE}/data.tar.gz" "$TARBALL"

if [ ! -f "$TARBALL" ]; then
    echo "Error: snapshot ${TOOL}/${DATE} not found in R2" >&2
    exit 1
fi

echo "[$(date)] Extracting to ${DATA}"
tar -xzf "$TARBALL" -C "$DATA"

echo "[$(date)] Verifying SQLite integrity..."
FAIL=$(find "$DATA" -type f -name "*.db" -exec sh -c '
    res=$(sqlite3 "$1" "PRAGMA integrity_check;")
    [ "$res" = "ok" ] || printf "%s: %s\n" "$1" "$res"
' _ {} \;)

if [ -n "$FAIL" ]; then
    echo "Error: integrity check failed:" >&2
    echo "$FAIL" >&2
    exit 1
fi

chown -R "${HOST_UID}:${HOST_GID}" "$DATA"
rm -f "$TARBALL"

echo "[$(date)] Done. Restored ${TOOL} from ${DATE}, owner ${HOST_UID}:${HOST_GID}"
