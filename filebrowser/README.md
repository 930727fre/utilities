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
```

Default admin credentials on first init: **`admin` / `admin`** (v2.32.0 behavior — despite what older tutorials say about "random generated password in logs", this version just uses hardcoded default; not a bug, checked directly).

### 3. First login
Visit `https://files.930727fre.dev` → pass CF Access (Gmail OTP) → filebrowser login page → `admin` / `admin`.

**Immediately** change the admin password via **Settings → Profile** and store the new one in Bitwarden. The default `admin/admin` is behind CF Access (Gmail-gated) so it's not instantly exploitable, but leaving it means anyone who slips past CF Access is instantly admin.

If you'd rather set a known password before ever touching the UI:
```bash
docker compose exec filebrowser filebrowser users update admin --password 'your-chosen-password'
```

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

## LAN access (bypass CF Tunnel for full-speed uploads)

Family on home Wi-Fi can hit filebrowser at `http://<host-lan-ip>:8082` directly (port 8082 is mapped to the container's port 80 in the compose file). This bypasses CF Tunnel entirely — LAN-native speed (Gbps class) instead of the ~2 MB/s CF free-tier tunnel throughput.

Filebrowser's own account login still gates access, so publishing the port to LAN is safe under normal home Wi-Fi (any device on your Wi-Fi still needs a filebrowser account to log in).

External access continues to work at `https://files.930727fre.dev` — the CF Tunnel route hasn't changed. Family visiting outside home uses that URL, family at home uses the LAN one.

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
