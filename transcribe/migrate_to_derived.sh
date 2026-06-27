#!/usr/bin/env bash
# One-shot migration from the pre-refactor layout (everything in
# data/bt/<wrapper>/) to the new derived layout (annotated + Chinese SRTs
# under data/derived/<wrapper>/<stem>/).
#
# What it does:
#   *.zh-tw.srt              → derived/<wrapper>/<stem>/zh.srt
#   *.srt with ※ marker      → derived/<wrapper>/<stem>/annotated.srt
#   *.srt without ※          → leave in bt/ (the original BT-bundled English
#                              srt; the new pipeline will pair against it)
#   *.zh-tw.srt.error        → unlinked (stale, no useful info)
#
# After this runs, the new pipeline picks up where the old one left off:
# annotated.srt already in derived/ short-circuits whisper + annotation;
# zh.srt short-circuits translation. HLS still needs to transcode once
# per video (no pre-existing artifact).
#
# Run from transcribe/. Use --dry-run first.

set -euo pipefail

DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  ""|--apply) DRY=0 ;;
  *) echo "usage: $0 [--dry-run|--apply]"; exit 2 ;;
esac

BT_ROOT="data/bt"
DERIVED_ROOT="data/derived"

if [[ ! -d "$BT_ROOT" ]]; then
  echo "ERROR: $BT_ROOT not found — run from transcribe/" >&2
  exit 1
fi

do_move() {
  local src="$1" dst="$2"
  if [[ $DRY -eq 1 ]]; then
    echo "DRY mv: $src"
    echo "    →  $dst"
  else
    mkdir -p "$(dirname "$dst")"
    if [[ -e "$dst" ]]; then
      echo "SKIP (dst exists): $dst"
    else
      mv "$src" "$dst"
      echo " mv: $src"
      echo "  →  $dst"
    fi
  fi
}

do_unlink() {
  local p="$1"
  if [[ $DRY -eq 1 ]]; then
    echo "DRY rm: $p"
  else
    rm -f "$p"
    echo " rm: $p"
  fi
}

count_zh=0
count_annotated=0
count_keep_original=0
count_err_unlinked=0

# ─── Pass 1: zh-tw subs → derived/.../zh.srt ────────────────────────────
while IFS= read -r -d '' src; do
  rel="${src#$BT_ROOT/}"           # <wrapper>/<...>/<stem>.zh-tw.srt
  wrapper="${rel%%/*}"             # <wrapper>
  fname="${src##*/}"               # <stem>.zh-tw.srt
  stem="${fname%.zh-tw.srt}"       # <stem>
  dst="$DERIVED_ROOT/$wrapper/$stem/zh.srt"
  do_move "$src" "$dst"
  count_zh=$((count_zh + 1))
done < <(find "$BT_ROOT" -type f -name "*.zh-tw.srt" -print0)

# ─── Pass 2: stale .zh-tw.srt.error files (no longer useful) ────────────
while IFS= read -r -d '' src; do
  do_unlink "$src"
  count_err_unlinked=$((count_err_unlinked + 1))
done < <(find "$BT_ROOT" -type f -name "*.zh-tw.srt.error" -print0)

# ─── Pass 3: regular .srt — split annotated vs original English ─────────
# ※ marker is UTF-8 0xE2 0x80 0xBB. Annotation always appends an
# `※ annotated` sentinel cue OR per-cue `※ <note>` lines, so any presence
# of the byte sequence signals "this file went through annotate.py".
while IFS= read -r -d '' src; do
  fname="${src##*/}"
  # zh-tw.srt was handled in pass 1 — skip here.
  [[ "$fname" == *.zh-tw.srt ]] && continue

  rel="${src#$BT_ROOT/}"
  wrapper="${rel%%/*}"
  stem="${fname%.srt}"

  if grep -q $'\xe2\x80\xbb' "$src" 2>/dev/null; then
    dst="$DERIVED_ROOT/$wrapper/$stem/annotated.srt"
    do_move "$src" "$dst"
    count_annotated=$((count_annotated + 1))
  else
    echo "KEEP (no ※, treated as original BT English srt): $src"
    count_keep_original=$((count_keep_original + 1))
  fi
done < <(find "$BT_ROOT" -type f -name "*.srt" -print0)

echo ""
echo "── Summary ───────────────────────────────────────────────"
echo "  *.zh-tw.srt → zh.srt:              $count_zh"
echo "  *.srt (with ※) → annotated.srt:    $count_annotated"
echo "  *.srt (no ※) kept in bt/:          $count_keep_original"
echo "  *.zh-tw.srt.error unlinked:        $count_err_unlinked"
echo ""

if [[ $DRY -eq 1 ]]; then
  echo "DRY RUN — nothing was moved or deleted."
  echo "Re-run as: $0 --apply"
fi
