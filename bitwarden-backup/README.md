# bitwarden-backup

**Status: PLANNED — spec only, not yet implemented.** Waiting on retrieval of the offline-stored age public key before scaffolding `Dockerfile` + `docker-compose.yml` + `backup.sh`.

Daily container that logs into `vault.bitwarden.com` via personal API key, exports the whole vault, wraps it in a second encryption layer, and pushes the resulting blob directly to NAS + MEGA via `rclone copy`. Purpose: survive Bitwarden.com going away — the blob is decryptable offline with `age --decrypt` + master password, no Bitwarden infrastructure required.

## Why this doesn't live in `utilities/backup`

The main backup pipeline is restic-based: it excels at incremental, deduplicated, mutable state. Bitwarden's daily export doesn't fit that shape:

- **Every blob is a fresh AES output** — pre-encrypted, dedup finds nothing.
- **Each blob is a monolithic full snapshot** — restic's snapshot concept adds no value on top; the filename already carries the date.
- **Recovery should have the fewest dependencies possible** — decrypting an `.age` file needs one tool (age) and one key (private key). Adding `restic` to the recovery chain trades simplicity for zero gain.

Plain `rclone copy` is the primitive that matches: opaque file, dated name, push to two backends, done.

## Encryption: two independent layers

```
vault
  ↓  bw export --format encrypted_json --password $BW_PASSWORD
Bitwarden-encrypted JSON (PBKDF2-SHA256 → AES-256-CBC + HMAC-SHA256, format documented)
  ↓  age -r $AGE_RECIPIENT
bw-YYYY-MM-DD.encrypted.json.age  ← final artifact, doubly encrypted
  ↓  rclone copy (to nas + mega, in parallel)
nas:bitwarden/bw-YYYY-MM-DD.encrypted.json.age
mega:bitwarden/bw-YYYY-MM-DD.encrypted.json.age
```

Why both encryption layers:

- **Inner** (Bitwarden's own encrypted export). Same PBKDF2-derived AES-256 that unlocks the vault day-to-day. Master password unlocks it.
- **Outer** (age with pubkey). X25519 + ChaCha20-Poly1305. Only the private key holder can peel this off — password strength is irrelevant to the outer.

Together: an attacker with the blob needs both the age private key *and* the master password. Either alone yields nothing. And on this host the file is never plaintext — the `bw export` output already carries the inner encryption before `age` touches it, and the pipeline never materializes an unencrypted JSON on disk.

## Recovery when vault.bitwarden.com is gone

1. Grab any `bw-*.encrypted.json.age` — from `data/` on this host (full history kept locally, files are tiny), or `nas:bitwarden/`, or `mega:bitwarden/`.
2. Decrypt outer: `age --decrypt -i <private-key-file> bw-*.encrypted.json.age > bw.encrypted.json` (or via age-plugin-yubikey if the private key lives on a hardware token).
3. Import inner into any Bitwarden-compatible client — Bitwarden Desktop (works offline), Vaultwarden, 1Password, KeePass all read the `encrypted_json` format. Enter the master password.
4. Vault is back.

Zero dependency on Bitwarden.com, on this host, on restic, on any specific storage backend. The `.age` blob is fully self-contained.

**Root of trust**: the age private key. Owner keeps three offline copies, so single-key loss is not the failure mode to plan for.

## Retention

**Local (`data/`)**: infinite. Files are tiny (~50 KB each), 10 years = ~18 MB. Kept for offline browsing without needing to touch NAS/MEGA.

**NAS (`nas:bitwarden/`)**: infinite. Same rationale.

**MEGA (`mega:bitwarden/`)**: infinite. Same rationale.

No `find -mtime -delete`. The whole point of these blobs is historical recoverability, and their size makes retention pruning unnecessary.

## Auth setup (one-time, before first run)

1. Web vault → Account settings → Security → Keys → **View API key**. Save `client_id` + `client_secret`.
2. On the host shell (per repo convention, no `.env`):
   ```sh
   export BW_CLIENTID=user.<...>
   export BW_CLIENTSECRET=<...>
   export BW_PASSWORD=<master password>       # unlocks vault + inner encryption
   export AGE_RECIPIENT=age1<...>              # public key (or ssh-ed25519 fingerprint), safe to commit if desired
   ```
3. `~/rclone.conf` must have `[nas]` and `[mega]` remotes (already set up for `utilities/backup`; this container reuses the same file).
4. `docker compose up -d`. Cron inside the container fires nightly.

The API key is a per-account credential — revoke it in the web vault to instantly kill this container's access without touching the master password.

## File layout (once built)

```
bitwarden-backup/
  Dockerfile           # alpine + node20 + @bitwarden/cli + age + rclone + dcron
  docker-compose.yml   # restart: unless-stopped, mounts rclone.conf + data/
  backup.sh            # bw login → unlock → export encrypted → age wrap → rclone copy → cleanup
  crontab              # 0 4 * * * /backup.sh
  data/                # bw-YYYY-MM-DD.encrypted.json.age (all history kept, .gitignored)
```

`backup.sh` skeleton:

```sh
set -eu
STAMP=$(date +%Y-%m-%d)
OUT=/data/bw-$STAMP.encrypted.json.age
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

bw config server https://vault.bitwarden.com
bw login --apikey
SESSION=$(bw unlock --raw --passwordenv BW_PASSWORD)
bw sync --session "$SESSION"

bw export --session "$SESSION" \
   --format encrypted_json --password "$BW_PASSWORD" \
   --output "$TMP"

age -r "$AGE_RECIPIENT" "$TMP" -o "$OUT"

bw logout

rclone copy "$OUT" nas:bitwarden/
rclone copy "$OUT" mega:bitwarden/

# Telegram notify (bot token + chat id via env)
```

## What's NOT covered

- **Attachments** (Bitwarden supports file attachments on items — passport scans, recovery codes as PDFs, etc.). `bw export` metadata only; attachment blobs need a separate `bw get attachment` loop. Add later if any items grow attachments worth preserving.
- **Sends** (one-time share links). Ephemeral by design; not in export.
- **Organization vaults** (if joined to any orgs). Personal vault only; org exports need a separate `--organizationid` invocation.

TOTP seeds embedded in items *are* included in the export — no separate handling needed for 2FA restoration.
