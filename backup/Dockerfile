FROM alpine:3.19

RUN apk add --no-cache rclone sqlite

COPY backup.sh /backup.sh
RUN chmod +x /backup.sh

RUN mkdir -p /var/spool/cron/crontabs && \
    echo "0 4 * * * /backup.sh >> /var/log/backup.log 2>&1" > /var/spool/cron/crontabs/root

CMD ["crond", "-f", "-d", "8"]
