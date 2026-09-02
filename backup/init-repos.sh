#!/bin/sh
# One-time restic repo initialization. Run once per fresh install
# before enabling the daily backup:
#
#   docker compose run --rm backup /init-repos.sh
#
# Idempotent: repos that already exist are skipped, so re-running is
# safe (useful for adding just one new remote later — set the env,
# rerun, existing repos untouched, new one gets inited).
#
# Deliberately separate from backup.sh: daily backups should never
# auto-init, because that path can't distinguish "repo missing" from
# "backend unreachable" and used to trigger spurious inits on
# transient NAS timeouts. Setup is a one-time human-driven action;
# daily backup assumes the setup has been done.
set -eu

for REPO_NAME in NAS MEGA; do
    eval "REPO_URL=\$RESTIC_REPOSITORY_$REPO_NAME"
    echo ""
    echo "─── $REPO_NAME ($REPO_URL) ───"
    if restic --repo "$REPO_URL" snapshots >/dev/null 2>&1; then
        echo "already initialized — skipping"
    else
        echo "not initialized — running restic init..."
        restic --repo "$REPO_URL" init
        echo "✓ $REPO_NAME initialized"
    fi
done

echo ""
echo "Done. Enable the daily backup with: docker compose up -d --build"
