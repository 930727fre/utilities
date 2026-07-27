# monitor

Small always-on container that runs periodic health checks against
homelab infrastructure and pings Telegram when something looks wrong.

Deliberately separate from `utilities/backup` even though they share the
alpine+cron+curl+Telegram pattern — backup's job is IO-heavy (tar 1.6 GB
of jellyfin config, upload to R2); mixing hourly `df` checks into the
same container muddles responsibility and means a `backup.sh` cron miss
blocks the disk alert too.

## Current checks

### `check-disk.sh` — hourly

Alerts when `/data` (bind-mounted from `../../homelab/data`) is
`≥ DISK_THRESHOLD_PCT%` full (default 80). Dedup'd per UTC day, so a
sustained full disk fires one alert per day, not 24.

Extend when a new check is needed: add a script, add a crontab line
in the Dockerfile, add whatever bind-mounts the new script needs to
`docker-compose.yml`.

## Env

```sh
export TELEGRAM_BOT_TOKEN=<from BotFather>
export TELEGRAM_CHAT_ID=<from getUpdates>
export DISK_THRESHOLD_PCT=80   # optional, defaults to 80
```

Both TELEGRAM vars are `:?required` at compose-parse — a silently
degraded monitor is worse than a broken build.
