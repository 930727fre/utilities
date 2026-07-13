"""Content-level subtitle verification against whisper (WER + LLM plot).

`verify_against_whisper(whisper_srt, candidate_srt, windows=None)` — WER
gate (default path). Whisper output is the ground-truth listening
reference; the candidate is a "literary upgrade" we accept only if its
full transcript text is close enough to whisper's that they're plausibly
the same audio. Timing is intentionally ignored for scoring — alass
handles alignment downstream once a candidate passes.

`find_pollution_windows(whisper_srt)` — detect whisper decoder
hallucination loops (long runs of consecutive identical short phrases).
Returns time ranges the caller passes back into `verify_against_whisper`
as `windows`, which scrubs those ranges from WHISPER (only) before
running WER — a polluted stretch doesn't tank an otherwise-matching
candidate. Scrub is single-sided (whisper only, not candidate) because
candidates from OS / bundled tiers haven't been alass-aligned yet at
this point, so their timeline may drift from whisper's — dropping
candidate cues by whisper's timeline would strip the wrong stretches.
Whisper-only scrub inflates WER slightly (candidate has content whisper
lost during pollution → those become "insertions"), but 5 min of
pollution in a 50 min episode only pushes WER up by ~0.1, well inside
the 0.5 pass margin.

`verify_by_plot(candidate_srt, show, season, episode)` — LLM fallback
gate for the edge case where pollution covers > 50% of runtime and WER
mathematically can't discriminate a good candidate from a bad one.
Sends the candidate's full dialogue (timestamps stripped) to Opus
(with web_search) and asks whether it matches the target episode's
known plot. TV episodes run ~10–15K input tokens, movies ~25–30K —
trivially within Opus's context and priced at pennies per check,
below sampling's savings threshold. Only invoked in the coverage-bail
branch of the bt pipeline; regular runs never see this. Opus is
deliberate — Haiku and Gemini flash-lite were both tested and neither
could discriminate specific episodes reliably. See caller in
`tasks.process_bt_file`.

The metadata (Haiku) prefilter this module used to carry was removed:
WER is authoritative — a candidate whose release-name / SxxExx metadata
looks fine but whose content is actually a different show / cut fails
WER anyway, and Haiku was empirically bypassed by uploaders spoofing
metadata. Cheaper cue-count prefilter catches the forced-subs case that
WER also catches but downloads-then-rejects. See `_MIN_REAL_CUES`.
"""
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
# climbs >0.7. 0.5 is the conventional pass cutoff and leaves safety
# margin in both directions.
#
# No cue-density pre-filter — WER alone catches every case density was
# meant to: forced subs land at WER ~1 (massive deletions vs whisper);
# bilingual / commentary-bundled subs land at WER >1 (massive insertions);
# different content lands at WER >0.7. The density gate we used to have
# false-rejected legitimate candidates when whisper produced anomalously
# few cues for an episode.
_WER_PASS_MAX = 0.5

# Forced-subs prefilter — subtitle tracks that translate only foreign
# signs / on-screen text typically ship 10–40 cues per episode (vs
# 300–800 for full dialogue). Rejecting these at cue-count level saves a
# WER computation whose result would always be ~1.0 (massive deletions).
# 100 is well below the low end of real-dialogue counts and well above
# the high end of forced tracks; a whole-episode subtitle with < 100
# cues is not full dialogue.
_MIN_REAL_CUES = 100

# Pollution detection — long runs of consecutive identical cue text
# signal a whisper decoder hallucination loop ("No.", "Thank you.").
# Threshold of 10 is well above plausible real-dialogue repetition
# (rapid affirmations peak at 3-5 consecutive cues; catchphrases rarely
# run consecutively past that). Hallucination runs are by definition
# consecutive and typically span dozens to hundreds of cues, so the
# threshold sits deep inside the safe zone.
_POLLUTION_RUN_THRESHOLD = 10


# Strip SRT formatting tags before normalization — `<i>...</i>`,
# `<b>`, `{\an8}` positioning markers, etc. — so the WER score reflects
# spoken-word content only.
_SRT_TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}")

# Drop everything that isn't a word character, whitespace, or apostrophe
# (apostrophes are kept so "don't" / "it's" don't get split mid-word).
_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)
_MULTI_WS_RE = re.compile(r"\s+")

# SRT timestamp: "HH:MM:SS,mmm --> HH:MM:SS,mmm"
_TS_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


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
    """Concatenate every cue's text, strip SRT formatting, lowercase,
    drop punctuation (except apostrophes), collapse whitespace. Result is
    a clean space-separated word stream ready to feed jiwer.wer."""
    raw = " ".join(_cue_text(c) for c in cues)
    s = _SRT_TAG_RE.sub("", raw)
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


def _filter_out_windows(
    cues: list[dict],
    windows: list[tuple[float, float]],
) -> list[dict]:
    if not windows:
        return cues
    return [c for c in cues if not _cue_in_windows(c, windows)]


# ── Pollution detection ───────────────────────────────────────────────────

def find_pollution_windows(whisper_srt: Path) -> list[tuple[float, float]]:
    """Return time ranges (start_s, end_s) where whisper's decoder was
    stuck in a hallucination loop (`_POLLUTION_RUN_THRESHOLD`+ consecutive
    identical short phrases). Empty list if the transcript is clean.

    Windows are used by `verify_against_whisper` to filter cues from
    whisper (single-sided scrub) before scoring — a polluted stretch
    would otherwise blow up WER against an honest candidate that has
    the real dialogue for that time range. Caller should first check
    that pollution is a small fraction of the transcript (see
    `pollution_cue_ratio`); when it's not, no salvage is meaningful
    and the pipeline should stamp `.whisper-polluted` directly.

    Runs that span an empty-body cue are broken (safer — don't merge two
    unrelated runs across an accidental gap)."""
    cues = _real_cues(whisper_srt)
    if len(cues) < _POLLUTION_RUN_THRESHOLD:
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

    for i, c in enumerate(cues):
        flat = _cue_text(c).strip().lower()
        if not flat:
            _emit_run(i - 1)
            cur_run = 1
            run_start_idx = i + 1
            prev_text = None
            continue
        if flat == prev_text:
            cur_run += 1
        else:
            _emit_run(i - 1)
            cur_run = 1
            run_start_idx = i
        prev_text = flat
    _emit_run(len(cues) - 1)

    return windows


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

def verify_against_whisper(
    whisper_srt: Path,
    candidate_srt: Path,
    windows: Optional[list[tuple[float, float]]] = None,
) -> tuple[bool, str]:
    """WER-based content gate. Returns (pass, reason).

    Concat all cue text from each side, strip SRT formatting +
    punctuation, lowercase. Compute word error rate between whisper
    (reference) and candidate (hypothesis); pass if WER ≤ 0.5.

    Prefilter — reject candidates with fewer than `_MIN_REAL_CUES`
    (typically forced-subs tracks) without a WER computation.

    `windows` (optional) — time ranges to scrub from WHISPER (only)
    before scoring. Used when whisper has hallucination loops; see
    `find_pollution_windows`. Candidate is NOT scrubbed — its timeline
    may drift from whisper's (alass hasn't run yet at this point) so
    time-window scrub on candidate would strip the wrong stretches.
    Caller is responsible for bailing before calling here if pollution
    covers too much of the runtime (single-side scrub can't rescue a
    mostly-hallucinated whisper).

    Timing is deliberately not considered for scoring — alass handles
    alignment after this gate passes."""
    w_cues = _real_cues(whisper_srt)
    c_cues = _real_cues(candidate_srt)

    if not w_cues:
        return False, "whisper SRT empty or unparseable"
    if not c_cues:
        return False, "candidate SRT empty or unparseable"
    if len(c_cues) < _MIN_REAL_CUES:
        return False, (
            f"only {len(c_cues)} real cues — below {_MIN_REAL_CUES} min "
            f"(likely forced-subs or partial)"
        )

    if windows:
        w_cues = _filter_out_windows(w_cues, windows)

    w_text = _normalized_full_text(w_cues)
    c_text = _normalized_full_text(c_cues)
    if not w_text or not c_text:
        return False, "normalized transcript empty after cleanup"

    try:
        wer = jiwer.wer(w_text, c_text)
    except ValueError as e:
        return False, f"WER computation failed: {e}"

    scrub_note = f" (whisper scrubbed by {len(windows)} window(s))" if windows else ""
    if wer <= _WER_PASS_MAX:
        return True, f"WER {wer:.2f} ≤ {_WER_PASS_MAX}{scrub_note}"
    return False, (
        f"WER {wer:.2f} > {_WER_PASS_MAX}{scrub_note} — likely different "
        f"content / wrong cut / wrong language"
    )


# ── LLM plot-check fallback (>50% pollution edge case) ────────────────────

# Opus 4.7 with web search. Haiku and Gemini flash-lite were both
# empirically inadequate for episode-level discrimination (Haiku
# hallucinated matches on wrong episodes of the same show; Gemini
# guessed "yes" whenever the show was recognizable). Opus recalls
# specific plot beats reliably; web search fills gaps for episodes
# outside its training coverage. Per-check cost is a handful of cents
# (TV episode ~10-15K input tokens at $5/M + web_search $0.01/query,
# max 3 queries), and the trigger (>50% whisper pollution) is rare
# enough that this doesn't materially move total pipeline cost.
_PLOT_MODEL = "claude-opus-4-7"
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


def verify_by_plot(
    candidate_srt: Path,
    show: str,
    season: Optional[int],
    episode: Optional[int],
) -> tuple[bool, str]:
    """LLM plot-check against `show` (+ optional season/episode).
    Returns (accept, reason). Used only in the polluted-whisper >50%
    coverage fallback where WER can't discriminate.

    Sends the candidate's full dialogue (timestamps stripped) so Opus
    doesn't have to guess based on partial sampling — timestamps
    convey no plot information for content-match and their bulk is
    real tokens we'd rather not pay for.

    See module docstring for why Opus + web_search specifically."""
    cues = _real_cues(candidate_srt)
    if len(cues) < _MIN_REAL_CUES:
        return False, (
            f"only {len(cues)} real cues — below {_MIN_REAL_CUES} min "
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
