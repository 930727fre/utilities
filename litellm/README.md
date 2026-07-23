# litellm

Personal LiteLLM proxy — single container that fronts Claude (Anthropic) and
Gemini (Google) with an OpenAI-compatible API. No master key, no DB, no cost
tracking. Purpose is centralising provider API keys and giving every other
`utilities/` service one URL to call.

## Setup

Keys live in Bitwarden and are exported into the shell that runs `docker
compose up`. They never touch git and never enter Claude Code's context.

```sh
export ANTHROPIC_API_KEY='sk-ant-...'
export GEMINI_API_KEY='AIza...'
docker compose up -d --build
```

Forget an export → compose parse loud-fails immediately, no silent
half-broken state.

## Usage from other utilities containers

```sh
# From anywhere on my_network:
curl http://litellm:4000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{
    "model": "anthropic/claude-opus-4-8",
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

Model name is always `<provider>/<model-id>` — `anthropic/claude-*`,
`gemini/gemini-*`. Wildcard routing means new models work automatically
without editing `config.yaml`.

## Listing configured models

`GET /v1/models` returns whatever's declared in `config.yaml` — with
wildcards, that's just two entries (`anthropic/*` and `gemini/*`), not
the live list from each provider. For live provider models, hit each
provider's own `models` endpoint directly (auth handled by the exported
env vars, or go through the proxy with the model set to
`anthropic/claude-3.5-*` etc. and let the proxy attach the key).

## Security

- No `master_key`: any my_network container or process on the host can
  call the proxy. Fine for a single-user setup — the trust boundary is
  "my machine".
- Port 4000 is bound to the host too (for `curl localhost:4000` from
  the shell). If you want proxy-only-from-containers, remove the `ports:`
  block from `docker-compose.yml`.
- No DB, no logs of prompt content beyond default container stdout.
- Rotate provider keys in Bitwarden → re-export → `docker compose up -d`.

## Adding auth later

If you ever want to expose this beyond localhost (e.g. via a tunnel),
add to `config.yaml` under `general_settings:`:

```yaml
general_settings:
  master_key: sk-...  # generate one, store in Bitwarden
```

Then every client call needs `Authorization: Bearer sk-...`.
