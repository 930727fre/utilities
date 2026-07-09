# jellyfin

Media server. Reads read-only from `transcribe`'s canonical library + `yt-dlp` downloads. Cloudflare tunnel + Tailscale LAN both point at port 8096.

## Run

```sh
docker compose up -d
```

Everything persists under `data/config/` (users, watch progress, plugin state) and `data/cache/` (transcode temp + poster cache).

## GPU config (encoding.xml)

Only meaningful edits vs a fresh Jellyfin install — everything else is default. Card is an RTX 3060 (Ampere, NVENC 7th gen, NVDEC full-feature).

| Setting | Value | Why |
|---|---|---|
| `HardwareAccelerationType` | `nvenc` | Route transcodes through NVENC |
| `EnableHardwareEncoding` | `true` | Actually let NVENC output; without this the accel type is decode-only |
| `AllowHevcEncoding` | `true` | HEVC output = ~40% less bandwidth than H.264 at same quality. iOS 17+ / modern browsers eat HEVC in HLS fine |
| `AllowAv1Encoding` | `false` | **Do not enable.** Ampere has no AV1 encoder — that's Ada (RTX 40xx) only. Enabling it here would break transcodes that route to AV1 output |
| `HardwareDecodingCodecs` | h264, hevc, av1, vp9, vp8, vc1, mpeg4, mpeg2video | 3060 NVDEC handles all of these. Old codecs (vc1, mpeg2) never appear in the library but are harmless to leave on |
| `EnableDecodingColorDepth10HevcRext` | `true` | 10-bit HEVC — most x265 web-dl releases |
| `EnableDecodingColorDepth12HevcRext` | `true` | 12-bit HEVC — Ampere supports it, some HDR remuxes need it |
| `EnableEnhancedNvdecDecoder` | `true` | Nvidia's newer decode path, more format coverage |
| `PreferSystemNativeHwDecoder` | `true` | OS-native hwaccel over generic; the two are equivalent on Linux but this settles the tie-breaker |
| `EnableSegmentDeletion` | `false` | Keep HLS segments during a session so seek doesn't re-encode from scratch |

Tonemapping (`EnableTonemapping`, `EnableVppTonemapping`, ...) is deliberately **off**. Turning it on forces HDR → SDR conversion during transcode; the current library is mostly SDR so it's pure GPU cost with no perceptual gain. Flip on later if the library gets HDR-heavy.

## Burn-in subtitles are client-side, not server

**There is no `AlwaysBurnInSubtitle` server config.** The setting lives in each client:

- Jellyfin Web: browser localStorage. Set once per browser
- Jellyfin Mobile app (iOS/Android): stored on device
- Infuse / Swiftfin / Streamyfin: each has its own preference
- Cast targets (Chromecast / AirPlay): follow the sending client's profile

Mechanism: on playback start, the client sends a `DeviceProfile` JSON declaring supported subtitle delivery methods. If the profile only advertises `Encode`, the server has no choice but to burn in. If the profile lists `Embed`/`External`, the server sends subs as separate tracks and the client renders them.

So "always burn in" in Jellyfin Web ≠ automatic burn-in in Infuse on iOS — set it on each device that plays. There's no way to force it server-wide without patching jellyfin-web's default profile.

## Client strategy

Primary: **Infuse Free** — direct-plays every codec the library uses (H.264, HEVC 8/10-bit, EAC3, AAC). Server GPU stays idle, playback progress writes back to Jellyfin as source of truth so Continue Watching syncs across all clients.

Fallback: **Jellyfin Web with burn-in "all"** — for tracks Infuse Free won't decode (TrueHD, DTS-HD MA, Dolby Vision Profile 5/8). Uses the NVENC path configured above. Same account, progress merges into the same DB row.
