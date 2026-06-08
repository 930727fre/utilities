# cloudflared

Runs a Cloudflare Tunnel to expose all services via subdomains on your domain.

## Cloudflare Setup (one-time)

1. Go to [Cloudflare Zero Trust](https://one.dash.cloudflare.com) → Networks → Tunnels → Create a tunnel
2. Copy the tunnel token
3. Under the tunnel → **Published application routes**, add one route per service:
   - `clock.domain.com` → `http://clock:80`
   - `marker.domain.com` → `http://marker-pipeline-frontend:3000`
   - `transcribe.domain.com` → `http://transcribe-frontend:8000`
   - `flashcard.domain.com` → `http://flashcard-frontend:80`
   - `keyboard.domain.com` → `http://keyboard-backend:8080`
   - `desktop.domain.com` → `https://kasm-desktop:6901` (HTTPS, enable "No TLS Verify" — self-signed cert)
4. Go to Zero Trust → Access → Applications → Add an application → Self-hosted:
   - Domain: `*.domain.com`
   - Under Policies, add a rule: **Emails → `you@gmail.com`** (one-time PIN sent to your email)

`ollama` is shared infrastructure (no public route) — only accessible from inside `my_network` on `http://ollama:11434`. As of the Gemini migration it's no longer started by default; bring it up only if you re-enable a local-LLM consumer.

## Usage

Start the tunnel first (it creates `my_network` automatically), then each service. The `ollama` line below is commented out because no current consumer needs it — uncomment if you revive local-LLM usage.

**Linux/macOS**
```bash
cd cloudflared && CLOUDFLARE_TUNNEL_TOKEN=<token> docker compose up -d
# cd ../ollama && docker compose up -d
cd ../marker-pipeline && docker compose up -d
cd ../transcribe && docker compose up -d
cd ../flashcard && docker compose up -d
cd ../keyboard && docker compose up -d
cd ../clock && docker compose up -d
```

**Windows CMD**
```cmd
cd cloudflared && set CLOUDFLARE_TUNNEL_TOKEN=<token> && docker compose up -d
:: cd ../ollama && docker compose up -d
cd ../marker-pipeline && docker compose up -d
cd ../transcribe && docker compose up -d
cd ../flashcard && docker compose up -d
cd ../keyboard && docker compose up -d
cd ../clock && docker compose up -d
```

**Windows PowerShell**
```powershell
cd cloudflared; $env:CLOUDFLARE_TUNNEL_TOKEN="<token>"; docker compose up -d
# cd ../ollama; docker compose up -d
cd ../marker-pipeline; docker compose up -d
cd ../transcribe; docker compose up -d
cd ../flashcard; docker compose up -d
cd ../keyboard; docker compose up -d
cd ../clock; docker compose up -d
```

Services communicate via the shared `my_network` Docker network — no host ports exposed. Cloudflare tunnel routes use **container names** as DNS hostnames (not service names), since cloudflared is a separate compose stack.
