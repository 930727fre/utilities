# aria2

Standalone BT downloader that routes all peer / tracker / DHT traffic through PIA via [gluetun](https://github.com/qdm12/gluetun). Extracted from `transcribe/` so transcribe's LLM/OS API calls can stay on direct network without VPN throttling.

Exposes a small REST API (`POST /torrents`, `GET /torrents`, `DELETE /torrents/{wrapper}`) on `http://aria2-gluetun:8080` reachable from other containers on the shared `my_network` Docker overlay.

## Design

- **One-shot aria2c per magnet** — no daemon, no shared session state. Each magnet spawns its own `aria2c` subprocess into a per-torrent wrapper folder under `/data/bt`. Subprocess exits when both seed-time (1440 min) and seed-ratio (1.0) limits are hit. State comes from disk (`.aria2` control file) + an in-memory dict of live `Popen` handles.
- **Kill-switch via netns sharing** — the `aria2` container has `network_mode: "service:aria2-gluetun"` and no networks of its own. If gluetun dies or the VPN tunnel drops, aria2 loses connectivity → no clear-text leak.
- **PIA port forwarding** — gluetun writes the negotiated peer port to `/tmp/gluetun/forwarded_port`; that path is shared with the aria2 container via a named docker volume (`gluetun-shared`). Each aria2c spawn reads the file and passes `--listen-port=<PIA-port>` so incoming BT peer connections actually reach us. Without this, seeding is severely capacity-limited (typical ratio stalls around 0.1-0.3).

## Config

Only PIA credentials are required. Both use compose's `${VAR:?...}` syntax so a missing export fails `docker compose up` at parse time.

```sh
export PIA_USER=...
export PIA_PASSWORD=...
docker compose up -d --build
```

The default region is `Netherlands` (a PIA gateway that supports port forwarding — US regions dropped PF years ago). Edit `docker-compose.yml` to switch to Romania / Sweden / Switzerland / Czech Republic / Israel / Spain if you prefer.

## Verify

```sh
# tunnel + forwarded port ready?
curl -s http://aria2-gluetun:8080/health
# → {"ok":true,"forwarded_port":54321}

# submit a magnet
curl -s -X POST http://aria2-gluetun:8080/torrents \
     -H 'content-type: application/json' \
     -d '{"magnet":"magnet:?xt=urn:btih:..."}'
# → {"wrapper":"..."}

# list
curl -s http://aria2-gluetun:8080/torrents

# delete
curl -s -X DELETE http://aria2-gluetun:8080/torrents/<wrapper-name>
```

## Volume layout

Bind-mounted from `../transcribe/data/bt` on the host so downloads land where the transcribe pipeline reads them. Both containers must see the same host path or bt_filter's hardlinks won't work (must share `st_dev` for `os.link()` to succeed within the transcribe container).

If your transcribe repo lives elsewhere, adjust the `volumes:` entry in `docker-compose.yml` accordingly.

## Startup dependency

`aria2` waits for `aria2-gluetun` to start (`depends_on: service_started`), but not for the VPN tunnel to be fully up. If aria2c spawns before gluetun negotiates the forwarded port, `--listen-port` gets omitted (falls back to aria2's default) with a log line. The next spawn after gluetun is ready picks up the correct port. In practice, if you `docker compose up` cold, the first submission is likely to hit this — resubmit after ~10 s.
