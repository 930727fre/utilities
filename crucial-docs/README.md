# crucial-docs

A passive folder for personal documents that are painful to lose and painful to recreate — certificates, transcripts, IDs, insurance, tax records, medical scans, anything you'd feel sick about losing.

No container, no cron, no service. You drop files into `data/` by hand; the backup pipeline picks it up.

## Backup treatment

Handled by [`utilities/backup`](../backup) as a standard tool (mount, `TOOLS` list, restic pipeline). Special treatment vs the other tools:

- **Both tiers**: NAS + MEGA (not MEGA-excluded — this stuff is more important than immich, not less)
- **Retention**: infinite on both tiers (listed in `NO_FORGET_TOOLS`) — you don't want a bounded window on a legal document you last touched three years ago

Restic still encrypts (via `RESTIC_PASSWORD`) — the two-layer bitwarden dance isn't warranted here because these files usually aren't format-sensitive: worst case a PDF certificate leaks its own contents, unlike a Bitwarden vault which is the master key to your whole digital life. Single strong `RESTIC_PASSWORD` is enough for this bucket.

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

- **Bitwarden vault** — has its own encryption discipline (see [`../bitwarden-backup/`](../bitwarden-backup)), lives in its own pipeline
- **Working files you're actively editing** — this folder is for stable references, not scratch space
- **Anything so large that daily backup delta becomes expensive** — restic dedup handles small changes fine; multi-GB video files that change often go elsewhere
- **Secrets meant for automation** — those go in your password manager, not a passive folder
