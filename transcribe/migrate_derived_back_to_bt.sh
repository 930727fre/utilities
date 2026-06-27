#!/usr/bin/env bash
# Reverse migration: undo migrate_to_derived.sh.
# Move pipeline output back into bt/ sidecar layout:
#
#   data/derived/<wrapper>/<stem>/annotated.srt → data/bt/<wrapper>/<stem>.srt
#   data/derived/<wrapper>/<stem>/zh.srt        → data/bt/<wrapper>/<stem>.zh-tw.srt
#
# Everything else in derived/ (master.m3u8, seg_*.ts, .error stamps,
# english.srt) is unused by the 2a9445b-era pipeline and gets reported
# but NOT auto-deleted — separate step at the end.
#
# Run from transcribe/. Use --dry-run first.

set -euo pipefail

DRY=1
case "${1:-}" in
  --dry-run) DRY=1 ;;
  --apply)   DRY=0 ;;
  ""|*) echo "usage: $0 --dry-run | --apply"; exit 2 ;;
esac

BT_ROOT="data/bt"
DERIVED_ROOT="data/derived"
# Where BT-bundled originals get backed up before being overwritten.
# Outside bt/ so the 2a9445b-era bt_filter cleanup pass can't eat them.
BACKUP_ROOT="data/_bt_originals"

[[ -d "$BT_ROOT"      ]] || { echo "ERROR: $BT_ROOT not found (run from transcribe/)"; exit 1; }
[[ -d "$DERIVED_ROOT" ]] || { echo "ERROR: $DERIVED_ROOT not found";                     exit 1; }

count_annotated=0
count_annotated_overwrite=0
count_zh=0
count_zh_overwrite=0
count_skip_no_wrapper=0
declare -A leftovers   # stem_dir → list of unmoved files

do_move() {
  local src="$1" dst="$2" label="$3"
  if [[ ! -f "$src" ]]; then
    return 1
  fi
  if [[ -e "$dst" ]]; then
    # bt/ already has an srt at the destination filename. That's the
    # unannotated BT-bundled original (we never moved those during the
    # forward migration — only `*.zh-tw.srt` and `*.srt` with ※). The
    # annotated.srt from derived/ is "BT srt + Claude ※ markers", strictly
    # a superset, so it's safe to overwrite. Back the original up to
    # data/_bt_originals/ — outside bt/ so the 2a9445b-era bt_filter
    # cleanup pass can't eat it.
    local rel_from_bt="${dst#$BT_ROOT/}"
    local backup="$BACKUP_ROOT/$rel_from_bt"
    if [[ $DRY -eq 1 ]]; then
      echo "DRY[$label-overwrite] $src"
      echo "    →  $dst"
      echo "    backup: $dst → $backup"
    else
      mkdir -p "$(dirname "$backup")"
      if [[ ! -e "$backup" ]]; then
        cp -p "$dst" "$backup"
        echo "BACKUP $dst → $backup"
      fi
      mv -f "$src" "$dst"
      echo "OVERWRITE[$label] $(basename "$src") → $(basename "$dst")"
    fi
    return 3   # distinguished from clean mv
  fi
  if [[ $DRY -eq 1 ]]; then
    echo "DRY[$label] $src"
    echo "    →  $dst"
  else
    mv "$src" "$dst"
    echo "MV[$label] $(basename "$src") → $(basename "$dst")"
  fi
  return 0
}

# Walk every derived/<wrapper>/<stem>/ directory (mindepth 2 = wrapper/stem,
# maxdepth 2 = don't descend deeper).
while IFS= read -r -d '' stem_dir; do
  rel="${stem_dir#$DERIVED_ROOT/}"
  wrapper="${rel%%/*}"
  stem="${rel#*/}"

  bt_wrapper="$BT_ROOT/$wrapper"
  if [[ ! -d "$bt_wrapper" ]]; then
    echo "WARN: bt/<wrapper> missing for $rel — skipping"
    count_skip_no_wrapper=$((count_skip_no_wrapper+1))
    continue
  fi

  # Move annotated.srt → bt/<stem>.srt
  rc=0
  do_move "$stem_dir/annotated.srt" "$bt_wrapper/$stem.srt" "annotated" || rc=$?
  case $rc in
    0) count_annotated=$((count_annotated+1)) ;;
    3) count_annotated_overwrite=$((count_annotated_overwrite+1)) ;;
  esac

  # Move zh.srt → bt/<stem>.zh-tw.srt
  rc=0
  do_move "$stem_dir/zh.srt" "$bt_wrapper/$stem.zh-tw.srt" "zh" || rc=$?
  case $rc in
    0) count_zh=$((count_zh+1)) ;;
    3) count_zh_overwrite=$((count_zh_overwrite+1)) ;;
  esac

  # Survey leftovers (anything still in this stem_dir besides the two srts
  # we just acted on).
  if [[ -d "$stem_dir" ]]; then
    leftover_list=""
    while IFS= read -r f; do
      base="$(basename "$f")"
      # Ignore the two srts we (would have) handled.
      [[ "$base" == "annotated.srt" || "$base" == "zh.srt" ]] && continue
      leftover_list+="$base "
    done < <(find "$stem_dir" -mindepth 1 -maxdepth 1 2>/dev/null)
    if [[ -n "$leftover_list" ]]; then
      leftovers["$rel"]="$leftover_list"
    fi
  fi
done < <(find "$DERIVED_ROOT" -mindepth 2 -maxdepth 2 -type d -print0)

echo
echo "── Summary ───────────────────────────────────────────────"
echo "  annotated.srt → bt/<stem>.srt (clean):              $count_annotated"
echo "  annotated.srt → bt/<stem>.srt (overwrite BT orig):  $count_annotated_overwrite"
echo "  zh.srt        → bt/<stem>.zh-tw.srt (clean):        $count_zh"
echo "  zh.srt        → bt/<stem>.zh-tw.srt (overwrite):    $count_zh_overwrite"
echo "  skipped (bt/<wrapper> missing):                     $count_skip_no_wrapper"
echo

if (( ${#leftovers[@]} > 0 )); then
  echo "── Leftover files in derived/ stem dirs (NOT moved) ──────"
  for rel in "${!leftovers[@]}"; do
    echo "  $rel/"
    for f in ${leftovers[$rel]}; do
      echo "    $f"
    done
  done
  echo
  echo "These are pre-compute HLS cache (master.m3u8 / seg_*.ts) and"
  echo ".error stamps. Safe to discard for the live-hls architecture."
  echo "Run separately:  sudo rm -rf data/derived"
  echo
fi

if [[ $DRY -eq 1 ]]; then
  echo "DRY RUN — nothing was moved or deleted."
  echo "Re-run as: $0 --apply"
fi
