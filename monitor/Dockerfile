FROM alpine:3.19

# curl for Telegram POST (predictable URL-encoding via --data-urlencode).
# tzdata so cron respects TZ env; coreutils' df is more portable than
# busybox df output columns.
RUN apk add --no-cache curl tzdata coreutils

COPY check-disk.sh /check-disk.sh
RUN chmod +x /check-disk.sh

# 10-minute disk check. Fires Telegram every tick when over threshold —
# no dedup — because "disk 90%+ full" is a real problem that deserves
# a nag until it's addressed, and 10-min cadence keeps the nag pace
# tolerable. Extensible: add more scripts + crontab entries as new
# checks come up (VPN health, cert expiry, service reachability).
RUN mkdir -p /var/spool/cron/crontabs && \
    echo "*/10 * * * * /check-disk.sh >> /var/log/check-disk.log 2>&1" \
        > /var/spool/cron/crontabs/root

CMD ["crond", "-f", "-d", "8"]
