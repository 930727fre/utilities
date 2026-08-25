# nextcloud

Self-hosted cloud drive for a small circle (~3 users). Native iOS + Android apps for the daily "upload photo/video/file from phone" flow — bypasses the iOS Safari + Photos HEVC-transcode issue that made FileBrowser miserable for iPhone-heavy family members.

Deliberately **LAN-only exposure** (no CF Tunnel route). Family uses the native app over home Wi-Fi. External access via Tailscale node share — wiring deferred; see "Tailscale" section below.

Picked over FileBrowser (no native apps, Safari transcode plague), Seafile (post-first-boot config surgery), Immich (photos-only, doesn't cover generic files like PDFs / zips). Nextcloud CE with SQLite mode is single-container and covers both file types + Photos auto-upload + trash + versioning.

## Architecture

One container. Local build extends `nextcloud:apache` with `libheif1` + `ffmpeg` (see Dockerfile) so HEIC (iPhone photo default) and MOV/HEVC (iPhone video default) files get proper server-side thumbnails. Without these, uploads work but thumbnails are blank icons — bad UX for photo browsing. SQLite DB lives inside the bind-mounted `./data/data/` alongside user files, so backup is straightforward (one dir, sqlite3 .backup for the DB).

Trade-off vs external MariaDB: SQLite is officially "not recommended for production" but that guidance is aimed at 100+ user deployments. For 3 concurrent users, SQLite is fine and saves 2 sidecar containers.

Photo browsing UX (timeline, face recognition, map view) is still noticeably worse than Immich even with thumbnails working — accept as trade-off or run Immich alongside for photo-heavy use.

## Setup

### 1. Secrets (Bitwarden + shell export)

```bash
export NEXTCLOUD_ADMIN_USER=admin                 # or whatever you want
export NEXTCLOUD_ADMIN_PASSWORD="$(openssl rand -base64 24)"
```

### 2. Trusted domains

Nextcloud rejects requests whose Host header isn't whitelisted (defense against Host-header spoofing). List the hostnames/IPs family will type into their Nextcloud app.

```bash
# Space-separated. Include LAN IP + short hostname. Tailscale MagicDNS
# name gets added later when that path is wired.
export NEXTCLOUD_TRUSTED_DOMAINS="192.168.1.100 tuf-3060-2404.local"
```

Find your LAN IP: `hostname -I | awk '{print $1}'`.

### 3. Bring up

```bash
cd ~/utilities/nextcloud
docker compose up -d
docker compose logs -f nextcloud
# First boot takes ~1 min — installs Nextcloud, creates SQLite DB,
# runs initial upgrade. Look for "Nextcloud was successfully installed"
# then Apache starts serving. Ctrl+C the log tail.
```

### 4. First login

Browser → `http://<host-lan-ip>:8082` → Nextcloud login page → admin credentials from step 1.

**Turn off the "Recommended Apps" carousel** in the setup wizard — Nextcloud tries to install Talk / Calendar / Contacts / Mail on first login. You don't need any of them for the file-share use case. Uncheck all and skip.

### 5. Create user accounts (2 more)

Top-right avatar → **Users** → **New user**. For each family member:
- Display name + username + password
- Storage quota (optional; sensible default is unlimited for 3-user home use, tighten later if abuse)
- Groups: none needed

Hand out passwords out-of-band (Signal / text).

### 6. Family app setup

Each family member on their phone:
1. Install Nextcloud app (iOS App Store / Google Play)
2. Add account: server URL = `http://<host-lan-ip>:8082`, then their username + password
3. Enable Auto Upload for Photos:
   - iOS: Settings → Auto Upload → toggle on the account → **turn ON "Keep original filenames"** and **turn ON "Preserve original format"** so HEVC videos upload as HEVC (no transcode)
   - Android: same, in the app's Settings

App remembers the LAN URL. When at home, uploads run at native LAN speed. When outside, it'll fail to connect (LAN URL isn't routable externally) — that's fine, uploads resume when they get home.

## Ops

**Restart / update:**
```bash
docker compose pull
docker compose up -d
```
Nextcloud handles its own DB migrations on startup.

**Check DB (SQLite):**
```bash
docker compose exec nextcloud sqlite3 /var/www/html/data/nextcloud.db ".tables"
```

**Run occ (Nextcloud CLI):**
```bash
docker compose exec -u www-data nextcloud php occ status
docker compose exec -u www-data nextcloud php occ user:list
docker compose exec -u www-data nextcloud php occ user:setting <username> files quota "50 GB"
```

**Storage usage:**
```bash
du -sh data/data/
# Most of the growth is under data/data/<username>/files/
```

## Tailscale (deferred)

When ready to give family external access, share your host node into each family member's own Tailscale account (see main homelab notes / node-sharing docs). Then:
1. Add the Tailscale MagicDNS name of your host to `NEXTCLOUD_TRUSTED_DOMAINS`
2. `docker compose up -d` to pick up the env change
3. Family adds a **second account** in their Nextcloud app with URL = `http://<host-tailnet-name>:8082`
4. When at home → LAN account (fast); when away → Tailscale account (P2P encrypted, no CF slowdown)

## Backup (deferred)

Handled by `utilities/backup` (restic to friend's NAS). Nextcloud's `data/` gets added to backup's TOOLS list in a follow-up commit. Backup script's existing sqlite hot-copy logic picks up `nextcloud.db` automatically — no per-tool special case needed, unlike what Seafile would have required.

## Disk watchdog (deferred)

`homelab/disk-watchdog` currently has a filebrowser API integration that stops uploads when disk fills. That integration will be reworked for Nextcloud in a follow-up commit — Nextcloud has native per-user quota via `occ user:setting`, so the pattern shifts from "flip a permission flag" to "lower quotas to 0". Until then, unset `FILEBROWSER_ADMIN_PASSWORD` in your shell so the watchdog skips the (now-dead) filebrowser branch.
