# seafile

Self-hosted file sync + share for a small circle (~3 users). Web-only access via Cloudflare Tunnel + CF Access (Gmail OTP); no native app auth wired.

Chose Seafile over FileBrowser (too basic — no trash), Nextcloud (bloated for pure file-share use), OpenCloud (too new). Seafile's built-in trash + version history + share expiry cover the day-to-day mistake-recovery cases without needing the external backup layer to catch every oops.

## Architecture

3 containers on `seafile-net` (internal); `seafile` also joined to `my_network` external so cloudflared can reach it:

- **seafile** (`seafileltd/seafile-mc:11.0-latest`) — main app (seaf-server + seahub + internal nginx). Pinned to 11.x LTS-track instead of 12.x — simpler compose contract (no JWT / SeaDoc surprises), enough for our use case.
- **db** (`mariadb:10.11`) — required by Seafile; SQLite variant is officially deprecated.
- **memcached** (`memcached:1.6.18`, 256 MB cap) — library metadata cache.

Data lives at `./data/{seafile,mariadb}/` — bind-mounted, gitignored, backed up via `utilities/backup` (added separately).

## Setup

### 1. Cloudflare side (one-time)
In the CF Zero Trust dashboard:
1. **Networks → Tunnels → your tunnel → Published application routes**: add
   - `seafile.930727fre.dev` → `http://seafile:80`
2. **Access → Applications**: your existing `*.930727fre.dev` wildcard app auto-catches this subdomain (Gmail OTP policy). No new app needed.

### 2. Secrets (Bitwarden + shell export)
Three secrets; store in Bitwarden and export in shell before `docker compose up`. Compose uses `${VAR:?...}` so missing any of them fails loudly at parse time.

```bash
# Random DB root password — 32 bytes is enough, remember it's only
# used inside seafile-net.
export SEAFILE_MYSQL_ROOT_PASSWORD="$(openssl rand -base64 32)"

# First-boot admin — you'll log in with this and can change it via
# the web UI after. Keep the exports around for from-scratch redeploys.
export SEAFILE_ADMIN_EMAIL="you@example.com"
export SEAFILE_ADMIN_PASSWORD="$(openssl rand -base64 24)"
```

### 3. Bring up
```bash
cd ~/utilities/seafile
docker compose up -d
# Wait ~30-60s for Seafile to init the schema + write default configs.
docker compose logs -f seafile
# Look for "Seahub is started" — that's the OK signal.
```

Then visit https://seafile.930727fre.dev, pass CF Access (Gmail OTP), log in with `SEAFILE_ADMIN_EMAIL` / `SEAFILE_ADMIN_PASSWORD`.

### 4. Create the other 2 user accounts
Admin panel → Users → Add user. Give them the temporary passwords out-of-band (Signal, text, whatever). They log in via CF Access first (their Gmail must be added to the Access policy — see cloudflared/README.md for the policy config) then land at Seafile login and use their assigned password.

## Trash + versioning defaults

Both features are on by default in Seafile CE:
- **Trash**: deleted files stay in the per-library trash for 30 days. Admin can tune via `SEAFILE.CONF` → `history` section.
- **File versions**: every save is a new version, keeps forever by default. Tune per-library from the library settings gear.

These are why our external backup (via `utilities/backup`) can afford to focus purely on disaster-recovery role (host death, disk failure, DB corruption) rather than day-to-day file recovery.

## Backup

Handled by `utilities/backup` (restic to friend's NAS). Special case in that container's `backup.sh` runs `mariadb-dump` against `seafile-db` before the restic snapshot — live MariaDB data files can't be safely copied. See `utilities/backup/README.md` for how to add / adjust.

## Ops

**Restart just Seafile (config change, e.g. tweaking trash retention):**
```bash
docker compose restart seafile
```

**Full stack rebuild (image update):**
```bash
docker compose pull
docker compose up -d
```

**Check DB from inside the network:**
```bash
docker compose exec db mariadb -u root -p"$SEAFILE_MYSQL_ROOT_PASSWORD"
```

**Storage usage:**
```bash
du -sh data/{seafile,mariadb}
# Most of the growth will be in data/seafile/seafile-data/storage/ (content-addressed blob store).
```

## Notes

- The Seafile image bundles nginx internally to reverse-proxy seaf-server + Django (seahub). You can't strip it cleanly, so we accept the ~1 extra process per container.
- `container_name: seafile` is what cloudflared routes to; don't rename without updating the CF tunnel route.
- If seafile fails to start with `Error: 'gunicorn' not found` or similar, the image may be mid-upgrade — pin to a specific tag (e.g. `11.0.13`) instead of `11.0-latest`.
