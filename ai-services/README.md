# ai-services

LiteLLM gateway, n8n, and their shared infra (postgres, redis, mongo) for
`smarthome01`.

- **Deploy target**: `smarthome01:/usr/local/apps/docker-compose/ai-services/`
- **Runtime**: Docker on Linux
- **Public gateway**: `http://smarthome01.thefootonline.local:4000` (LiteLLM)
- **n8n UI**: `http://smarthome01.thefootonline.local:5678`

## Sync convention

The repo is authoritative. The workflow is:

1. Edit `docker-compose.yml` / `litellm-config.yaml` here, on a branch.
2. PR + merge to `main`.
3. Deploy: `rsync` the merged files onto smarthome01 into
   `/usr/local/apps/docker-compose/ai-services/`, then
   `docker compose up -d`.

Live-first edits (an incident hotfix on the host) MUST be captured back into
the repo in the same session that made them. The class of drift this sync
convention exists to kill is the one where a hand-copy on the host silently
races ahead of the repo for weeks and nobody notices until deploy.

If you find the host and the repo out of sync at the start of a session, stop
and reconcile before making further changes; do not layer a new change on top
of an unknown baseline.

## Secret-reference policy

**This repo carries references only, never secret values.** May 2026 incident
class. Concretely:

- `docker-compose.yml` and `litellm-config.yaml` reference every secret via
  `${VAR}` placeholders (compose) or `os.environ/VAR` (LiteLLM).
- Real values live in `.env` on the host. `.env` is gitignored.
- The keys the deploy needs are enumerated in `.env.example` — treat it as
  the contract. When a new secret is introduced, add its key to
  `.env.example` in the same PR that references it.
- If you ever see an actual secret value in a diff or in a file being
  committed here, STOP and escalate; do not scrub history yourself.

## Files

| File | Role |
|---|---|
| `docker-compose.yml` | Service topology. Volumes, ports, image pins, env-var references. |
| `litellm-config.yaml` | LiteLLM model list, provider credentials (env-referenced), routing. |
| `.env.example` | Documented list of env keys the deploy expects. Never contains values. |
| `.env` | Real values on the host. Gitignored. |
| `LITELLM_SETUP.md` | Historical setup notes. |

## Model routing

The estate's stable alias is `local/default` — always call that instead of a
concrete engine tag. To rotate the local default model, repoint only that
entry's `litellm_params.model` in `litellm-config.yaml`; no caller has to
change.

Spark-hosted models (`spark/*`) are served by a separate vLLM compose that
lives outside this repo. Do not add spark model definitions to
`docker-compose.yml` here — they belong on the spark host's own compose. Only
the LiteLLM routes (`spark/<name>` → `openai/<name>` at `SPARK_BASE_URL`) live
here.

## Pinned image

`ghcr.io/berriai/litellm:main-v1.82.3-stable.patch.2` — pinned after the
supply-chain attack on `main-latest` in March 2026. Never revert to
`main-latest` without a fresh advisory review; the pin note in the compose
file is load-bearing.
