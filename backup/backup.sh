#!/bin/sh
set -e

DATE=$(TZ=Asia/Taipei date +%Y-%m-%d)
START=$(date +%s)
STEP="init"

notify() {
    wget -qO- "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=$1" > /dev/null || true
}

trap 'notify "Backup+FAILED+at+${STEP}"' EXIT

echo "[$(date)] Starting backup for tools: ${TOOLS}"

for TOOL in $TOOLS; do
    STEP="${TOOL}:setup"
    DATA="/tools/${TOOL}/data"
    STAGING="/tmp/${TOOL}-staging"
    TARBALL="/tmp/${TOOL}.tar.gz"

    if [ ! -d "$DATA" ]; then
        echo "Error: ${DATA} not mounted (check docker-compose volumes for tool '${TOOL}')" >&2
        exit 1
    fi

    # Per-tool copy-step exclusion (regex, POSIX ERE via grep -E). Only
    # affects the plain file copy — sqlite-safe .db backup always runs
    # so critical DB state is never at risk here.
    #
    # Default '^$' never matches, so nothing is excluded when a tool
    # isn't listed.
    case "$TOOL" in
        jellyfin)
            # metadata/ = TMDB/TVDB poster + backdrop cache (~1.4 GB,
            # regenerable via library scan). log/ = server logs
            # (~1 MB, regenerable). Dropping both takes the tarball
            # from ~500 MB gzipped to ~10 MB while leaving library.db
            # (in data/, sqlite-backed up above), server config, users,
            # plugins, and watch state fully covered.
            COPY_EXCLUDE='/metadata/|/log/'
            ;;
        *)
            COPY_EXCLUDE='^$'
            ;;
    esac

    STEP="${TOOL}:sqlite"
    find "$DATA" -type f -name "*.db" 2>/dev/null | while IFS= read -r db; do
        rel=${db#$DATA/}
        target="$STAGING/$rel"
        mkdir -p "$(dirname "$target")"
        sqlite3 "$db" ".backup '$target'"
    done

    STEP="${TOOL}:copy"
    find "$DATA" -type f ! -name "*.db" ! -name "*.db-wal" ! -name "*.db-shm" 2>/dev/null \
        | grep -Ev "$COPY_EXCLUDE" \
        | while IFS= read -r f; do
        rel=${f#$DATA/}
        target="$STAGING/$rel"
        mkdir -p "$(dirname "$target")"
        cp "$f" "$target"
    done

    STEP="${TOOL}:tar"
    echo "[$(date)] ${TOOL}: tar-gzipping staging..."
    tar -czf "$TARBALL" -C "$STAGING" .
    SIZE=$(du -h "$TARBALL" | cut -f1)

    STEP="${TOOL}:rclone+copy"
    echo "[$(date)] ${TOOL}: uploading ${SIZE} to nas-crypt:backups/${TOOL}/${DATE}/..."
    # --progress = live in-place bar on TTY, periodic one-line stats on
    # pipe/cron; --stats 5s = update cadence (default 60s is too coarse
    # for the small tarballs here — jellyfin at 10 MB / 25 Mbps uplink
    # is done in 3 sec, so 60s stats would never fire).
    rclone copyto "$TARBALL" "nas-crypt:backups/${TOOL}/${DATE}/data.tar.gz" \
        --progress --stats 5s

    echo "[$(date)] ${TOOL}: ${SIZE} uploaded"

    rm -rf "$STAGING" "$TARBALL"
done

STEP="rclone+prune"
# 90d retention — NAS has no daily-quota / cost pressure like R2 did,
# so keep enough history to survive a "corrupted state noticed weeks
# later" recovery.
#
# Two-step instead of `delete --rmdirs`: the combined form errors out
# with "directory not empty" on every dir that has current snapshots
# (fresh uploads seconds ago are 0d old, not >90d), and that error
# exit trips `set -e` even though the delete-of-files portion did the
# right thing (deleted zero files, which is fine on day one).
#
# Standalone `rclone rmdirs` walks bottom-up and no-ops on non-empty
# dirs instead of erroring. `|| true` guards against WebDAV-backend
# edge cases where even rmdirs surfaces a non-fatal complaint. Worst
# case an empty tool/YYYY-MM-DD/ dir sits after its data.tar.gz ages
# out — visible in `rclone lsf`, cleaned on the next successful run.
rclone delete "nas-crypt:backups/" --min-age 90d
rclone rmdirs "nas-crypt:backups/" --leave-root 2>/dev/null || true

ELAPSED=$(( $(date +%s) - START ))
TOOLS_ENC=$(echo "$TOOLS" | tr ' ' '+')

trap - EXIT
notify "Backup+done+%7C+${TOOLS_ENC}+%7C+${ELAPSED}s"

echo "[$(date)] Done"
