#!/bin/bash
# Bring every tool up in dependency order with a fresh build.
# A failure in one tool prints a warning and continues — one broken container
# should not prevent the rest from being brought up.

cd "$(dirname "$0")"

# Order: infrastructure (ollama, gpu-broker) first, then GPU consumers, then
# UI/utility tools. cloudflared last so the tunnel opens with every route
# already serving 200 instead of 502.
SERVICES=(
  gpu-broker
  marker-pipeline
  xyt
  flashcard
  keyboard
  transcribe
  free2speak
  clock
  recorder
  clipboard
  backup
  cloudflared
)

for svc in "${SERVICES[@]}"; do
  compose="$svc/docker-compose.yml"
  if [ ! -f "$compose" ]; then
    continue
  fi
  echo "=== $svc ==="
  docker compose -f "$compose" up -d --build || echo "  (failed — continuing)"
done
