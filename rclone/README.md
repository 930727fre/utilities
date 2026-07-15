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

- **`gdrive`** — Google Drive remote (OAuth-authorized to your account)
- **`gdrive-crypt`** — crypt remote wrapping `gdrive:transcribe-backup/`
  with your chosen encryption password

Because OAuth needs a browser, `rclone config` is easiest run on any
machine with `rclone` installed + a browser (your laptop). Then copy
the resulting `rclone.conf` to `config/rclone.conf` in this directory.

### On your laptop (once)

```bash
rclone config
```

Interactive flow:

1. `n` → new remote → name `gdrive` → type `drive` → follow OAuth
   prompts, log in as your 5 TB Google account.
2. `n` → new remote → name `gdrive-crypt` → type `crypt`
   - `remote` = `gdrive:transcribe-backup/`   (the folder inside Drive
     where encrypted blobs live; will auto-create on first upload)
   - `filename_encryption` = `standard`       (filenames also encrypted;
     use `off` if you want to see wrapper names in the Drive UI)
   - `directory_name_encryption` = `true`
   - `password` = **choose a strong one, save it in Bitwarden**
   - `password2` (salt) = **generate one, save it too**
3. `q` to quit.

`rclone config file` prints the path (usually `~/.config/rclone/rclone.conf`).
Copy that file to this project:

```bash
scp ~/.config/rclone/rclone.conf your-server:/path/to/utilities/rclone/config/rclone.conf
```

### On the server (once)

```bash
cd utilities/rclone
chmod 600 config/rclone.conf   # secret file, restrict perms
./backup.sh "test-wrapper-name"   # trial run to confirm it works
```

## ⚠️ Losing the password = losing the data

`rclone crypt` uses AES-256 with your password + salt. There's **no
recovery mechanism**. Store the password + salt in a password manager
(Bitwarden, 1Password, whatever) the moment you set them.

## Files here

- `docker-compose.yml` — service definition using `rclone/rclone:latest`
- `backup.sh` — `./backup.sh <wrapper>` → sync `/bt/<wrapper>/` → gdrive
- `restore.sh` — `./restore.sh <wrapper>` → sync gdrive → `/bt/<wrapper>/`
- `config/rclone.conf` — **your** rclone credentials, gitignored

## What if I want to see what's in the backup

```bash
docker compose run --rm rclone lsd gdrive-crypt:transcribe/
docker compose run --rm rclone tree gdrive-crypt:transcribe/<wrapper>/
```

Filenames are decrypted on-the-fly for these read commands.
