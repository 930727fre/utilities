# backup

Daily backup at 04:00 Asia/Taipei, fanning out to **two independent offsite tiers** — `nas:` (friend's Tailscale WebDAV) and `mega:` (mega.nz free tier, 50 GB). Two branches inside one container:

### Branch 1 — restic (tool data)

Tools: `flashcard`, `free2speak`, `jellyfin`, `immich`, `crucial-docs`. Each `<tool>/data/` directory is snapshotted (SQLite via `sqlite3 .backup`, everything else copied as-is) into a staging dir, then `restic backup`ed into both repos independently.

Repos: `rclone:nas:restic/` + `rclone:mega:restic/`. Both share `RESTIC_PASSWORD` (one keychain entry, two storage locations); chunk keyspaces are separate — no `restic copy` between them, each push builds staging once and sends to both.

| Tool bucket | NAS | MEGA | Retention |
|---|---|---|---|
| `flashcard` / `free2speak` / `jellyfin` / `crucial-docs` | ✓ | ✓ | 90-day on both |
| `immich` | ✓ | ✗ (in `MEGA_EXCLUDE`) | 90-day on NAS |

Restic dedups per-snapshot chunk-by-chunk, so 90-day retention costs roughly base-repo-size + delta chunks, not `snapshots × tarball_size`.

### Branch 2 — rclone copy (secrets)

Location: `data/secrets/` (bind-mounted to `/secrets/` in the container). Holds pre-sealed monolithic blobs — user-dropped hardware-encrypted archives, and (once the age pubkey lands) age-wrapped bw exports produced by a separate `utilities/bitwarden-export/` container that shares this directory as its output target.

Push: plain `rclone copy /secrets/ nas:secrets/` + `rclone copy /secrets/ mega:secrets/`, no encryption applied at this layer (sources supply their own strong seal). No retention pruning — blobs accumulate forever (they're tiny, updates infrequent).

Why not restic for these: pre-encrypted monolithic blobs get zero benefit from restic dedup (random-looking bytes per update = unique chunks every time). Recovery is more direct through plain rclone (just download the `.age` / `.7z` file), avoiding the `restic → RESTIC_PASSWORD → Bitwarden` circular dependency for the disaster case.

### Data folders (`data/`)

Both branches read from `data/` inside this repo, gitignored entirely (private per-install content):

- `data/crucial-docs/` — passive folder of "would cry to lose but not catastrophic" personal documents (certs, transcripts, IDs, scanned records). `cp` files in by hand; the restic branch picks up the tree.
- `data/secrets/` — passive folder of pre-sealed critical blobs. `cp` in already-sealed archives (or let `bitwarden-export` write age-wrapped exports there). The rclone branch mirrors to both tiers.

Neither folder has a container or code of its own — pure input directories for this pipeline. If you clone this repo to a fresh machine, both dirs need to be created first (docker's `create_host_path: false` will fail loudly otherwise): `mkdir -p data/crucial-docs data/secrets`.

### Integrity checks

Every Sunday, tacked onto the end of the daily tick — no separate cron entry:

- `restic check --read-data` on both repos: downloads and decrypts every pack, verifies HMAC → catches bit-rot or password/crypto issues.
- `rclone check --download` on secrets against both tiers: fetches every remote blob, byte-compares against local → catches bit-rot at either tier.

One unified weekly cadence, no monthly/subset split — bandwidth isn't the constraint (04:00, home broadband, no quota concerns), simpler is better. Failures bubble to the outer trap → Telegram `❌ | Backup | FAILED at {step}`.

Manual quarterly restore drill is still worth doing on top: pick a tool via `./restore.sh` (or a secrets blob), verify the restored bytes are actually usable end-to-end.

### MEGA opt-out (`MEGA_EXCLUDE`)

`MEGA_EXCLUDE` in `docker-compose.yml` is a space-separated tool list that skips the MEGA restic push (NAS still gets it). Currently: `immich` — photo library already 3-2-1 via phone + host + NAS. Any new tool with a comparable independent-copy story goes here too. Does NOT apply to the secrets branch — that branch pushes to both tiers unconditionally.

## Adding a tool

1. Add a volume mount in `docker-compose.yml` — must use the exact path `/tools/<tool>/data` inside the container:
   ```yaml
   volumes:
     - type: bind
       source: ../newtool/data
       target: /tools/newtool/data
       bind:
         create_host_path: false
   ```
2. Add the tool name to the `TOOLS` env var.
3. If the tool's host path isn't `../<tool>/data` (e.g. `jellyfin`'s config lives at `../../homelab/jellyfin/config`), add a `case` branch in `restore.sh` so the restore target resolves correctly.
4. Rebuild: `docker compose up -d --build`.

`*.db` files are snapshotted via `sqlite3 .backup` (safe while the source app is running); `.db-wal` / `.db-shm` skipped as regenerable WAL artifacts. Everything else is plain copied into staging.

### Immich exception (Postgres)

Immich uses Postgres (not SQLite). backup.sh has an immich-specific pre-copy step that runs `pg_dump` against `127.0.0.1:5433` (Immich's compose publishes Postgres on loopback specifically for this) and writes `immich-postgres.sql` into the staging dir. The live `postgres/` data dir is excluded from the file copy (`COPY_EXCLUDE=/postgres/`) since raw-copying WAL-in-flight files corrupts them. Restic tarball ends up with `upload/` (photos) + `immich-postgres.sql` (DB snapshot).

**Restore is manual on the Immich side**: `restore.sh` puts files back (including the `.sql`), but you then have to:

```bash
cd ~/utilities/immich
docker compose down
sudo rm -rf data/postgres              # nuke stale schema
docker compose up -d database          # fresh init
sleep 20
docker compose exec -T database psql -U postgres -d immich < data/immich-postgres.sql
docker compose up -d                   # bring server back up
```

Not automated because it's rare + destructive; explicit steps prevent accidents.

## Setup — rclone.conf

Restic uses rclone as its backend (`rclone:<remote>:restic/`); rclone reads `${HOME}/rclone.conf` on the host, bind-mounted read-only into the container. **This file is shared with `homelab/rclone`**; if you haven't set it up yet, see the setup section in `homelab/rclone/README.md`.

Two remotes are required:

- **`[nas]`** — plain WebDAV to friend's Tailscale NAS. Not crypt (restic does its own AES-256; layering rclone-crypt on top would just add CPU for no security gain). Set up per `homelab/rclone/README.md`.
- **`[mega]`** — MEGA.nz account. One-time on the host:
  ```bash
  cd ~/homelab/rclone   # any repo with the rclone image works
  docker compose run --rm rclone config
  # n → mega → user: <MEGA email> → pass: <MEGA password>
  ```
  MEGA CLI login uses email + password directly (no OAuth). Password gets `obscure`d into `rclone.conf` — it's reversible obfuscation, not encryption, so treat `~/rclone.conf` accordingly.

## Setup — RESTIC_PASSWORD

Restic encrypts each repo with a password of your choosing; the same `RESTIC_PASSWORD` unlocks both NAS and MEGA repos here. **Losing this password = losing all snapshots on BOTH repos.** There's no recovery mechanism (same failure mode as rclone crypt).

Generate one and store in Bitwarden:

```bash
openssl rand -base64 32
```

Then export in shell before `docker compose up`:

```bash
export RESTIC_PASSWORD='<the value from Bitwarden>'
```

First `docker compose up` triggers an implicit `restic init` per repo inside `backup.sh` (the check is `restic --repo <URL> snapshots >/dev/null 2>&1 || restic --repo <URL> init`). Idempotent — later runs skip init.

## Deploy

```bash
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
export RESTIC_PASSWORD='...'
docker compose up -d --build
```

`network_mode: host` is set so rclone reaches `tailscale0` — Docker's default bridge netns can't see the host's Tailscale interface.

## Test

Run the backup script immediately without waiting for 04:00:

```bash
docker compose run --rm backup /backup.sh
```

Inspect either repo (`-e RESTIC_REPOSITORY=...` picks which one; falls back to `RESTIC_REPOSITORY_NAS` if you export that at the shell first):

```bash
# All snapshots on NAS
docker compose run --rm -e RESTIC_REPOSITORY=rclone:nas:restic/ backup restic snapshots

# Same on MEGA
docker compose run --rm -e RESTIC_REPOSITORY=rclone:mega:restic/ backup restic snapshots

# Snapshots for one tool
docker compose run --rm -e RESTIC_REPOSITORY=rclone:nas:restic/ backup restic snapshots --tag jellyfin

# Repo stats (see how well dedup is working)
docker compose run --rm -e RESTIC_REPOSITORY=rclone:nas:restic/ backup restic stats

# Mount the repo as a FUSE filesystem to browse historical files
# without a full restore (needs privileged mode; ad-hoc peek):
docker compose run --rm --privileged -e RESTIC_REPOSITORY=rclone:nas:restic/ backup \
    sh -c 'mkdir -p /mnt/r && restic mount /mnt/r'
```

## Restore

Interactive script on the host, dispatches by restore kind at the top.

```bash
./restore.sh
```

**Kind 1 — Tool data (restic snapshot)**: pick source repo (NAS / MEGA) → pick tool → pick snapshot → confirm → wipes and replaces the tool's whole `data/` directory (or, for jellyfin, its `config/`). Stop dependent services first. Files restored to a staging temp dir first and moved into place, so a restic-side error doesn't leave the target half-wiped. Restored files land root-owned (rclone in-container runs as root); `chown -R <uid>:<gid> <path>` afterward if the consuming service runs non-root. Exception: `crucial-docs` auto-chowns back to the host user because it's user-facing, not service-consumed.

**Kind 2 — Secrets vault file**: pick source tier (NAS / MEGA) → pick file from the remote listing → download to a fresh `/tmp/restore-XXXXXX/` dir. Never dropped back into `data/secrets/` — you unseal / decrypt / import from `/tmp` and decide where the plaintext (if any) goes. Blob types + how to unseal each:

- `bw-*.encrypted.json.age` — `age --decrypt -i <private-key> ...` then import into Bitwarden Desktop / Vaultwarden with master password
- `*.7z` — 7z + hardware key (or whatever the archive was sealed with)

NAS is the go-to for a routine restore (primary, holds every restic tool including immich, and has the full secrets mirror). Fall back to MEGA when NAS is unreachable or gone. For advanced restore (single file, browse historical state via FUSE, restore to alternate location), fall back to `docker compose run --rm backup restic ...` directly — see the restic docs.

## Migration from the old tarball setup

Previous version of this backup used `rclone copyto` tarballs to `nas-crypt:backups/<tool>/YYYY-MM-DD/data.tar.gz`. After switching to restic:

- Old tarballs at `nas-crypt:backups/` are NOT auto-expired anymore (nothing in this code touches that path). They'll sit at the NAS until manually purged:
  ```bash
  # From homelab/rclone (has [nas-crypt] configured):
  docker compose run --rm rclone purge "nas-crypt:backups/"
  ```
  Do this once you're comfortable that the restic repo is healthy and you've verified a test restore.

- Add `RESTIC_PASSWORD` to your shell rc / password manager — the old R2 env vars (already removed in the previous migration) don't need any handling this round.
