# kasm-desktop

Browser-accessible Ubuntu desktop, powered by [Kasm Workspaces](https://www.kasmweb.com/)' official `kasmweb/ubuntu-jammy-desktop` image. No custom code — just a compose file wrapping their image.

## Run

```bash
docker compose up -d
```

Access at `https://localhost:6901` from the host, or `https://<host-tailnet-name>:6901` from other devices on the same Tailscale tailnet (self-signed cert — accept the warning once per client). Login: `kasm_user` / `123456`.

Not exposed to the public internet on purpose — the full desktop, clipboard sync, and keystroke capture make the blast radius of a compromise too big for CF Access's email-PIN gate to be a reasonable trust boundary. Tailnet-only keeps it inside a WireGuard perimeter you already control.

The user home lives inside the container — `docker compose down` wipes Chrome history, installed apps, and everything else. To start fresh, just recreate the container.

Drag-and-drop file upload, clipboard sync, and audio passthrough work in the browser UI.

## GPU

GPU passthrough is enabled (`count: all`) for NVENC streaming and Chrome's hardware-accelerated rendering. **Documented exception to the [gpu-broker](../gpu-broker) rule**: kasm doesn't coordinate through the broker because casual desktop use (idle / 1080p video) only touches a few hundred MB of VRAM, which coexists fine with one brokered ML job (whisper-medium ~5 GB, marker ~3-4 GB) on any 8 GB+ card.

If you start running heavy GPU work *inside* the desktop (4K video, WebGL games, local Stable Diffusion) AND a brokered ML job at the same time, you can OOM — bring kasm down for the duration of those concurrent loads. The broker enforces a mutex among its consumers, not a VRAM ceiling, so it can't detect or prevent this on its behalf.

## Password

`VNC_PW=123456` is hardcoded (Kasm requires ≥ 6 chars). Since access is already gated by tailnet membership, this is effectively a shared secret with every device on the tailnet — treat it as convenience, not real auth. Change it if you ever add a less-trusted device to the tailnet. Don't put it in `.env`.
