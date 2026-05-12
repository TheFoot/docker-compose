# ai-services (smarthome01)

The AI services stack as deployed on **smarthome01** (`/usr/local/apps/docker-compose/ai-services/`).

This is the smarthome01 variant of the stack. The repository root also has an
`ai-services/` directory which is the **macbook** variant — they have diverged
(different volume paths, different ports, different service set, different
LiteLLM image pinning). Keep both until/unless a unification effort consolidates
them.

## Services

| Service  | Image                                                | Purpose                               |
|----------|------------------------------------------------------|---------------------------------------|
| mongo    | `mongo:latest` (auth-required)                       | Application document store            |
| postgres | `postgres:16-alpine`                                 | LiteLLM key/budget management         |
| redis    | `redis:latest`                                       | n8n queue backend                     |
| litellm  | `ghcr.io/berriai/litellm:main-v1.82.3-stable.patch.2` | Pinned LiteLLM proxy (gateway to LLMs) |
| n8n      | `docker.n8n.io/n8nio/n8n`                            | Workflow automation                   |

LiteLLM is pinned to `main-v1.82.3-stable.patch.2` following the March 2026 PyPI
supply-chain attack on `v1.82.7`/`v1.82.8`. **Do not switch back to `main-latest`
without checking the LiteLLM security advisory feed.**

## Network

Custom bridge `br-ai-services`. The compose file binds postgres/litellm/n8n on
`0.0.0.0` (LAN reachable); mongo + redis are loopback-only plus an explicit LAN
bind to `10.137.1.49:27017` for cross-host access.

## Persistent data

Bind mounts under `/mnt/data/ai-services/{mongo,postgres,n8n}` on the host.
These are **not** in this repo (intentionally) — they are runtime data and live
only on smarthome01.

## Environment

A `.env` file alongside `docker-compose.yml` is required at runtime and is
**gitignored**. Required keys:

```
LITELLM_MASTER_KEY=          # sk- prefixed
LITELLM_ALLOW_NON_REGISTERED_MODELS=true
OPENAI_API_KEY=              # sk-proj-...
ANTHROPIC_API_KEY=           # sk-ant-...
SPARK_BASE_URL=              # e.g. http://10.137.1.27:8000/v1
UI_USERNAME=
UI_PASSWORD=
POSTGRES_PASSWORD=
N8N_API_KEY=                 # JWT issued by the running n8n instance
RESEND_API_KEY=              # re_...
```

These should move to the HQ SOPS+age vault (`node core/tools/secrets/bin/secrets`)
as part of the broader secrets-migration effort; see the related upstream
`ai-services/LITELLM_SETUP.md` for additional context.

## Running

```
docker compose up -d
```

LiteLLM admin UI: `http://smarthome01.thefootonline.local:4000/ui`
n8n UI:            `http://smarthome01.thefootonline.local:5678/`
