# cloudflared

Runs a Cloudflare Tunnel to expose all services via subdomains on your domain.

## Cloudflare Setup (one-time)

1. Go to [Cloudflare Zero Trust](https://one.dash.cloudflare.com) → Networks → Tunnels → Create a tunnel
2. Copy the tunnel token
3. Under the tunnel → **Published application routes**, add one route per service:
   - `clock.domain.com` → `http://clock:80`
   - `twelvereader.domain.com` → `http://frontend:3000`
   - `yt-whisper.domain.com` → `http://web:8000`
   - `flashcard.domain.com` → `http://flashcard-frontend:80`
4. Go to Zero Trust → Access → Applications → Add an application → Self-hosted:
   - Domain: `*.domain.com`
   - Under Policies, add a rule: **Emails → `you@gmail.com`** (one-time PIN sent to your email)

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
