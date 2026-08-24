# filebrowser

Self-hosted web file manager for a small circle (~3 users). Web-only access via Cloudflare Tunnel + CF Access (Gmail OTP via existing `*.930727fre.dev` wildcard app).

Picked over Seafile after weighing: Seafile's built-in trash + version history are nice UX but require MariaDB + memcached sidecars, mariadb-dump plumbing in the backup script, and a post-first-boot `seahub_settings.py` edit for reverse-proxy setups. FileBrowser is one 20 MB Go container with BoltDB inside — zero config surgery after first boot. **Undelete is provided at the backup layer** (`utilities/backup` runs restic daily, 90d retention) rather than in-app; recovery is admin-mediated instead of user self-service, which is fine at this scale.

## Architecture

Single container. `/srv` is the file root users see; `/config/filebrowser.db` is the embedded BoltDB (users, share links, settings). Both bind-mounted from `./data/` on the host, gitignored.

## Setup

### 1. Cloudflare side (one-time)
In the CF Zero Trust dashboard:
1. **Networks → Tunnels → your tunnel → Published application routes**: add
   - `files.930727fre.dev` → `http://filebrowser:80`
2. **Access → Applications**: the existing `*.930727fre.dev` wildcard app auto-catches this subdomain. No new app needed.

(Adjust the subdomain if `files` doesn't fit — anything under `.930727fre.dev` inherits the wildcard.)

### 2. Bring up
```bash
cd ~/utilities/filebrowser
docker compose up -d
docker compose logs filebrowser
```

**Look for the admin password in the first-run logs** — filebrowser generates a random one on init and prints it once (e.g. `Randomly generated password: XXXXXXXX`). Grab it before scrolling past.

If you miss it: `rm -rf data/config && docker compose up -d` re-inits. (This wipes users + shares, not files under `data/files/`.)

### 3. First login
Visit `https://files.930727fre.dev` → pass CF Access (Gmail OTP) → filebrowser login page → username `admin`, password from step 2.

Immediately: **Settings → Profile → change admin password** to something you'll store in Bitwarden. The auto-generated one is fine but not something you can look up later.

### 4. Create the 2 other user accounts
Settings → Users → New. For each:
- Username, password (out-of-band delivery to them: Signal / text)
- Scope: `/users/<their-name>/` (creates a per-user root; they only see their own space)
- Permissions: Modify + Delete + Share for their own scope; no admin
- Also add their Gmail to the CF Access policy so the outer auth lets them in

## Undelete / recovery

There's no in-app trash. If a user deletes a file:
- You run `./restore.sh` in `~/utilities/backup/` (the daily restic snapshots include filebrowser's data dir)
- Pick `filebrowser`, pick the most recent snapshot from before the delete
- Restic restores; you `docker compose restart filebrowser` if BoltDB was touched
- Advanced (single file, don't wipe others' changes): `docker compose run --rm backup restic restore <snap_id> --include /tmp/staging/files/users/<name>/<path> --target /tmp/one-file`

Snapshots are daily, retention 90d — so worst case a same-day delete + oops means losing edits made since 04:00 that morning.

## Ops

**Rebuild / update:**
```bash
docker compose pull
docker compose up -d
```

**Peek at files without going through the web UI:**
```bash
ls -la data/files/
```
(That's the point of picking FileBrowser — data is `ls`-inspectable, not opaque.)

**Storage:**
```bash
du -sh data/
```

## Backup

Handled by `utilities/backup` (restic to friend's NAS). `data/` is one bind mount for the backup container:
- `data/files/` — the actual user content
- `data/config/filebrowser.db` — BoltDB (users, shares, settings)

BoltDB is copied live during backup; single-file, low-write app means the chance of a mid-transaction snapshot is negligible. Worst case one daily snapshot has slightly stale DB; next day's is fine.
