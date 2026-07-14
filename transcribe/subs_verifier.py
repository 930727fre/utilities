"""Content-level subtitle verification against whisper (WER + LLM plot).

`verify_against_whisper(whisper_srt, candidate_srt)` — WER gate (default
path). Whisper output is the ground-truth listening reference; the
candidate is a "literary upgrade" we accept only if its full transcript
text is close enough to whisper's that they're plausibly the same
audio. Timing is intentionally ignored for scoring — alass handles
alignment downstream once a candidate passes.

`find_pollution_windows(whisper_srt)` + `pollution_cue_ratio(...)` —
detect whisper decoder hallucination loops (long runs of consecutive
identical short phrases). Used as a ROUTING signal by the caller: if
`pollution_cue_ratio` exceeds the polluted-fallback threshold the
pipeline switches to `verify_by_plot`. WER itself never scrubs the
polluted stretches from whisper; empirically single-side scrub either
does nothing (pollution windows already overlap silence, candidate
matches independently) or makes things worse (scrub removes real
dialogue whisper mis-transcribed → those words become insertions
against the candidate → WER inflates). See git log for the S2 GoT
dataset that motivated the removal.

`verify_by_plot(candidate_srt, show, season, episode)` — LLM fallback
gate for the edge case where pollution covers > 50% of runtime and WER
mathematically can't discriminate a good candidate from a bad one.
Sends the candidate's full dialogue (timestamps stripped) to Sonnet
(with web_search) and asks whether it matches the target episode's
known plot. TV episodes run ~10–15K input tokens, movies ~25–30K —
trivially within context and priced at pennies per check, below
sampling's savings threshold. Only invoked in the coverage-bail branch
of the bt pipeline; regular runs never see this. Model tier: Haiku
and Gemini flash-lite were both tested and neither could discriminate
specific episodes reliably; Sonnet is the smallest tier known to hold
up. See caller in `tasks.process_bt_file`.

The metadata (Haiku) prefilter this module used to carry was removed:
WER is authoritative — a candidate whose release-name / SxxExx metadata
looks fine but whose content is actually a different show / cut fails
WER anyway, and Haiku was empirically bypassed by uploaders spoofing
metadata. Cheaper cue-count prefilter catches the forced-subs case that
WER also catches but downloads-then-rejects. See `MIN_REAL_CUES`.
"""
import os
import re
from pathlib import Path
from typing import Optional

import jiwer

from annotate import parse_srt
from claude_client import generate_json

# ── WER threshold ─────────────────────────────────────────────────────────
#
# Calibration from the whisper / ASR-eval literature: clean human
# transcript vs whisper-large output on the same audio usually scores
# 0.1–0.3. With the extra noise of release-mismatch (CC vs full
# dialogue, occasional missing lines, etc.) realistic same-content WER
# lands around 0.2–0.5. Different content / wrong cut / wrong language
# climbs >0.7. Empirically wrong-episode subs from a season pack score
# ≥1.5 — the 0.5–1.5 band is essentially empty in real data, so a
# threshold up to ~0.6 is still safe against wrong-episode candidates.
#
# Raised to 0.55 (from 0.5) after the S04E09 GoT case: whisper only
# transcribed ~70% of dialogue for that episode, so every correct-
# episode candidate (both bundled and OS variants) clustered tightly
# at 0.502–0.519, just above 0.5 — all rejected while wrong-episode
# subs stayed at 1.7–2.4. Extra 0.05 catches this false-negative band
# without moving into anything dangerous. See git log for the analysis.
#
# No cue-density pre-filter — WER alone catches every case density was
# meant to: forced subs land at WER ~1 (massive deletions vs whisper);
# bilingual / commentary-bundled subs land at WER >1 (massive insertions);
# different content lands at WER >0.7. The density gate we used to have
# false-rejected legitimate candidates when whisper produced anomalously
# few cues for an episode.
WER_PASS_MAX = 0.55

# Forced-subs prefilter — subtitle tracks that translate only foreign
# signs / on-screen text typically ship 10–40 cues per episode (vs
# 300–800 for full dialogue). Rejecting these at cue-count level saves a
# WER computation whose result would always be ~1.0 (massive deletions).
# 100 is well below the low end of real-dialogue counts and well above
# the high end of forced tracks; a whole-episode subtitle with < 100
# cues is not full dialogue.
MIN_REAL_CUES = 100

# Pollution detection — two independent signals:
#
# 1. Consecutive-identical run: N+ neighbouring cues with byte-identical
#    normalized text. Catches the classic "No.", "Thank you." decoder
#    loops where whisper produces the exact same short cue over and
#    over. Threshold 10 sits well above plausible real-dialogue
#    repetition (rapid affirmations peak at 3-5 consecutive cues;
#    catchphrases rarely run past that).
#
# 2. Per-cue degeneracy: a single cue whose word content is a single
#    token repeated many times ("ha ha ha ha ha..."). Catches the case
#    where whisper's decoder is stuck on one token but chops the run
#    into cues of different lengths, so consecutive-equality never
#    fires. South Park S01E02 broke signal 1 that way — 12 cues each
#    holding 30-230 "ha" tokens, none of them bytewise equal because
#    the counts differ.
_POLLUTION_RUN_THRESHOLD = 10

# Cue is "degenerate" (self-evident pollution) if it's long enough to
# be past incidental repetition AND ≤10% of its tokens are unique.
# A 20-token real dialogue cue has ~15-20 unique tokens (function
# words repeat, content words don't). A 20-token "ha ha ha..." cue
# has 1 unique token → ratio 0.05, well under 0.1.
_DEGEN_MIN_TOKENS = 20
_DEGEN_UNIQUE_RATIO = 0.1


# Strip SRT formatting tags before normalization — `<i>...</i>`,
# `<b>`, `{\an8}` positioning markers, etc. — so the WER score reflects
# spoken-word content only.
_SRT_TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}")

# SDH (hearing-impaired) annotation strip: sound-effect brackets and
# speaker labels. Whisper never produces these, so leaving them in
# makes each SDH token count as an insertion against the reference
# and inflates candidate WER by ~0.03-0.05 on SDH-heavy releases
# (S02 GoT SDH: 63-92 annotations per episode). Applied to both sides
# for symmetry — whisper side is a no-op, candidate side gets cleaned.
#
# Bracketed cues: (URINATING), [door slams], ♪music♪. Regex tolerates
# nested content but not nested brackets (real SDH doesn't nest).
_SDH_TAG_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]|♪[^♪]*♪")
# Speaker labels: MAN:, MAN 2:, LORD VARYS:, etc. All caps + optional
# digits, colon followed by whitespace or end. Requires ≥2 uppercase
# to avoid stray "I:" / "A:" matches; applied BEFORE lowercase step so
# the caps constraint has signal.
_SDH_SPEAKER_RE = re.compile(r"\b[A-Z]{2,}(?:[A-Z0-9 ]{0,20})?:(?=\s|$)")

# Drop everything that isn't a word character, whitespace, or apostrophe
# (apostrophes are kept so "don't" / "it's" don't get split mid-word).
_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)
_MULTI_WS_RE = re.compile(r"\s+")

# SRT timestamp: "HH:MM:SS,mmm --> HH:MM:SS,mmm"
_TS_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def cue_count_ok(sub: Path, rel: Path, video: Path, *, tag: str) -> bool:
    """Cheap cue-count prefilter — reject candidates with too few cues
    to plausibly be a full-dialogue track (forced-subs / partial /
    broken extraction / OCR-bomb). Uses `text.count("-->")` instead of
    parsing cues; that's enough for a lower-bound sanity check and
    avoids the parse cost.

    Called before any expensive gate — WER, plot-check, or the
    trust-tier "just copy it" branch.

    `rel` is only for logging (the sub's relative path within its
    container / wrapper); `tag` becomes the log prefix
    (`[embedded]`, `[bundled]`, `[pgs-ocr]`, etc.)."""
    try:
        text = sub.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    cues = text.count("-->")
    if cues < MIN_REAL_CUES:
        print(f"[{tag}] {video.name!r}: SKIP {rel} — only {cues} cues, "
              f"below {MIN_REAL_CUES} min (likely forced-subs / partial)",
              flush=True)
        return False
    return True


def _cue_text(cue: dict) -> str:
    """Flatten a cue's lines into a single string."""
    return " ".join(ln.strip() for ln in cue.get("lines", []) if ln.strip())


def _cue_times(cue: dict) -> Optional[tuple[float, float]]:
    """Parse a cue's `time` line into (start_s, end_s). None if malformed."""
    m = _TS_RE.search(cue.get("time", ""))
    if not m:
        return None
    h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
    start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
    end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
    return start, end


def _real_cues(srt_path: Path) -> list[dict]:
    """Parse SRT and drop our ※ sentinel cues (idx >= 99000 by convention).
    Verification compares dialogue, not status overlay."""
    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    cues = parse_srt(text)
    return [c for c in cues if c.get("idx", 0) < 99000]


def _normalized_full_text(cues: list[dict]) -> str:
    """Concatenate every cue's text, strip SRT formatting + SDH
    annotations, lowercase, drop punctuation (except apostrophes),
    collapse whitespace. Result is a clean space-separated word stream
    ready to feed jiwer.wer."""
    raw = " ".join(_cue_text(c) for c in cues)
    s = _SRT_TAG_RE.sub("", raw)
    s = _SDH_TAG_RE.sub(" ", s)
    s = _SDH_SPEAKER_RE.sub(" ", s)
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _MULTI_WS_RE.sub(" ", s)
    return s.strip()


def _cue_in_windows(cue: dict, windows: list[tuple[float, float]]) -> bool:
    """True if this cue's time span overlaps any window (start/end
    inclusive). Cues without parseable timing are treated as "in" — safer
    to drop than to keep unaligned noise in the WER pool."""
    times = _cue_times(cue)
    if times is None:
        return True
    c_start, c_end = times
    for w_start, w_end in windows:
        if c_end >= w_start and c_start <= w_end:
            return True
    return False




# ── Pollution detection ───────────────────────────────────────────────────

def _is_degenerate_cue(flat_text: str) -> bool:
    """A single cue is 'degenerate' when its word content collapses to
    a handful of unique tokens repeated many times — the classic per-
    cue whisper hallucination signature (`ha ha ha × 200`). Independent
    of neighbouring cues, so it catches runs that consecutive-identical
    detection misses because the repetition count varies between cues.

    Short cues are exempt (real dialogue can legitimately be a short
    run of one token: `"No no no."`, `"Yes."`); the degeneracy call
    only fires once the cue is long enough to be past coincidence.
    """
    tokens = flat_text.split()
    if len(tokens) < _DEGEN_MIN_TOKENS:
        return False
    unique = len(set(tokens))
    return unique / len(tokens) < _DEGEN_UNIQUE_RATIO


def find_pollution_windows(whisper_srt: Path) -> list[tuple[float, float]]:
    """Return time ranges (start_s, end_s) where whisper's decoder was
    stuck in a hallucination loop. Empty list if the transcript is clean.

    Two independent pollution signals per cue:
    - **Consecutive-identical run**: cue text bytewise equals previous
      cue → running counter. A run of `_POLLUTION_RUN_THRESHOLD`+ cues
      becomes a window.
    - **Per-cue degeneracy**: cue's tokens are ≥90% duplicates. Each
      such cue is a single-cue window on its own, regardless of what
      neighbours look like.

    Windows are consumed by `pollution_cue_ratio` for the routing
    decision: high ratio → pipeline switches from WER to plot-check
    fallback; low ratio → WER is trusted as-is (no scrub — see module
    docstring). Windows also feed the whisper-polluted sidecar message
    for user diagnostics.

    Runs that span an empty-body cue are broken (safer — don't merge two
    unrelated runs across an accidental gap)."""
    cues = _real_cues(whisper_srt)
    if not cues:
        return []

    windows: list[tuple[float, float]] = []
    run_start_idx = 0
    cur_run = 1
    prev_text: str | None = None

    def _emit_run(end_idx: int) -> None:
        if cur_run < _POLLUTION_RUN_THRESHOLD:
            return
        start_times = _cue_times(cues[run_start_idx])
        end_times = _cue_times(cues[end_idx])
        if start_times is None or end_times is None:
            return
        windows.append((start_times[0], end_times[1]))

    def _emit_degen(idx: int) -> None:
        times = _cue_times(cues[idx])
        if times is not None:
            windows.append(times)

    for i, c in enumerate(cues):
        flat = _cue_text(c).strip().lower()
        if not flat:
            _emit_run(i - 1)
            cur_run = 1
            run_start_idx = i + 1
            prev_text = None
            continue
        # Per-cue degeneracy — self-contained pollution signal. Doesn't
        # interact with the run counter; a degenerate cue in the middle
        # of otherwise-varied text still gets its own window.
        if _is_degenerate_cue(flat):
            _emit_degen(i)
        if flat == prev_text:
            cur_run += 1
        else:
            _emit_run(i - 1)
            cur_run = 1
            run_start_idx = i
        prev_text = flat
    _emit_run(len(cues) - 1)

    return windows


def write_srt_without_windows(
    src_srt: Path,
    windows: list[tuple[float, float]],
    dest_srt: Path,
) -> int:
    """Read `src_srt`, drop every cue overlapping any window, renumber the
    survivors 1..N, write to `dest_srt`. Returns count of surviving cues.

    Used when a low-pollution whisper transcript is being promoted to
    verified.srt as a no-candidate-salvage fallback — dropping the
    hallucinated cues keeps them from appearing in the annotated
    canonical output the user sees in Jellyfin."""
    from annotate import render_srt
    cues = _real_cues(src_srt)
    kept = [c for c in cues if not _cue_in_windows(c, windows)]
    for new_idx, c in enumerate(kept, 1):
        c["idx"] = new_idx
    dest_srt.parent.mkdir(parents=True, exist_ok=True)
    dest_srt.write_text(render_srt(kept), encoding="utf-8")
    return len(kept)


def pollution_cue_ratio(
    whisper_srt: Path,
    windows: list[tuple[float, float]],
) -> float:
    """Fraction of whisper's real cues that fall inside `windows` — i.e.
    the share that single-side scrub would drop from the WER reference.

    Cue-based rather than time-based: a movie can have 25 min of
    hallucinated "Hey." pollution in what was silent scenes and still
    look mild if measured against the 2 h runtime (~20% coverage), but
    if the whisper transcript is 80% "Hey." cues then the surviving
    reference after scrub is tiny and WER against a full-length
    candidate becomes noise-dominated. Cue ratio catches that
    directly.

    Returns 0.0 on empty windows or unparseable SRT (safe default —
    caller treats "no pollution" as "run WER normally")."""
    if not windows:
        return 0.0
    cues = _real_cues(whisper_srt)
    if not cues:
        return 0.0
    polluted = sum(1 for c in cues if _cue_in_windows(c, windows))
    return polluted / len(cues)


# ── WER scoring ───────────────────────────────────────────────────────────

def wer_score(
    whisper_srt: Path,
    candidate_srt: Path,
) -> tuple[Optional[float], str]:
    """Compute WER between whisper (reference) and candidate (hypothesis).
    Returns (score, reason). Score is None when a computation isn't
    possible (empty transcripts, forced-subs prefilter, malformed SRT,
    jiwer error); the reason string explains why in every case for
    logging.

    Concat all cue text from each side, strip SRT formatting +
    punctuation, lowercase. Cue-count prefilter (`MIN_REAL_CUES`)
    rejects forced-subs / partial tracks before computation.

    Timing is deliberately not considered — alass handles alignment
    downstream once a candidate is picked. This is a content-match
    scorer; the caller decides how to interpret the number (threshold
    gate, min-of-N pick, etc.).

    No pollution-window scrub: single-side scrub only inflates the
    score when whisper's hallucination happened during real dialogue
    (whisper lost the true words → candidate has them → +insertions).
    Callers route to `verify_by_plot` via `pollution_cue_ratio` for
    the high-pollution case instead."""
    w_cues = _real_cues(whisper_srt)
    c_cues = _real_cues(candidate_srt)

    if not w_cues:
        return None, "whisper SRT empty or unparseable"
    if not c_cues:
        return None, "candidate SRT empty or unparseable"
    if len(c_cues) < MIN_REAL_CUES:
        return None, (
            f"only {len(c_cues)} real cues — below {MIN_REAL_CUES} min "
            f"(likely forced-subs or partial)"
        )

    w_text = _normalized_full_text(w_cues)
    c_text = _normalized_full_text(c_cues)
    if not w_text or not c_text:
        return None, "normalized transcript empty after cleanup"

    try:
        wer = jiwer.wer(w_text, c_text)
    except ValueError as e:
        return None, f"WER computation failed: {e}"

    return wer, f"WER {wer:.2f}"


def verify_against_whisper(
    whisper_srt: Path,
    candidate_srt: Path,
) -> tuple[bool, str]:
    """Threshold-gate wrapper around `wer_score`. Returns (pass, reason).
    Passes if WER ≤ `WER_PASS_MAX` (0.5)."""
    score, reason = wer_score(whisper_srt, candidate_srt)
    if score is None:
        return False, reason
    if score <= WER_PASS_MAX:
        return True, f"{reason} ≤ {WER_PASS_MAX}"
    return False, (
        f"{reason} > {WER_PASS_MAX} — likely different "
        f"content / wrong cut / wrong language"
    )


# ── LLM plot-check fallback (>50% pollution edge case) ────────────────────

# Sonnet 4.6 with web search. Haiku and Gemini flash-lite were both
# empirically inadequate for episode-level discrimination (Haiku
# hallucinated matches on wrong episodes of the same show; Gemini
# guessed "yes" whenever the show was recognizable). Sonnet is the
# smallest tier that hasn't been shown to fail on this task — Opus was
# used previously but the middle tier was never actually tested, and
# Sonnet's long-context + factual recall are strong enough that Opus
# was likely over-provisioning. Web search fills gaps for episodes
# outside training coverage regardless of tier. If Sonnet turns out
# to false-positive on wrong-episode discrimination, revert to
# `claude-opus-4-7` and record the failure here.
_PLOT_MODEL = "claude-sonnet-4-6"
_PLOT_WEB_SEARCH_MAX_USES = 3

_PLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["match", "reason"],
}

_PLOT_PROMPT_TEMPLATE = """\
You are verifying that a subtitle file (SRT) actually contains the \
dialogue of a specific TV episode or film. This is a fallback quality \
gate — the normal automatic verifier (word-error-rate against a \
whisper ASR transcript) is unavailable for this run because whisper's \
decoder hallucinated over more than half of the audio, so we can't \
use it as a reference.

Target:
  Show / film: {target}
  {episode_line}

Full dialogue from the candidate subtitle. Timestamps are stripped; \
each line is one cue, in order of appearance:

{dialogue}

Task: decide whether this dialogue is plausibly the actual dialogue of \
the target above. Base your decision ONLY on the dialogue content — do \
NOT look at filenames, release names, or any other metadata (uploader \
metadata has been observed to be forged in this domain, which is why \
we're falling back to you).

If you're not certain about the target episode's specific plot / scenes \
/ dialogue beats, USE WEB SEARCH — search for a synopsis / recap / \
transcript summary (e.g., "{target} episode plot", "{target} recap", \
"{target} script"). It's much better to search than to guess; \
false-positives here contaminate the user's library with the wrong \
subtitles.

Reject cases (return match=false):
- Wrong show entirely (dialogue is clearly from a different series / \
film — even if genre-adjacent)
- Right show but wrong episode / cut (e.g., S01E03 dialogue when the \
target is S01E05; theatrical vs director's cut differences big enough \
to notice)
- Forced-subs or partial track (only occasional lines translating \
non-English signage, not full dialogue)
- Dialogue too generic to identify confidently — err on the side of \
rejecting; the user can drop a manual SRT

Accept case (return match=true):
- The dialogue matches known scenes / beats / lines from the target \
episode with high confidence

Respond via the `respond` tool with:
- `match`: boolean (true = same content as target, false = reject)
- `reason`: string — cite the specific lines, scenes, or plot beats \
that decided it (or the specific mismatch that rejected it). If you \
used web_search, note what source you cross-referenced against.
"""


# ── LLM smart-pick for bundled candidates (polluted-mode narrower) ──────

# Haiku suffices for this — the decision is filename convention, not
# content analysis. Iterating plot-check on a full season pack's worth
# of subtitles would cost dollars per episode; a single Haiku pick +
# one plot-check bounds it to ~$0.10 per bundled attempt.
_PICK_MODEL = os.environ.get("ANTHROPIC_BUNDLED_PICK_MODEL",
                             "claude-haiku-4-5-20251001")

_PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "pick": {
            "type": "string",
            "description": "Exact relative path from the candidate list, "
                           "or empty string if no candidate plausibly matches.",
        },
        "reason": {"type": "string"},
    },
    "required": ["pick", "reason"],
}

_PICK_PROMPT_TEMPLATE = """\
You are picking the subtitle file that corresponds to a specific video, \
from a list of subtitles bundled in a torrent wrapper.

Wrapper name: {wrapper_name}

Target video (relative path within the wrapper):
  {video_rel}

Candidate subtitle files (relative paths within the wrapper):
{candidate_list}

Task: return the single subtitle whose filename / path most plausibly \
corresponds to the target video. Base your judgment on filename \
convention:
- Exact stem match (video.stem == subtitle.stem)
- SxxExx match (same season / episode code)
- Language label (English variants usually appear as `.eng`, `.en`, \
`.english`, or no language tag at all; foreign languages tend to be \
explicit — `.fre`, `.spa`, `.chi`, etc.)
- Folder layout (e.g., `Subs/S01E05/2_English.srt` — the S01E05 folder \
alone gives you enough)

If nothing plausibly matches (e.g., all subtitles are for a different \
episode of the pack), return an empty pick.

Respond via the `respond` tool with:
- `pick`: the exact relative path from the candidate list, or empty string
- `reason`: one line explaining the choice
"""


def smart_pick_bundled(
    wrapper_name: str,
    video_rel: Path,
    candidates_rel: list[Path],
) -> Optional[Path]:
    """LLM-driven pick from bundled subtitle candidates. Used in the
    polluted-whisper fallback where iterating plot-check on every
    wrapper subtitle would be expensive. Returns the picked relative
    path (from `candidates_rel`) or None if nothing matched / API
    failed.

    Callers pass paths relative to the wrapper root — the LLM sees the
    same relative form and returns it verbatim. Validated on the way
    out (LLM response must exactly match one of the provided paths).
    """
    if not candidates_rel:
        return None
    if len(candidates_rel) == 1:
        return candidates_rel[0]

    listing = "\n".join(f"  {p}" for p in candidates_rel)
    prompt = _PICK_PROMPT_TEMPLATE.format(
        wrapper_name=wrapper_name,
        video_rel=str(video_rel),
        candidate_list=listing,
    )
    try:
        result = generate_json(
            prompt,
            _PICK_SCHEMA,
            model=_PICK_MODEL,
            temperature=0.0,
            timeout=(10, 60),
        )
    except Exception as e:
        print(f"[bundled smart-pick] API error: {e}", flush=True)
        return None

    pick_str = (result.get("pick") or "").strip()
    if not pick_str:
        reason = (result.get("reason") or "").strip() or "(no reason)"
        print(f"[bundled smart-pick] LLM declined: {reason}", flush=True)
        return None

    for cand in candidates_rel:
        if str(cand) == pick_str:
            reason = (result.get("reason") or "").strip() or "(no reason)"
            print(f"[bundled smart-pick] picked {pick_str} — {reason}", flush=True)
            return cand
    print(f"[bundled smart-pick] LLM returned {pick_str!r} not in candidate "
          f"list — treating as no match", flush=True)
    return None


def verify_by_plot(
    candidate_srt: Path,
    show: str,
    season: Optional[int],
    episode: Optional[int],
) -> tuple[bool, str]:
    """LLM plot-check against `show` (+ optional season/episode).
    Returns (accept, reason). Used only in the polluted-whisper >50%
    coverage fallback where WER can't discriminate.

    Sends the candidate's full dialogue (timestamps stripped) so the
    model doesn't have to guess based on partial sampling — timestamps
    convey no plot information for content-match and their bulk is
    real tokens we'd rather not pay for.

    See module docstring for why Sonnet + web_search specifically."""
    cues = _real_cues(candidate_srt)
    if len(cues) < MIN_REAL_CUES:
        return False, (
            f"only {len(cues)} real cues — below {MIN_REAL_CUES} min "
            f"(likely forced-subs or partial), skipped plot-check"
        )

    dialogue = "\n".join(t for t in (_cue_text(c) for c in cues) if t)

    if season is not None and episode is not None:
        target = show
        episode_line = f"Season / Episode: S{season:02d}E{episode:02d}"
    else:
        target = show
        episode_line = "(movie or single-work — no season/episode)"

    prompt = _PLOT_PROMPT_TEMPLATE.format(
        target=target,
        episode_line=episode_line,
        dialogue=dialogue,
    )

    try:
        result = generate_json(
            prompt,
            _PLOT_SCHEMA,
            model=_PLOT_MODEL,
            temperature=0.0,
            web_search=True,
            web_search_max_uses=_PLOT_WEB_SEARCH_MAX_USES,
            timeout=(10, 180),
        )
    except Exception as e:
        return False, f"plot-check API error: {e}"

    match = bool(result.get("match"))
    reason = (result.get("reason") or "").strip() or "(no reason given)"
    prefix = "plot-check ACCEPT" if match else "plot-check REJECT"
    return match, f"{prefix}: {reason}"
