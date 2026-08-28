FROM alpine:3.19

# rclone   — push local blobs to nas: + mega: (both remotes share the
#            host ~/rclone.conf with utilities/backup + homelab/rclone)
# jq       — URL-encode the Telegram message body (handles UTF-8 emoji +
#            newlines correctly, unlike hand-rolled +/%0A substitution)
# tzdata   — respect TZ env for log timestamps
# dcron    — cron scheduler
# tini     — PID 1 signal forwarding for clean docker stop
RUN apk add --no-cache rclone jq tzdata dcron tini

COPY backup.sh /backup.sh
RUN chmod +x /backup.sh

RUN mkdir -p /var/spool/cron/crontabs && \
    echo "0 4 * * * /backup.sh >> /var/log/backup.log 2>&1" > /var/spool/cron/crontabs/root

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["crond", "-f", "-l", "8"]
