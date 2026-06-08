# kasm-desktop

Browser-accessible Ubuntu desktop, powered by [Kasm Workspaces](https://www.kasmweb.com/)' official `kasmweb/ubuntu-jammy-desktop` image. No custom code — just a compose file wrapping their image.

## Run

```bash
docker compose up -d
```

Access at `https://localhost:6901` (self-signed cert — accept the warning). Login: `kasm_user` / `1234`.

The user home directory persists to `./data/home/` on the host (gitignored). Drag-and-drop file upload, clipboard sync, and audio passthrough work in the browser UI.

## Cloudflare Tunnel

To expose via subdomain, add a route in the Cloudflare tunnel: `desktop.domain.com → https://kasm-desktop:6901` (HTTPS — Kasm only listens on TLS, set "No TLS Verify" since the cert is self-signed). The container joins `my_network` so cloudflared can reach it by name.

## GPU

GPU passthrough is enabled (`count: all`) for NVENC streaming and Chrome's hardware-accelerated rendering. **Documented exception to the [gpu-broker](../gpu-broker) rule**: kasm doesn't coordinate through the broker because casual desktop use (idle / 1080p video) only touches a few hundred MB of VRAM, which coexists fine with one brokered ML job (whisper-medium ~5 GB, marker ~3-4 GB) on any 8 GB+ card.

If you start running heavy GPU work *inside* the desktop (4K video, WebGL games, local Stable Diffusion) AND a brokered ML job at the same time, you can OOM — bring kasm down for the duration of those concurrent loads. The broker enforces a mutex among its consumers, not a VRAM ceiling, so it can't detect or prevent this on its behalf.

## Password

`VNC_PW=1234` is hardcoded. Behind Cloudflare Access this is effectively a second factor on top of the email-PIN gate; for LAN-only access, change it. Don't put it in `.env`.
