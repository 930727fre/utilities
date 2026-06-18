# webdav

Read-only WebDAV server exposing [transcribe](../transcribe)'s library — both the BT downloads (transcribe spawns aria2c when you submit a magnet on the qb tab) and the YouTube downloads — for [Infuse](https://firecore.com/infuse) (or any other player that speaks WebDAV: VLC, Kodi, etc.).

Replaces Jellyfin in the playback layer for this stack — Infuse handles codec decoding (universal) and offers iCloud-backed progress sync across iPhone + Mac, which is all this user needs. No transcoding, no metadata catalog, no library scans. Just files.

## Stack

| Layer | Tech |
|------|------|
| Server | [`hacdias/webdav`](https://github.com/hacdias/webdav) — single Go binary, YAML config |
| Auth | None — Tailscale provides device-level access control upstream |
| Access | Tailscale (host's tailnet IP on port 8081) |
| Mounts | `/media/qb` (transcribe's BT downloads + annotated SRTs) + `/media/yt` (transcribe's YouTube output), both read-only |

Read-only is deliberate: Infuse is for browsing and playback, not library management. Deletes happen via shell.

## Run

```sh
docker compose up -d
```

`config.yaml` is checked in — there's no secret in it. Auth is disabled because Tailscale already gates who can reach the port. If you ever expose this beyond Tailscale (Cloudflare Tunnel, public port), turn auth back on (`auth: true` + `users:` block) before doing so.

## Infuse setup

iPhone / Mac → **Library → Add Files → Other → WebDAV**

- Server: `http://<host-tailnet-ip>:8081`
- Username / password: leave blank

The `:8081` is mandatory — Infuse defaults to port 80 if you omit it, which nothing's listening on. Change the host-side port in `docker-compose.yml` if you want a different number; 8081 was chosen arbitrarily.

Infuse auto-detects sidecar `.srt` files next to each video, including the annotated ones produced by [transcribe](../transcribe).
