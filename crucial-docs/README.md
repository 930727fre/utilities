# crucial-docs

A passive folder for personal file-tree stuff worth keeping around — certificates, transcripts, IDs, insurance, tax records, medical scans. Loss of any single file here is annoying (usually re-requestable from the issuing institution) but not catastrophic — the blast radius stays per-file.

No container, no cron, no service. You drop files into `data/` by hand; the backup pipeline picks it up.

## Backup treatment

Handled by [`utilities/backup`](../backup) as a standard selfhost tool — same tier as `flashcard`, `free2speak`, `jellyfin`:

- **Both tiers**: NAS + MEGA
- **Retention**: 90-day on both (uniform with other selfhost tools)
- **Verify**: weekly structural + monthly 10% sampled read-data check (uniform)

If you have blobs whose loss would be **genuinely catastrophic** (Bitwarden vault export, hardware-encrypted archive of irreplaceable keys), those live in [`../secrets-vault/`](../secrets-vault) — infinite retention, plain rclone copy of pre-sealed monolithic files. Don't put those here.

## Adding files

```sh
cp ~/Downloads/my-transcript.pdf ~/utilities/crucial-docs/data/
# tonight's 04:00 tick picks it up automatically
```

Organize `data/` however you want — the whole tree gets snapshotted. Suggested layout:

```
data/
├── certificates/     # professional / education
├── transcripts/      # academic records
├── id/               # passport scans, driver's license
├── insurance/        # policy docs, claim history
├── tax/              # returns, receipts
├── medical/          # scans, prescriptions
└── ...
```

## What NOT to put here

- **Anything that would be catastrophic if lost** — belongs in [`../secrets-vault/`](../secrets-vault) with infinite retention, not here (this folder rotates at 90 days like other selfhost tools).
- **Bitwarden vault** — has its own encryption discipline; lives in `secrets-vault`.
- **Working files you're actively editing** — this folder is for stable references, not scratch space.
- **Multi-GB media that changes often** — restic dedup handles small changes fine; large frequently-changing binaries burn snapshot storage.
- **Secrets meant for automation** — those go in your password manager, not a passive folder.
