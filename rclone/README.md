# rclone: encrypted offsite backup for bt wrappers

Push / pull one BT wrapper (whole aria2 download directory) to Google
Drive via `rclone crypt` — files are AES-encrypted before upload, so
Google's content-scanner can't identify the media inside. Storage
counts against your 5 TB Google plan; egress is free but slow on a
consumer uplink.

Use case: `/bt/` fills up, you want to free space but keep the option
to re-watch later without re-downloading the torrent.

## Workflow

```
disk full         →  ./backup.sh "<wrapper name>"     (uploads to gdrive-crypt)
                  →  UI ✕ delete torrent               (cascade removes /bt/, /artifact/…, sentinel)
                  →  wrapper gone locally, srts in /archive/ preserved

want to rewatch   →  ./restore.sh "<wrapper name>"   (downloads back into /bt/)
                  →  transcribe scan tick picks it up in ~30 s
                  →  filter_wrapper Opus runs (~$0.10) → archive tier catches → canonical srts land instantly
                  →  Jellyfin sees the show again
```

## One-time setup

You need an `rclone.conf` with two remotes:

- **`gdrive`** — Google Drive remote (OAuth-authorized to your account,
  with your own OAuth Client ID from Google Cloud Console)
- **`gdrive-crypt`** — crypt remote wrapping `gdrive:transcribe-backup/`
  with your chosen encryption password + salt

Because OAuth needs a browser, `rclone config` is easiest run on any
machine with `rclone` installed + a browser (your laptop). Then copy
the resulting `rclone.conf` to `config/rclone.conf` in this directory.

See the OAuth Client ID walkthrough in `rclone.org/drive/#making-your-own-client-id`
— the built-in shared key is heavily rate-limited and unusable for
large backups. Publish the OAuth app to production (in Google Cloud
Console → OAuth consent screen) so the token doesn't expire every 7
days.

After `rclone config` completes on the laptop:

```bash
rclone config file                                             # print path
scp ~/.config/rclone/rclone.conf server:/…/utilities/rclone/config/rclone.conf
```

Server side:

```bash
chmod 600 config/rclone.conf
```

## ⚠️ Losing the password = losing the data

`rclone crypt` uses AES-256 with your password + salt. There's **no
recovery mechanism**. Store the password + salt in a password manager
(Bitwarden, 1Password, whatever) the moment you set them.

## Everyday commands

All commands run through the rclone container (no rclone install needed
on the host). Assumes you're in `utilities/rclone/`.

### Backup a wrapper

```bash
./backup.sh "<wrapper name>"
```

or equivalent explicit form:

```bash
docker compose run --rm rclone sync \
    "/bt/<wrapper name>" \
    "gdrive-crypt:transcribe/<wrapper name>" \
    --progress --stats-one-line --checksum
```

### Restore a wrapper

```bash
./restore.sh "<wrapper name>"
```

or:

```bash
docker compose run --rm rclone sync \
    "gdrive-crypt:transcribe/<wrapper name>" \
    "/bt/<wrapper name>" \
    --progress --stats-one-line --checksum
```

### List what's backed up

```bash
docker compose run --rm rclone lsd gdrive-crypt:transcribe/
```

Prints each wrapper as a directory line with mtime and size. Filenames
are decrypted on the fly — from your side these look like the original
wrapper names.

### Check size of one backed-up wrapper

```bash
docker compose run --rm rclone size gdrive-crypt:transcribe/"<wrapper name>"
```

### List files inside one wrapper on remote

```bash
docker compose run --rm rclone lsf gdrive-crypt:transcribe/"<wrapper name>"
```

### Delete a wrapper from remote

```bash
docker compose run --rm rclone purge gdrive-crypt:transcribe/"<wrapper name>"
```

### Total remote size / free space check

```bash
docker compose run --rm rclone about gdrive:
```

Prints your Google Drive total / used / free (crypt overhead is
negligible — a few bytes per file for the encryption header).

### Verify one backup matches local

```bash
docker compose run --rm rclone check \
    "/bt/<wrapper name>" \
    "gdrive-crypt:transcribe/<wrapper name>"
```

Reports any files that differ. Uses checksums, not just mtime — slower
but authoritative.

### Dry-run a backup (see what would upload without actually doing it)

```bash
docker compose run --rm rclone sync --dry-run \
    "/bt/<wrapper name>" \
    "gdrive-crypt:transcribe/<wrapper name>" \
    --progress
```

## Files here

- `docker-compose.yml` — service definition using `rclone/rclone:latest`
- `backup.sh` — thin wrapper around `docker compose run … sync /bt/ gdrive-crypt:`
- `restore.sh` — same, reversed direction
- `config/rclone.conf` — **your** rclone credentials, gitignored
- `.gitignore` — keeps rclone.conf out of the repo
