#!/bin/sh
# Interactive restore of bt wrappers from Google Drive (decrypted on
# the fly). Run it, pick from the numbered list, done.
#
# Uses `rclone copy` — skips files already present locally with matching
# size + mtime, so an interrupted restore can be resumed by re-running.
# Ranges (`1-5`) fold into a single rclone invocation via `--include`,
# so 20 wrappers = 1 container start + 1 shared connection pool + 1
# unified progress bar, not N of each.
set -eu

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

# --dirs-only + sort gives a stable, one-name-per-line index source.
# Trailing slash from lsf is stripped so the name matches what
# `rclone copy --include` expects.
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
docker compose run --rm rclone lsf --dirs-only gdrive-crypt:transcribe/ \
    2>/dev/null | sed 's|/$||' | sort > "$TMP"
TOTAL=$(wc -l < "$TMP")
if [ "$TOTAL" -eq 0 ]; then
    echo "No wrappers on remote."
    exit 0
fi

echo "Available wrappers on gdrive-crypt:transcribe/ :"
echo ""
awk '{printf "  %3d  %s\n", NR, $0}' "$TMP"
echo ""
printf "Restore which? (index like 3, or range like 1-5; Enter to cancel): "

if ! IFS= read -r ANSWER; then
    echo ""
    echo "Cancelled."
    exit 0
fi

if [ -z "$ANSWER" ]; then
    echo "Cancelled."
    exit 0
fi

# Parse: either N or N-M.
if echo "$ANSWER" | grep -qE '^[0-9]+-[0-9]+$'; then
    START=$(echo "$ANSWER" | cut -d- -f1)
    END=$(echo "$ANSWER" | cut -d- -f2)
elif echo "$ANSWER" | grep -qE '^[0-9]+$'; then
    START="$ANSWER"
    END="$ANSWER"
else
    echo "ERROR: expected index (e.g. 3) or range (e.g. 1-5), got: $ANSWER" >&2
    exit 1
fi

if [ "$START" -lt 1 ] || [ "$END" -gt "$TOTAL" ] || [ "$START" -gt "$END" ]; then
    echo "ERROR: bad range $START-$END (available: 1-$TOTAL)" >&2
    exit 1
fi

# Build the rclone arg list: one --include per selected wrapper, so a
# single rclone process handles the whole batch. `set --` builds the
# positional args in a way that survives wrapper names with spaces /
# colons / other legal-Linux chars.
#
# Wrapper names commonly contain rclone-glob metachars: `[1080p]`,
# `[YTS.BZ]`, occasional `*` or `?`. Escape them with a leading `\`
# so rclone matches them literally, otherwise `--include "[1080p]/**"`
# is read as a character class and matches nothing.
echo ""
echo "Restoring:"
set --
i=$START
while [ "$i" -le "$END" ]; do
    wrapper=$(sed -n "${i}p" "$TMP")
    echo "  → $wrapper"
    escaped=$(printf '%s' "$wrapper" | sed 's/[][*?{}]/\\&/g')
    set -- "$@" --include "${escaped}/**"
    i=$((i + 1))
done
echo ""

# ── Disk headroom preflight ────────────────────────────────────────────
# Sum the selected wrappers' remote size (same --include list `rclone
# copy` will use below), compare against local /bt/ headroom. Reject
# if the download would push the mount past 90% used. Mirrors the
# transcribe /api/bt/magnet policy — restore is another vector for
# filling the disk.
echo "Checking disk headroom..."
size_json=$(docker compose run --rm rclone size --json \
    gdrive-crypt:transcribe "$@" 2>/dev/null)
size_bytes=$(printf '%s' "$size_json" | \
    grep -oE '"bytes"[[:space:]]*:[[:space:]]*[0-9]+' | \
    grep -oE '[0-9]+' | head -1)

if [ -z "$size_bytes" ] || [ "$size_bytes" -eq 0 ]; then
    echo "ERROR: could not determine remote size for selected wrappers" >&2
    echo "  raw output: $size_json" >&2
    exit 1
fi

bt_host=$(realpath ../transcribe/data/bt 2>/dev/null)
if [ -z "$bt_host" ] || [ ! -d "$bt_host" ]; then
    echo "ERROR: local /bt path not found at ../transcribe/data/bt" >&2
    exit 1
fi

df_line=$(df -B1 --output=used,size "$bt_host" | tail -1)
used=$(echo "$df_line" | awk '{print $1}')
total=$(echo "$df_line" | awk '{print $2}')
threshold=90

if [ $(( (used + size_bytes) * 100 )) -gt $((total * threshold)) ]; then
    awk -v u="$used" -v s="$size_bytes" -v t="$total" -v th="$threshold" \
        'BEGIN {
            proj = (u + s) / t * 100
            curr = u / t * 100
            gb = s / 1e9
            printf "\nREJECTED: selected wrappers total %.1f GB.\n", gb
            printf "  Would push /bt to %.1f%% (limit %d%%). Currently %.1f%%.\n", proj, th, curr
            print "  Delete some torrents first, or narrow the range."
        }' >&2
    exit 1
fi

echo ""

docker compose run --rm rclone copy \
    gdrive-crypt:transcribe /bt \
    "$@" \
    --progress \
    --stats-one-line
