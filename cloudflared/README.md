# cloudflared

Runs a Cloudflare Tunnel to expose all services via subdomains on `930727fre.dev`.

## Cloudflare Setup (one-time)

1. Go to [Cloudflare Zero Trust](https://one.dash.cloudflare.com) → Networks → Tunnels → Create a tunnel
2. Copy the tunnel token
3. Under the tunnel → **Published application routes**, add one route per service:
   - `clock.930727fre.dev` → `http://clock:80`
   - `twelvereader.930727fre.dev` → `http://frontend:3000`
   - `yt-whisper.930727fre.dev` → `http://web:8000`
4. Go to Zero Trust → Access → Applications → Add an application → Self-hosted:
   - Domain: `*.930727fre.dev`
   - Under Policies, add a rule: **Emails → `930727fre@gmail.com`** (one-time PIN sent to your email)

## Usage

Start the tunnel first (it creates `my_network` automatically), then each service:

**Linux/macOS**
```bash
cd cloudflared && CLOUDFLARE_TUNNEL_TOKEN=<token> docker compose up -d
cd ../yt-whisper && docker compose up -d
cd ../TwelveReader && docker compose up -d
cd ../clock && docker compose up -d
```

**Windows CMD**
```cmd
cd cloudflared && set CLOUDFLARE_TUNNEL_TOKEN=<token> && docker compose up -d
cd ../yt-whisper && docker compose up -d
cd ../TwelveReader && docker compose up -d
cd ../clock && docker compose up -d
```

**Windows PowerShell**
```powershell
cd cloudflared; $env:CLOUDFLARE_TUNNEL_TOKEN="<token>"; docker compose up -d
cd ../yt-whisper; docker compose up -d
cd ../TwelveReader; docker compose up -d
cd ../clock; docker compose up -d
```

Services communicate via the shared `my_network` Docker network — no host ports exposed.
