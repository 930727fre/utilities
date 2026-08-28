#!/bin/sh
# Daily Bitwarden vault export → age-wrapped → dropped into
# ../backup/data/secrets/ for the backup container's rclone-copy
# branch to fan out to NAS + MEGA.
#
# Two encryption layers:
#   1. Bitwarden's own encrypted_json export (Argon2 + AES, keyed by
#      the master password — same encryption the vault uses at rest)
#   2. age wrap keyed by an offline public key (X25519 + ChaCha20-
#      Poly1305). The private key never touches this container or the
#      host — recovery requires bringing it out from cold storage.
#
# Recovery in a disaster (Bitwarden.com dies):
#   age -d -i <offline-key> bw-YYYY-MM-DD.encrypted.json.age > bw.json
#   Import bw.json into Bitwarden Desktop with master password → vault
#   back.
set -eu

# Defensive: if a previous run died mid-way, the container filesystem
# may still hold a live bw session. Wipe before starting so `bw login`
# doesn't hit "You are already logged in" and abort.
bw logout > /dev/null 2>&1 || true

STEP="init"
notify() {
    ENCODED=$(printf '%s' "$1" | jq -sRr @uri)
    curl -sfSL "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=${ENCODED}" > /dev/null || true
}
trap 'notify "❌ | BitwardenExport | FAILED at ${STEP}"' EXIT

STEP="bw+login"
# API key login (BW_CLIENTID + BW_CLIENTSECRET from env). Independent
# of master password — revoke this key alone if the container is
# compromised without touching the master password.
bw login --apikey > /dev/null

STEP="bw+unlock"
# Master password is still required to decrypt vault contents
# (Bitwarden's zero-knowledge design — API key alone can't read
# entries). Session token goes into BW_SESSION for the export call.
BW_SESSION=$(bw unlock --raw "${BW_PASSWORD}")
export BW_SESSION

STEP="bw+sync"
bw sync > /dev/null

STEP="bw+export"
# encrypted_json: Bitwarden-native format, Argon2-derived AES key,
# re-importable into any Bitwarden client (Desktop/mobile/web) with
# the password used here. We reuse master password so recovery needs
# one secret, not two.
TMPFILE=$(mktemp)
bw export --format encrypted_json --password "${BW_PASSWORD}" --output "$TMPFILE" > /dev/null

STEP="age+wrap"
# Outer age layer. Even if master password leaks, the on-disk blob is
# still unreadable without the offline age private key. Public-key
# crypto (X25519) so this container needs only the recipient string.
DATE=$(date +%Y-%m-%d)
OUT="/output/bw-${DATE}.encrypted.json.age"
age -r "${AGE_RECIPIENT}" -o "$OUT" "$TMPFILE"
rm -f "$TMPFILE"

STEP="bw+logout"
bw logout > /dev/null || true

trap - EXIT
notify "✅ | BitwardenExport | ${DATE}"
