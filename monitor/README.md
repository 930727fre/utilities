# monitor

Small always-on container that runs periodic health checks against
homelab infrastructure and pings Telegram when something looks wrong.

Deliberately separate from `utilities/backup` even though they share the
alpine+cron+curl+Telegram pattern — backup's job is IO-heavy (tar 1.6 GB
of jellyfin config, upload to R2); mixing hourly `df` checks into the
same container muddles responsibility and means a `backup.sh` cron miss
blocks the disk alert too.

## Current checks

### `check-disk.sh` — every 10 minutes

Alerts when `/data` (bind-mounted from `../../homelab/data`) is
`≥ DISK_THRESHOLD_PCT%` full (default 90). **No dedup** — every
over-threshold tick fires. At 90% the disk really is close to
catastrophe and the every-10-min nag is the design intent, not a
bug. User acts on it, the nag stops.

Extend when a new check is needed: add a script, add a crontab line
in the Dockerfile, add whatever bind-mounts the new script needs to
`docker-compose.yml`.

## Env

```sh
export TELEGRAM_BOT_TOKEN=<from BotFather>
export TELEGRAM_CHAT_ID=<from getUpdates>
export DISK_THRESHOLD_PCT=90   # optional, defaults to 90
```

Both TELEGRAM vars are `:?required` at compose-parse — a silently
degraded monitor is worse than a broken build.
