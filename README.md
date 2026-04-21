# utility

A collection of self-hosted tools, each containerized with Docker.

| Tool | Description |
|------|-------------|
| [clock](./clock) | Workout interval timer (1 min work / 30 sec rest) |
| [TwelveReader](./TwelveReader) | EPUB audiobook reader with on-demand TTS and synchronized highlighting |
| [yt-whisper](./yt-whisper) | YouTube video downloader with GPU-accelerated Whisper transcription |
| [flashcard](./flashcard) | FSRS-based flashcard app with spaced repetition |
| [cloudflared](./cloudflared) | Cloudflare Tunnel — exposes all services via subdomains |
| [backup](./backup) | Daily backup of flashcard and TwelveReader data to Cloudflare R2 at 04:00 |

## Notes

1. Make sure to `.gitignore` `data/` and put all persistent files under it — services must create all required subdirectories programmatically on startup (no manual `mkdir` needed after `git clone && docker compose up`)
2. Remember to register a subdomain in the Cloudflare tunnel dashboard for each new service
3. If a frontend container simply uses `frontend` as the service name it may collide on `my_network` — use `servicename-frontend` instead
4. (Optional) To prevent iOS Safari from auto-zooming on input/textarea focus, add `maximum-scale=1` to the viewport meta tag: `<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">`
