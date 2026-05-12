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
