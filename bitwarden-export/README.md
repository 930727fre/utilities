# bitwarden-export

Daily 03:00 Asia/Taipei snapshot of the Bitwarden.com vault → age-wrapped → dropped into `../backup/data/secrets/`. The `backup` container picks it up on its 04:00 tick and mirrors to NAS + MEGA via the rclone-copy branch.

Only exists to survive one specific disaster: **Bitwarden.com itself dies and the online vault becomes unreachable**. Everyday password access still goes through Bitwarden Desktop / mobile as normal.

## Two encryption layers

```
plaintext vault
    │
    │ bw export --format encrypted_json --password <master password>
    ▼
Bitwarden encrypted_json     (Argon2 + AES, keyed by master password)
    │
    │ age -r <pubkey>
    ▼
bw-YYYY-MM-DD.encrypted.json.age    (X25519 + ChaCha20-Poly1305, keyed by offline age key)
```

Both must be peeled to recover:

1. `age -d -i <offline-private-key> bw-*.age > bw.encrypted.json`
2. Import `bw.encrypted.json` into Bitwarden Desktop / Vaultwarden with the master password

Point of the second layer: even if the master password leaks (keylogger, phishing, coercion), the on-disk blob is still unreadable without the offline age private key. The private key never touches this container or the host — it lives in cold storage (offline medium, physical safe, etc.).

## Setup

Four env vars have to be exported in the shell before `docker compose up -d --build`:

### 1. Bitwarden API key — `BW_CLIENTID`, `BW_CLIENTSECRET`

Vault Web → Settings → My Account → API key → "View API key". Enter master password. Copy both values.

The API key authenticates the container to Bitwarden's API. Master password is still required (Bitwarden is zero-knowledge — API key alone can't decrypt anything), but revoking the API key from the same panel disables this container instantly without touching the master password.

```bash
export BW_CLIENTID='user.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
export BW_CLIENTSECRET='xxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

### 2. Master password — `BW_PASSWORD`

Reused for two things: `bw unlock` to decrypt vault contents inside the container, and as the export password for `bw export --format encrypted_json`. Using the same password for both keeps recovery to one secret.

```bash
export BW_PASSWORD='<master password>'
```

### 3. age recipient — `AGE_RECIPIENT`

Public key of the offline age keypair. Container only needs the public half (public-key crypto). Value starts with `age1...`.

```bash
export AGE_RECIPIENT='age1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

## Deploy

```bash
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
# plus the four env vars above
docker compose up -d --build
```

Container runs crond internally. First export fires at next 03:00 Taipei. To trigger immediately for testing:

```bash
docker compose exec bitwarden-export /export.sh
```

## Bootstrap: throwaway pubkey before the real one arrives

If the real offline age key isn't retrievable yet (safe deposit box run pending, etc.), the pipeline can still be brought online with a **throwaway pubkey**:

```bash
# On any machine, generate a fresh keypair
age-keygen -o /tmp/throwaway.key
# Note the public key ("Public key: age1..." line in stderr)
grep 'public key' /tmp/throwaway.key   # or read the file directly
```

Set `AGE_RECIPIENT` to that pubkey. **Before destroying the private key, run one full end-to-end test:**

```bash
docker compose exec bitwarden-export /export.sh
# check backup/data/secrets/ for the new .age blob
ls -lh ../backup/data/secrets/

# Decrypt with the throwaway private key
age -d -i /tmp/throwaway.key ../backup/data/secrets/bw-$(date +%Y-%m-%d).encrypted.json.age > /tmp/bw.json

# Manually import /tmp/bw.json into a scratch Bitwarden Desktop install
# and verify the master password unlocks it. Only then destroy the key:
shred -u /tmp/throwaway.key /tmp/bw.json
```

Any `.age` blobs produced during the throwaway window are permanently undecryptable — that's fine, they were only there to prove the pipeline. When the real offline pubkey arrives:

1. Swap `AGE_RECIPIENT` env, `docker compose up -d`
2. Wait one day (or trigger `/export.sh` manually) — new blob sealed to real key
3. Verify the new blob decrypts with the real private key (bring it out briefly)
4. **Delete the throwaway-era blobs from `backup/data/secrets/` and from both tiers** (`rclone delete nas:secrets/bw-<date>.encrypted.json.age`, same for `mega:`). Otherwise a future disaster response wastes time trying to decrypt files that can't be decrypted.

## Related

- `../backup/README.md` — fans this blob out to NAS + MEGA on the daily 04:00 pass, weekly Sunday `rclone check --download` verifies bit-integrity on both tiers
- Recovery drill (quarterly): pull latest blob back via `../backup/restore.sh` Kind 2 → age-decrypt → import to scratch Bitwarden Desktop → confirm it actually works. If it doesn't, the backup is theatre.
