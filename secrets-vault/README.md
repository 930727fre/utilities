# secrets-vault

Container that mirrors small, monolithic, pre-sealed blobs to `nas:secrets/` and `mega:secrets/` via nightly `rclone copy`. Weekly `rclone check --download` verifies byte-for-byte integrity on both tiers.

Two source flows produce blobs into `data/`:

1. **Bitwarden vault export** — *PLANNED, not yet implemented.* Requires the offline-stored age public key before the wrapping step can be built. See "Planned: Bitwarden export flow" below.
2. **User-dropped pre-sealed archives** — *IMPLEMENTED.* Any file placed in `data/` (hardware-encrypted 7z / age-wrapped archive / whatever) gets picked up by the next nightly tick and mirrored to both tiers as-is.

Why this container is separate from `utilities/backup` (which uses restic): the blobs handled here are already sealed, monolithic, and updated as-a-whole (never incrementally). Restic's dedup and per-file restore are moot for that pattern — one blob per update, each fully independent. Plain `rclone copy` is the right primitive.

## Retention

Infinite everywhere:

- **`data/`** (local host): user manages by hand; the container never deletes.
- **`nas:secrets/`**: no `rclone delete`, no rotation.
- **`mega:secrets/`**: same.

Rationale: each blob is small (`bw export` ~50 KB, user archives typically <100 MB), updates are infrequent, and losing any historical version would be regret waiting to happen. Not worth the retention logic — 10 years of history is still measurable in MB for the Bitwarden side.

## Encryption model

**Nothing in this container encrypts anything.** All blobs entering `data/` are expected to already be strongly sealed by whatever mechanism the source used:

- Bitwarden exports (once the planned flow lands): sealed by `bw export --format encrypted_json --password $BW_PASSWORD` (inner) + `age -r $AGE_RECIPIENT` (outer). The user-provided age wrap is what makes the blob resistant to any downstream compromise.
- User-provided archives: sealed by whatever the user did offline. E.g. an air-gapped 7z with hardware-key-derived encryption. The container treats these as opaque bytes and does not attempt to add another layer.

The `rclone` transport uses the plain `[nas]` + `[mega]` remotes (not any crypt wrapper) — the blobs are their own confidentiality layer.

## Verification

Every night after the copy step: nothing extra (`rclone copy` inherently verifies size + mtime match).

Every Sunday: `rclone check --download /data/ <remote>:secrets/` for both tiers. `--download` forces rclone to fetch every remote object and byte-compare against the local copy — catches bit-rot on either tier. Runtime is bounded by total `data/` size, which is expected to stay tiny.

Failures bubble to the outer `EXIT` trap → Telegram `❌ | SecretsVault | FAILED at <step>`. Success + weekly check adds a `Weekly full verify OK` line to the standard success message.

## Recovery

**For a user-dropped sealed archive** (e.g. hardware-encrypted 7z):

1. Retrieve the blob from `mega:secrets/<filename>` (via MEGA web UI or `rclone copy mega:secrets/... /tmp/`) or from `nas:secrets/<filename>` (friend's Synology, either via WebDAV/rclone or sudo on the NAS host).
2. Unseal with whatever tool + key the archive was originally created with (7z + hardware key / age + private key / etc.).

Zero dependency on this container, on `rclone.conf`, or on any password stored elsewhere. The archive is the archive.

**For a Bitwarden export** (once the planned flow is implemented):

1. Retrieve `bw-YYYY-MM-DD.encrypted.json.age` from either tier.
2. `age --decrypt -i <private-key> <blob> > vault.encrypted.json` (outer).
3. Import `vault.encrypted.json` into Bitwarden Desktop / Vaultwarden / another compatible client, entering the master password (inner).
4. Vault is back.

## Adding a new sealed blob (implemented flow)

```sh
cp ~/Downloads/my-sealed-archive.7z ~/utilities/secrets-vault/data/
# tonight's 04:00 tick picks it up automatically
```

Or trigger immediately without waiting:

```sh
cd ~/utilities/secrets-vault
docker compose run --rm secrets-vault /backup.sh
```

The filename you place is the filename that lands on both tiers (flat layout in `data/` — no subdirectory structure required or expected).

## Planned: Bitwarden export flow

**Waiting on retrieval of the offline-stored age public key before building.**

Container will additionally run nightly:

```sh
bw config server https://vault.bitwarden.com
bw login --apikey
SESSION=$(bw unlock --raw --passwordenv BW_PASSWORD)
bw sync --session "$SESSION"

TMP=$(mktemp)
bw export --session "$SESSION" \
   --format encrypted_json --password "$BW_PASSWORD" \
   --output "$TMP"
age -r "$AGE_RECIPIENT" "$TMP" -o "/data/bw-$(date +%Y-%m-%d).encrypted.json.age"
bw logout
rm -f "$TMP"
```

Then the existing `rclone copy` pass picks up the new `.age` file alongside any manually-dropped archives.

Additional env at that point:

- `BW_CLIENTID` / `BW_CLIENTSECRET` — personal API key from vault.bitwarden.com → Account settings → Security → Keys
- `BW_PASSWORD` — Bitwarden master password (unlocks vault + doubles as the export encryption password)
- `AGE_RECIPIENT` — age public key (safe to commit if desired)

Dockerfile additions: `@bitwarden/cli` (needs node), `age` (alpine community repo).

## What's NOT covered

- **Bitwarden attachments**. `bw export` metadata only; attachment blobs need a separate `bw get attachment` loop. Add when any items grow attachments worth preserving.
- **Bitwarden Sends**. Ephemeral by design.
- **Bitwarden Organization vaults**. Personal vault only; org exports need `--organizationid`.
- **File format inspection of user-dropped archives.** This container copies opaque bytes; if the user drops a corrupted-at-creation file, backup mirrors the corruption. Verify integrity of source archives before dropping.
