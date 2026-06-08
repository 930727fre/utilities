# kasm-desktop

Browser-accessible Ubuntu desktop, powered by [Kasm Workspaces](https://www.kasmweb.com/)' official `kasmweb/ubuntu-jammy-desktop` image. No custom code — just a compose file wrapping their image.

## Run

First run only — chown the bind mount to the container's `kasm-user` (UID 1000), otherwise the first-start profile copy fails with `Permission denied` and the desktop comes up broken:

```bash
mkdir -p data/home && sudo chown -R 1000:1000 data/home
docker compose up -d
```

(Docker creates the bind-mount dir as root; the container runs unprivileged.)

Access at `https://localhost:6901` (self-signed cert — accept the warning). Login: `kasm_user` / `123456`.

The user home directory persists to `./data/home/` on the host (gitignored). Drag-and-drop file upload, clipboard sync, and audio passthrough work in the browser UI.

### Reset the home folder

To wipe Chrome history / installed apps / everything and start with a fresh kasm-user home:

```bash
docker compose down
sudo rm -rf data/home
mkdir -p data/home && sudo chown -R 1000:1000 data/home
docker compose up -d
```

(Files inside `data/home/` are owned by UID 1000 because the container writes them as `kasm-user`, so `sudo` is needed to remove them from the host. Same chown step as first run keeps the new empty dir writable by the container.)

## Cloudflare Tunnel

To expose via subdomain, add a route in the Cloudflare tunnel: `desktop.domain.com → https://kasm-desktop:6901`. The container joins `my_network` so cloudflared can reach it by name.

Two settings must be right:

- **Service must be HTTPS, not HTTP** — Kasm refuses plaintext on principle (VNC password, keystrokes, and screen pixels travel over this channel). A plain-HTTP route gets rejected at the kasm side with `non-SSL connection disallowed` in the logs.
- **Enable "No TLS Verify"** — Kasm's cert is self-signed, so cloudflared can't verify the chain. The toggle is buried: edit the route → expand **Additional application settings** → expand **TLS** → toggle **No TLS Verify** on.

### Why "No TLS Verify" is safe here

The hop is still TLS-encrypted; cloudflared just isn't checking that the cert's CN matches the hostname or that a known CA signed it. End-to-end:

- phone → Cloudflare edge: real Cloudflare-issued TLS, fully verified
- Cloudflare → `cloudflared` container: tunnel-encrypted, authenticated by tunnel token
- `cloudflared` → `kasm-desktop:6901`: TLS-encrypted, cert not verified

For the last hop to be MITM'd, an attacker would need a container on `my_network` or root on the host — at which point they already have more access than the kasm session would expose. Baking a custom CA into both containers would close the gap formally but adds significant plumbing for ~zero practical security on a private Docker bridge you control.

## GPU

GPU passthrough is enabled (`count: all`) for NVENC streaming and Chrome's hardware-accelerated rendering. **Documented exception to the [gpu-broker](../gpu-broker) rule**: kasm doesn't coordinate through the broker because casual desktop use (idle / 1080p video) only touches a few hundred MB of VRAM, which coexists fine with one brokered ML job (whisper-medium ~5 GB, marker ~3-4 GB) on any 8 GB+ card.

If you start running heavy GPU work *inside* the desktop (4K video, WebGL games, local Stable Diffusion) AND a brokered ML job at the same time, you can OOM — bring kasm down for the duration of those concurrent loads. The broker enforces a mutex among its consumers, not a VRAM ceiling, so it can't detect or prevent this on its behalf.

## Password

`VNC_PW=123456` is hardcoded (Kasm requires ≥ 6 chars). Behind Cloudflare Access this is effectively a second factor on top of the email-PIN gate; for LAN-only access, change it. Don't put it in `.env`.
