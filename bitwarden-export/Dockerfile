FROM alpine:3.20

# nodejs+npm   — Bitwarden's `bw` CLI is a Node package. No official
#                Docker image is published, npm is the canonical
#                distribution channel. Pure-JS, so alpine's musl is fine.
# age          — outer encryption layer. X25519 + ChaCha20-Poly1305,
#                keyed by an offline-stored public key. Adds defense in
#                depth on top of Bitwarden's own encrypted_json format.
# curl         — Telegram notify. BusyBox wget's HTTPS support is
#                inconsistent across alpine versions; curl is reliable.
# jq           — URL-encode UTF-8 Telegram text via `jq -sRr @uri`.
# tzdata       — cron fires in UTC by default; keeping the package here
#                lets you switch to Asia/Taipei via TZ env if desired.
RUN apk add --no-cache nodejs npm age curl jq tzdata && \
    npm install -g @bitwarden/cli

COPY export.sh /export.sh
RUN chmod +x /export.sh

# 19:00 UTC = 03:00 Asia/Taipei next day — one hour before the backup
# container's 04:00 tick, so today's fresh export lands in secrets/
# before the daily backup pass sweeps it out to NAS + MEGA.
RUN mkdir -p /var/spool/cron/crontabs && \
    echo "0 19 * * * /export.sh >> /var/log/export.log 2>&1" > /var/spool/cron/crontabs/root

CMD ["crond", "-f", "-d", "8"]
