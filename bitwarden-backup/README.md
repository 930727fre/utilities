# bitwarden-backup

**Status: PLANNED — spec only, not yet implemented.** Waiting on retrieval of the offline-stored age public key before scaffolding `Dockerfile` + `docker-compose.yml` + `backup.sh`.

Daily container that logs into `vault.bitwarden.com` via personal API key, exports the whole vault, and drops a doubly-encrypted blob into `exports/` for the `utilities/backup` restic pipeline to pick up. Purpose: survive Bitwarden.com going away — the blob is decryptable offline with `age --decrypt` + master password, no Bitwarden infrastructure required.

## Encryption: two independent layers

```
vault
  ↓  bw export --format encrypted_json --password $BW_PASSWORD
Bitwarden-encrypted JSON (PBKDF2-SHA256 → AES-256-CBC + HMAC-SHA256, format documented)
  ↓  age -r $AGE_RECIPIENT
bw-YYYY-MM-DD.encrypted.json.age  ← final artifact
```

Why both:

- **Inner layer** (Bitwarden's own encrypted export). Same PBKDF2-derived AES-256 that unlocks the vault day-to-day. Master password unlocks it.
- **Outer layer** (age with pubkey). X25519 + ChaCha20-Poly1305. Only the private key holder can peel this off — password strength is irrelevant to the outer.

Together: an attacker with the blob needs both the age private key *and* the master password. Either alone yields nothing. And on this host the file is never plaintext — the `bw export` output already carries the inner encryption before `age` touches it, and the pipeline never materializes an unencrypted JSON on disk.

## Recovery when vault.bitwarden.com is gone

1. Retrieve the latest `bw-*.encrypted.json.age` (from `utilities/backup` restic → friend's NAS, or wherever you fanned it out to).
2. Decrypt outer: `age --decrypt -i <private-key-file> bw-*.encrypted.json.age > bw.encrypted.json` (or via age-plugin-yubikey if the private key lives on a hardware token).
3. Import inner into any Bitwarden-compatible client — Bitwarden Desktop (works offline), Vaultwarden, 1Password, KeePass all read the `encrypted_json` format. Enter the master password.
4. Vault is back.

Zero dependency on Bitwarden.com, on this host, or on restic (if the blob is fanned out to a second location).

**Root of trust**: the age private key. Owner keeps three offline copies, so single-key loss is not the failure mode to plan for.

## Auth setup (one-time, before first run)

1. Web vault → Account settings → Security → Keys → **View API key**. Save `client_id` + `client_secret`.
2. On the host shell (per repo convention, no `.env`):
   ```sh
   export BW_CLIENTID=user.<...>
   export BW_CLIENTSECRET=<...>
   export BW_PASSWORD=<master password>       # unlocks vault + inner encryption
   export AGE_RECIPIENT=age1<...>              # public key, safe to commit if desired
   ```
3. `docker compose up -d`. Cron inside the container fires nightly.

The API key is a per-account credential — revoke it in the web vault to instantly kill this container's access without touching the master password.

## File layout (once built)

```
bitwarden-backup/
  Dockerfile           # alpine + node20 + @bitwarden/cli + age + dcron
  docker-compose.yml   # restart: unless-stopped, mounts data/ and exports/
  backup.sh            # bw login → unlock → export encrypted → age wrap → cleanup
  crontab              # 0 4 * * * /backup.sh
  data/                # bw CLI's session cache (BITWARDENCLI_APPDATA_DIR)
  exports/             # bw-YYYY-MM-DD.encrypted.json.age  (30-day local rotation)
```

The `exports/` directory then gets picked up by `utilities/backup` — add `bitwarden-backup` to that repo's `TOOLS` env var and mount `bitwarden-backup/exports` as `/tools/bitwarden-backup/data`. Restic dedups incrementally; 30 daily files at ~50 KB each → negligible repo growth.

## What's NOT covered

- **Attachments** (Bitwarden supports file attachments on items — passport scans, recovery codes as PDFs, etc.). `bw export` metadata only; attachment blobs need a separate `bw get attachment` loop. Add later if any items grow attachments worth preserving.
- **Sends** (one-time share links). Ephemeral by design; not in export.
- **Organization vaults** (if joined to any orgs). Personal vault only; org exports need a separate `--organizationid` invocation.

TOTP seeds embedded in items *are* included in the export — no separate handling needed for 2FA restoration.
