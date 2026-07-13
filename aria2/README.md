# aria2

Standalone BT downloader that routes all peer / tracker / DHT traffic through Surfshark via [gluetun](https://github.com/qdm12/gluetun). Extracted from `transcribe/` so transcribe's LLM / OS API calls can stay on direct network without VPN throttling.

Exposes a small REST API (`POST /torrents`, `GET /torrents`, `DELETE /torrents/{wrapper}`) on `http://aria2-gluetun:8080` reachable from other containers on the shared `my_network` Docker overlay.

## Design

- **One-shot aria2c per magnet** — no daemon, no shared session state. Each magnet spawns its own `aria2c` subprocess into a per-torrent wrapper folder under `/data/bt`. Subprocess exits when both seed-time (1440 min) and seed-ratio (1.0) limits are hit. State comes from disk (`.aria2` control file) + an in-memory dict of live `Popen` handles.
- **Kill-switch via netns sharing** — the `aria2` container has `network_mode: "service:aria2-gluetun"` and no networks of its own. If gluetun dies or the VPN tunnel drops, aria2 loses connectivity → no clear-text leak.
- **No port forwarding** — Surfshark's stated policy is that they don't offer port forwarding on any gateway. Aria2c binds an ephemeral port in its default range (6881-6999) and BT peers cannot reach us inbound. Seeding is limited to peers we handshaked with during download, so the `SEED_RATIO=1.0` target rarely gets hit and `SEED_TIME_MIN=1440` (24 h) is what actually terminates each subprocess. If you switch to a PF-supporting VPN (PIA / Proton / AirVPN), see the comment block in `bt_torrents.py` for the changes needed to reintroduce `--listen-port`.

## Config

WireGuard over OpenVPN — faster + lower CPU + faster reconnect. Generate the key pair in Surfshark's panel: **Account → VPN → Manual setup → WireGuard → let them generate**. They show the private key ONCE — copy it into Bitwarden. The assigned address (e.g. `10.14.0.42/16`) shows up on the same page.

Both env vars use compose's `${VAR:?...}` syntax so a missing export fails `docker compose up` at parse time.

```sh
export SURFSHARK_WG_PRIVATE_KEY='...long base64 blob...'
export SURFSHARK_WG_ADDRESSES='10.14.0.42/16'
docker compose up -d --build
```

The default region is `Netherlands` (P2P-friendly jurisdiction, dense European BT peer coverage). Edit `docker-compose.yml`'s `SERVER_REGIONS` to switch — Surfshark has no PF differences per region, so pick based on RTT / peer proximity / legal comfort.

## Verify

```sh
# aria2 process reachable through gluetun's netns?
curl -s http://aria2-gluetun:8080/health
# → {"ok":true}

# submit a magnet
curl -s -X POST http://aria2-gluetun:8080/torrents \
     -H 'content-type: application/json' \
     -d '{"magnet":"magnet:?xt=urn:btih:..."}'
# → {"wrapper":"..."}

# list
curl -s http://aria2-gluetun:8080/torrents

# delete (kill subprocess only; caller cleans up wrapper dir on the shared bind-mount)
curl -s -X DELETE http://aria2-gluetun:8080/torrents/<wrapper-name>
```

## Volume layout

Bind-mounted from `../transcribe/data/bt` on the host so downloads land where the transcribe pipeline reads them. Both containers must see the same host path or bt_filter's hardlinks won't work (must share `st_dev` for `os.link()` to succeed within the transcribe container).

If your transcribe repo lives elsewhere, adjust the `volumes:` entry in `docker-compose.yml` accordingly.

## Startup dependency

`aria2` waits for `aria2-gluetun` to start (`depends_on: service_started`), but not for the VPN tunnel to be fully up. If aria2c spawns before gluetun's WireGuard handshake completes, it just fails to reach trackers and retries on aria2's own schedule; WireGuard usually handshakes within 1-2 s so this is a self-healing hiccup rather than a real failure mode.
