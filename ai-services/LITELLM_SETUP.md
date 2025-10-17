# LiteLLM Proxy Setup Guide

This document describes the LiteLLM proxy configuration and setup for the AI services stack.

## Overview

LiteLLM acts as a unified proxy gateway to multiple LLM providers with features including:
- API key management and authentication
- Request routing to upstream services
- Database-backed virtual key system
- Support for OpenAI, Anthropic, Ollama, and custom endpoints

## Architecture

```
Client Request
    ↓
LiteLLM Proxy (localhost:4000)
    ↓
PostgreSQL Database (authentication/key management)
    ↓
Upstream Services:
  - OpenAI API
  - Anthropic API
  - Ollama (local + smart home)
  - Custom LiteLLM upstream (uat-litellm.wsd.com)
```

## Configuration Files

### 1. `docker-compose.yml`

**Services:**
- `postgres`: PostgreSQL 16 database for LiteLLM key management
  - Port: `5433:5432` (external:internal)
  - Database: `litellm`
  - Credentials: `litellm` / `${POSTGRES_PASSWORD:-litellm_password}`

- `litellm`: LiteLLM proxy service
  - Port: `4000:4000`
  - Config: `./litellm-config.yaml`
  - Environment variables from `.env`
  - Web UI: `http://localhost:4000/ui`

### 2. `litellm-config.yaml`

**Key Sections:**

**General Settings:**
```yaml
general_settings:
  master_key: ${LITELLM_MASTER_KEY}
  allow_non_registered_models: true
```

**Model Configuration with Wildcard Routing:**

LiteLLM supports wildcard routing, allowing dynamic access to **all models** from a provider without explicitly defining each one:

```yaml
model_list:
  # Upstream LiteLLM instance
  - model_name: wsdlitellm/wsd-atlas
    litellm_params:
      model: openai/wsd-atlas
      api_base: ${UPSTREAM_LITELLM_BASE_URL}
      api_key: ${UPSTREAM_LITELLM_API_KEY}

  # OpenAI - all models dynamically accessible
  - model_name: "openai/*"
    litellm_params:
      model: "openai/*"
      api_key: os.environ/OPENAI_API_KEY

  # Anthropic - all models dynamically accessible
  - model_name: "anthropic/*"
    litellm_params:
      model: "anthropic/*"
      api_key: os.environ/ANTHROPIC_API_KEY

  # Ollama - all models
  - model_name: "ollama/*"
    litellm_params:
      model: "ollama/*"
      api_base: ${OLLAMA_LOCAL_BASE_URL}
```

**Wildcard Benefits:**
- Access ANY model from OpenAI (`gpt-4`, `gpt-4o`, `gpt-4o-mini`, etc.) without config changes
- Access ANY model from Anthropic (`claude-3-5-sonnet-20241022`, `claude-3-5-haiku-20241022`, etc.)
- No restart needed when providers release new models
- Single configuration per provider instead of dozens of model definitions

**Important Notes:**
- When proxying to another LiteLLM instance, use `model: openai/model-name` format, NOT `provider: litellm`
- Ollama wildcards work but don't auto-discover locally installed models (query Ollama API directly for model list)

### 3. `.env` File

Required environment variables:
```bash
# LiteLLM Master Key (must start with 'sk-')
LITELLM_MASTER_KEY=sk-65wXqHtszsYAcNKs7xJd

# PostgreSQL Password
POSTGRES_PASSWORD=litellm_password

# API Keys for upstream providers
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key

# Upstream LiteLLM instance
UPSTREAM_LITELLM_BASE_URL=https://uat-litellm.wsd.com
UPSTREAM_LITELLM_API_KEY=your-upstream-key

# Ollama endpoints
OLLAMA_LOCAL_BASE_URL=http://host.docker.internal:11434
OLLAMA_SMARTHOME_BASE_URL=http://smarthome01.local:11434

# UI Credentials
UI_USERNAME=thefoot
UI_PASSWORD=th3f00tn355123

# PostgreSQL Password
POSTGRES_PASSWORD=litellm_password
```

## Initial Setup

### 1. Start Services

```bash
docker compose up -d postgres litellm
```

### 2. Bootstrap Admin Access

When starting LiteLLM with a database for the first time, you need to bootstrap the admin user:

```bash
# 1. Insert master key hash into database
MASTER_KEY_HASH=$(echo -n "sk-65wXqHtszsYAcNKs7xJd" | shasum -a 256 | cut -d' ' -f1)

docker exec postgres psql -U litellm -d litellm -c "
INSERT INTO \"LiteLLM_VerificationToken\" (token, key_alias, models, permissions)
VALUES ('${MASTER_KEY_HASH}', 'master-key', ARRAY['all-proxy-models'], '{\"allow_all\": true}'::jsonb);
"

# 2. Create admin user
docker exec postgres psql -U litellm -d litellm -c "
INSERT INTO \"LiteLLM_UserTable\" (user_id, user_alias, user_role, user_email)
VALUES ('admin-user', 'Admin', 'proxy_admin', 'admin@example.com');
"

# 3. Link master key to admin user
docker exec postgres psql -U litellm -d litellm -c "
UPDATE \"LiteLLM_VerificationToken\"
SET user_id = 'admin-user'
WHERE token = '${MASTER_KEY_HASH}';
"

# 4. Restart LiteLLM to pick up changes
docker restart litellm
```

### 3. Generate Virtual Keys

Use the master key to generate virtual API keys:

```bash
curl --location 'http://localhost:4000/key/generate' \
  --header 'Authorization: Bearer sk-65wXqHtszsYAcNKs7xJd' \
  --header 'Content-Type: application/json' \
  --data '{
    "models": ["all-proxy-models"],
    "key_alias": "my-api-key",
    "metadata": {
      "user": "user@example.com",
      "purpose": "production"
    }
  }'
```

**Response:**
```json
{
  "key": "sk-vwkNYiDzht-GSWVFqXDh0Q",
  "key_alias": "my-api-key",
  ...
}
```

## Usage

### Making API Requests

Use the generated virtual key to make requests with **any** model from configured providers:

```bash
# OpenAI models
curl --location 'http://localhost:4000/v1/chat/completions' \
  --header 'Authorization: Bearer sk-vwkNYiDzht-GSWVFqXDh0Q' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Anthropic models
curl --location 'http://localhost:4000/v1/chat/completions' \
  --header 'Authorization: Bearer sk-vwkNYiDzht-GSWVFqXDh0Q' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "claude-3-5-sonnet-20241022",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Upstream LiteLLM model
curl --location 'http://localhost:4000/v1/chat/completions' \
  --header 'Authorization: Bearer sk-vwkNYiDzht-GSWVFqXDh0Q' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "wsdlitellm/wsd-atlas",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

**No configuration changes needed** - wildcard routing automatically handles all models!

### Available Models

Check available models:
```bash
curl http://localhost:4000/v1/models \
  --header 'Authorization: Bearer sk-vwkNYiDzht-GSWVFqXDh0Q'
```

### Health Check

```bash
curl http://localhost:4000/health/liveliness
```

### Web UI Access

LiteLLM includes a web-based admin UI for managing keys, users, teams, and monitoring usage.

**Access URL:** http://localhost:4000/ui

**Credentials:**
- Username: `thefoot`
- Password: `th3f00tn355123`

**UI Features:**
- View and manage API keys
- Monitor usage and spending
- Manage users and teams
- View model availability
- Track request logs
- Configure budgets and limits

## Key Management

### List All Keys

```bash
curl --location 'http://localhost:4000/key/list' \
  --header 'Authorization: Bearer sk-65wXqHtszsYAcNKs7xJd'
```

### Update Key Permissions

To allow a key to access all models:

```bash
docker exec postgres psql -U litellm -d litellm -c "
UPDATE \"LiteLLM_VerificationToken\"
SET models = ARRAY['all-proxy-models']
WHERE key_alias = 'api-key';
"
```

### Delete a Key

```bash
curl --location 'http://localhost:4000/key/delete' \
  --header 'Authorization: Bearer sk-65wXqHtszsYAcNKs7xJd' \
  --header 'Content-Type: application/json' \
  --data '{
    "key": "sk-vwkNYiDzht-GSWVFqXDh0Q"
  }'
```

## Current Keys

### Master Key (Admin)
- **Key:** `sk-65wXqHtszsYAcNKs7xJd`
- **User:** `admin-user` (proxy_admin role)
- **Permissions:** All admin operations
- **Use:** Generating/managing virtual keys

### Virtual Key
- **Key:** `sk-vwkNYiDzht-GSWVFqXDh0Q`
- **Alias:** `api-key`
- **Models:** `all-proxy-models` (access to all models)
- **Use:** Making API requests

## Troubleshooting

### Issue: "No connected db" Error

**Cause:** DATABASE_URL environment variable not set or database not accessible

**Solution:**
```bash
# Check if DATABASE_URL is set
docker exec litellm env | grep DATABASE_URL

# If missing, add to docker-compose.yml and recreate container
docker compose up -d litellm
```

### Issue: "LLM Provider NOT provided" Error

**Cause:** Incorrect provider configuration for upstream models

**Solution:** For OpenAI-compatible endpoints (including other LiteLLM instances), use:
```yaml
model: openai/model-name
```
NOT:
```yaml
provider: litellm  # ❌ Invalid
```

### Issue: "Only proxy admin can generate keys" Error

**Cause:** Master key not linked to admin user in database

**Solution:** Follow "Bootstrap Admin Access" steps above

### Issue: Connection Error to Upstream

**Cause:** Upstream service unreachable (network, VPN, firewall, or service down)

**Solution:**
1. Check upstream service is running
2. Verify network connectivity
3. Check firewall/VPN settings
4. Test upstream URL directly:
   ```bash
   curl https://uat-litellm.wsd.com/health
   ```

## Database Schema

Key tables:
- `LiteLLM_VerificationToken`: API keys and their permissions
- `LiteLLM_UserTable`: User accounts and roles
- `LiteLLM_BudgetTable`: Spending limits and budgets

To inspect:
```bash
docker exec postgres psql -U litellm -d litellm -c "\dt"
```

## Security Notes

1. **Never commit `.env` file** - Contains sensitive API keys
2. **Rotate master key regularly** - Update in both `.env` and database
3. **Use virtual keys for applications** - Never use master key directly in apps
4. **Set budget limits** - Prevent runaway costs:
   ```json
   {
     "max_budget": 100.0,
     "budget_duration": "30d"
   }
   ```

## Resources

- [LiteLLM Documentation](https://docs.litellm.ai/)
- [Virtual Keys Guide](https://docs.litellm.ai/docs/proxy/virtual_keys)
- [Supported Providers](https://docs.litellm.ai/docs/providers)
