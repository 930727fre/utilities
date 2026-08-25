FROM alpine:3.19

# rclone   — restic's `rclone:` backend uses rclone as a WebDAV bridge
#            (restic has no native WebDAV support). Also carries the
#            existing rclone.conf shared with homelab/rclone.
# restic   — content-addressable dedup backup. Replaces the previous
#            tarball approach: repeated chunks across snapshots share
#            storage, so 90d retention costs ~1-2x base repo size
#            instead of 90x tarball size. Encryption + integrity check
#            built in (state-of-the-art AES-256-CTR + Poly1305-ChaCha20),
#            so we back up via plain `nas:` remote — no rclone-crypt
#            wrapper needed, would just double-encrypt.
# sqlite   — hot .db snapshot via `sqlite3 .backup`. Restic reads
#            filesystem, can't hand it a live wal-mode db.
# jq       — parse `restic snapshots --json` in restore.sh (shell text
#            parsing on restic output is fragile).
# postgresql-client — pg_dump binary for the immich case in backup.sh.
# alpine's default package pulls a matching-major client; forward-
# compatible with Immich's postgres 14 server.
RUN apk add --no-cache rclone restic sqlite tzdata jq postgresql-client

COPY backup.sh /backup.sh
RUN chmod +x /backup.sh

RUN mkdir -p /var/spool/cron/crontabs && \
    echo "0 20 * * * /backup.sh >> /var/log/backup.log 2>&1" > /var/spool/cron/crontabs/root

CMD ["crond", "-f", "-d", "8"]
