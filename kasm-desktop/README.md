# kasm-desktop

Browser-accessible Ubuntu desktop, powered by [Kasm Workspaces](https://www.kasmweb.com/)' official `kasmweb/ubuntu-jammy-desktop` image. No custom code — just a compose file wrapping their image.

## Run

```bash
docker compose up -d
```

Access at `https://localhost:6901` (self-signed cert — accept the warning). Login: `kasm_user` / `1234`.

The user home directory persists to `./home/` on the host (gitignored). Drag-and-drop file upload, clipboard sync, and audio passthrough work in the browser UI.

## Cloudflare Tunnel

To expose via subdomain, add a route in the Cloudflare tunnel: `desktop.domain.com → https://kasm-desktop:6901` (HTTPS — Kasm only listens on TLS, set "No TLS Verify" since the cert is self-signed). The container joins `my_network` so cloudflared can reach it by name.

## GPU

No GPU passthrough — the use case is casual browsing, so libx264 on CPU handles the stream and Chrome renders pages on CPU. If you later need NVENC for smoother streaming or hardware-accelerated WebGL / 4K video inside the desktop, add back a `deploy.resources.reservations.devices` block — but be aware kasm would then hold the GPU continuously and conflict with [gpu-broker](../gpu-broker) consumers (transcribe / xyt / keyboard / marker-pipeline).

## Password

`VNC_PW=1234` is hardcoded. Behind Cloudflare Access this is effectively a second factor on top of the email-PIN gate; for LAN-only access, change it. Don't put it in `.env`.
