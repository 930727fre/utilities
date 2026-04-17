# utility

A collection of self-hosted tools, each containerized with Docker.

| Tool | Description |
|------|-------------|
| [clock](./clock) | Workout interval timer (1 min work / 30 sec rest) |
| [TwelveReader](./TwelveReader) | EPUB audiobook reader with on-demand TTS and synchronized highlighting |
| [yt-whisper](./yt-whisper) | YouTube video downloader with GPU-accelerated Whisper transcription |

## Usage

Each tool has its own `docker-compose.yml`. Run from the tool's directory:

```bash
cd <tool>
docker compose up
```
