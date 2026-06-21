# samba

Alternative read-only SMB server exposing [transcribe](../transcribe)'s library — same content as [webdav](../webdav), different protocol.

Reason it exists: macOS's native WebDAV client (`webdavfs`) is notoriously buggy with large video files — thumbnail-generation hangs, Spotlight indexing thrashing, sluggish browsing. SMB is mature, mainstream, and well-supported by every macOS app (Finder, IINA, VLC, nPlayer, Infuse) without the friction.

## When to use which

- **Infuse on iOS / iPad / Apple TV**: either works; pick one and stick to it
- **macOS Finder → IINA / VLC**: SMB strongly preferred (sidesteps the Finder/webdavfs hangs)
- **macOS Infuse**: either works (Infuse has its own client; doesn't touch webdavfs)

If you only ever watch on iOS + Apple TV via Infuse, [webdav](../webdav) is enough and you can skip this. Add SMB once Mac Finder browsing becomes a friction point.

The two stacks can run in parallel — webdav on port 8081, samba on 445, both exposing the same `transcribe/data/` directories. Different clients pick whichever they prefer.

## Stack

| Layer | Tech |
|------|------|
| Server | [`dperson/samba`](https://github.com/dperson/samba) — single Linux samba binary, config via CLI flags |
| Auth | None — guest access; Tailscale provides device-level access control upstream |
| Access | Tailscale (host's tailnet IP on port 445) |
| Mounts | `/bt` (transcribe's BT downloads + annotated English + Chinese SRTs) + `/yt` (transcribe's YouTube output), both read-only |

Share names mirror the WebDAV path segments exactly, so the protocol switch is the only thing changing.

## Run

```sh
docker compose up -d
```

No env vars, no config files. Everything's in the compose `command:`.

## Client setup

### macOS Finder

**Cmd+K** → `smb://<host-tailnet-ip>/bt` → click Connect → "Guest" → Connect.

The share mounts at `/Volumes/bt`. Drag any video to IINA / VLC; sibling SRT files auto-load (including the `.zh-tw.srt` Chinese sidecars). IINA Settings → Subtitle → Secondary Subtitle Track is where you turn on the dual-display.

To avoid Finder hanging on large videos, disable icon previews on the share once mounted:

```sh
defaults write com.apple.finder ShowIconPreview -bool false
killall Finder
```

(Or per-folder via Cmd+J → uncheck "Show icon preview" inside the SMB folder.)

### iOS Infuse / nPlayer

Add Files → Network → SMB
- Server: `<host-tailnet-ip>`
- Anonymous / Guest enabled, username blank
- Browse the auto-listed `bt` / `yt` shares

### Apple TV Infuse

Settings → Library → Add Files → Network Share → SMB → guest mode → same fields as iOS.

## Read-only enforcement

Two layers:

1. **Bind mount** (`./data/bt:/mount/bt:ro`) — kernel-level read-only, the strongest guarantee
2. **Samba share config** (`readonly=yes` flag in `command:`) — protocol-level, what clients see

Either layer alone would suffice; both together means even a misconfigured samba container can't accidentally accept writes. Deletes happen via the transcribe UI (per-torrent ✕ button) or shell on the host.

## Port note

If host port 445 is already in use (rare on Linux; macOS hosts running their own SMB server would conflict), change the host side of the port mapping — e.g. `"4450:445"` — and connect via `smb://<host-tailnet-ip>:4450/bt`.
