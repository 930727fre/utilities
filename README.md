# utility

A collection of self-hosted tools, each containerized with Docker.

| Tool | Description |
|------|-------------|
| [clock](./clock) | Workout interval timer (1 min work / 30 sec rest) |
| [marker-pipeline](./marker-pipeline) | Upload PDF or EPUB, get a zip of clean markdown + extracted images + metadata |
| [transcribe](./transcribe) | YouTube downloader and MP3 inbox processor with GPU-accelerated Whisper transcription |
| [flashcard](./flashcard) | FSRS-based flashcard app with spaced repetition |
| [keyboard](./keyboard) | Push-to-talk voice input PWA — Whisper transcription + LLM cleanup |
| [ollama](./ollama) | Local LLM runtime |
| [cloudflared](./cloudflared) | Cloudflare Tunnel — exposes all services via subdomains |
| [backup](./backup) | Daily backup of flashcard data to Cloudflare R2 at 04:00 |

## Notes

1. Make sure to `.gitignore` `data/` and put all persistent files under it — services must create all required subdirectories programmatically on startup (no manual `mkdir` needed after `git clone && docker compose up`)
2. Remember to register a subdomain in the Cloudflare tunnel dashboard for each new service
3. Always prefix service names and container names with the service name (e.g. `flashcard-backend`, `flashcard-frontend`). Service names act as DNS hostnames on shared networks — generic names like `frontend` or `backend` will collide across services on `my_network`. Container names should match for clarity in `docker ps`.
4. (Optional) To prevent iOS Safari from auto-zooming on input/textarea focus, add `maximum-scale=1` to the viewport meta tag: `<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">`
5. **iOS viewport lock — fix it once at html/body, not per-component.** Safari's address bar collapses/expands as you scroll, which makes `100vh` taller than the actually-visible area. Stacking `100svh`, `calc()` heights, or per-page wrappers to compensate amplifies the bug. The pattern that just works:
   ```css
   html, body, #root { height: 100%; }
   body {
     margin: 0;
     overflow: hidden;
     overscroll-behavior: none;
   }
   ```
   Then page roots fill with `height: 100%` and internal scrolling lives on whichever specific container should scroll (`overflow-y: auto`). **Do not put any viewport unit (`vh`, `svh`, `dvh`) anywhere in the height chain** — the parent already represents "the visible viewport" thanks to the html/body lock. Used in `keyboard/frontend/style.css` and `flashcard/src/index.css`.

## Design language

All user-facing tools (marker-pipeline, transcribe, flashcard) share a single visual language. When adding a new tool, follow this so they look like siblings.

### Palette

Dark surfaces with warm cream text, one honey accent for the single primary action per screen. Everything else is grayscale.

| Token | Hex | Use |
|-------|-----|-----|
| page bg | `#1c1c1e` | body background |
| card | `#2c2c2e` | card / row backgrounds |
| raised | `#3a3a3c` | borders, secondary button bg, snackbar |
| text primary | `#e8e3d9` | titles, body text |
| text secondary | `#aeaeb2` | author, captions, status |
| text tertiary | `#636366` | placeholders, delete icon, disabled |
| accent (honey) | `#c79968` | the one primary action per screen |

Honey is rare on purpose — its appearance signals "do this." If two things on screen are honey, neither stands out.

### Typography

- Body: `-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`
- Title / status / monospace: `ui-monospace, SFMono-Regular, Menlo, monospace`
- Page titles use monospace, body text uses sans

### Status & action glyphs

State indicators are characters, not colored pills. They occupy the same slot per row, never both showing at once:

| Glyph | State | Behavior |
|-------|-------|----------|
| `○` | working | CSS opacity pulse 0.35 ↔ 1.0, 1.4 s ease-in-out infinite |
| `!` | failed | static, gray, `title` tooltip explains why |
| `↓` | ready + downloadable | honey, clickable; replaces the status glyph entirely |
| `✕` | delete | gray `#636366`, hover lifts to `#e8e3d9`; never the primary action |
| `↻` | retry | honey when actionable |
| `▸` | play | honey |
| `+` | add | honey, used as the page-level CTA |

If an action button is present (e.g. `↓` Download), it *replaces* the status indicator rather than appearing alongside it — the button's existence is itself the "ready" signal.

### Button content rules

| Pattern | Use |
|---------|-----|
| char only (`+`, `▸`, `↻`, `↓`) | when the glyph alone carries the meaning |
| text only (`Submit`, `Update`, `Import N Cards`) | when there's no obvious glyph |
| text only (`MP4`, `MP3`, `SRT`) | when the *label* itself is the disambiguator (e.g. file formats) |
| char + text (`SHOW ANSWER (Space)`) | only when both add info — char for symbol, text for keyboard hint |

Don't pair a char with a redundant description. `↓ Download` is just `↓`. `+ Add File` is just `+`. Always include a `title="…"` tooltip on bare-char buttons.

### Shape

| Property | Value |
|----------|-------|
| Card radius | `12px` (rounded but not pill) |
| Button radius | `8px` |
| Card shadow | `0 1px 4px rgba(0,0,0,0.3)` (subtle, not glow-y) |
| Page width | `max-width: 720px`, centered |
| Inline padding | `16px` mobile, `24px` desktop |

### When to deviate

- **Functional color**: if color is encoding information the user *needs* (not just decoration), keep it. We discussed this for flashcard's review buttons (Again/Hard/Good/Easy traditionally color-coded) and decided to drop them in favor of position-based muscle memory — but that was a deliberate UX call, not a default. If your tool legitimately needs more than one accent (e.g. a status dashboard surfacing severity), don't twist yourself in knots staying monochrome.
- **Brand contexts**: when embedding inside something with its own identity (e.g. iframed into another tool), respect the host.
- **Errors that need to scream**: a quiet `!` glyph is fine for "this conversion failed, retry it." A loud red banner is appropriate for "your data is being deleted, are you sure?" The bar for breaking the gray-only rule is "does the user *need* to be alarmed."
