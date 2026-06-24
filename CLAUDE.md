# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Codex Proxy** is a local **FastAPI** server that exposes an OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`), authenticates to ChatGPT via OAuth PKCE — impersonating the Codex CLI — and forwards chat requests to `chatgpt.com/backend-api/codex/responses`, translating the backend's SSE Responses API stream back into OpenAI chat completions format. Chat only — there is no embeddings endpoint (the Codex backend doesn't serve embeddings and a ChatGPT subscription has no API quota).

Because it's FastAPI, interactive OpenAPI docs are served automatically at **`/docs`** (Swagger) and **`/openapi.json`** (`/redoc` is disabled via `redoc_url=None`).

The whole app is a single file: **`app.py`**.

## Running

Primary path is Docker; tokens persist in the named volume `codex-data` (no host path is hardcoded — `TOKEN_FILE` defaults to `/app/data/tokens.json` in the image).

```bash
docker compose up                              # 1st run prints a login URL in the logs
```

Local (no Docker):

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5001     # TOKEN_FILE defaults to ./tokens.json
```

**First-run login:** OAuth runs in `lifespan` startup. There's no browser inside the container, so the auth URL is **printed to stdout** — open it on the host; the callback returns to `http://localhost:1455/auth/callback` (compose publishes 1455). Tokens are then reused/refreshed; subsequent starts need no login.

Tooling: **ruff** (lint + format, config in `ruff.toml`) and **pytest** smoke tests in `tests/` — they use `TestClient` **without** the lifespan, so OAuth login never runs (no network). Dev deps in `requirements-dev.txt`. Run locally: `pip install -r requirements-dev.txt && ruff check . && ruff format --check . && pytest -q`. CI (`.github/workflows/ci.yml`) runs lint + tests (3.11/3.12) + a Docker build; `docker-publish.yml` pushes the image to GHCR on `main` and on `v*` tags (semver, multi-arch).

## Dependencies

`requirements.txt`: `fastapi` + `uvicorn[standard]`. Backend/OAuth HTTP calls use **stdlib `urllib`** (not httpx) — blocking calls run fine because the routes are sync `def` (Starlette runs them in a threadpool) and SSE streaming uses a **sync generator** passed to `StreamingResponse`.

## Environment Variables

See `.env.example`. Code defaults live at the top of `app.py`.

- `CODEX_MODEL` — default model when a request omits `model` (default `gpt-5.4-mini`; a request's `model` overrides it)
- `CODEX_MODELS` — comma-separated list returned by `/v1/models` (cosmetic; defaults to the built-in frontier list in `app.py`, overridable so it survives model updates)
- `PROXY_PORT` — host port (container always listens on 5001)
- `PROXY_API_KEY` — if set, every request needs `Authorization: Bearer {key}` (enforced by the `require_auth` dependency, an `HTTPBearer` scheme that also renders the **Authorize** button in `/docs`)
- `CODEX_BACKEND`, `CODEX_REASONING_EFFORT`/`CODEX_REASONING_SUMMARY` (`medium`/`auto`), `BACKEND_TIMEOUT` (600s), `PROXY_DEBUG_EVENTS`, `TOKEN_FILE`

## Architecture (`app.py`)

### Auth — `TokenManager` + `oauth_login()`

- OAuth PKCE against `auth.openai.com` using the public **codex-cli** client (`CLIENT_ID = app_EMoamEEZ73f0CkXaXp7hrann`). Authorize request includes `id_token_add_organizations=true` and `codex_cli_simplified_flow=true`.
- `TokenManager` loads `access_token` / `refresh_token` / `id_token` from `TOKEN_FILE`, decodes the access-token JWT for `exp` + `chatgpt_account_id`, auto-refreshes 60s before expiry, re-logs-in if refresh fails. Thread-safe via `threading.Lock()`.

### Chat translation — `build_responses_payload()` + streaming

OpenAI chat request → Responses API body:
- `system` messages → top-level `instructions`; other messages → `input` items (`input_text`/`output_text`/`input_image`); `tool`/`function` messages → `function_call_output`; assistant `tool_calls` + legacy `function_call` → `function_call` items.
- `tools`, `tool_choice`, `parallel_tool_calls`, `reasoning`/`reasoning_effort`/`reasoning_summary` are converted and attached. Backend body always sets `store: false` and `stream: true` (backend **requires** streaming).
- `temperature`, `top_p`, `max_tokens` are intentionally **dropped** (the codex backend may reject them).

Two response paths:
- **Streaming** (`stream: true`): `stream_openai_chunks()` consumes backend events via `iter_sse_events()` and yields OpenAI `chat.completion.chunk` SSE — text → `content`, reasoning deltas → `reasoning_content`, function-call events reassembled into incremental `tool_calls` (state keyed by `output_index`, avoids re-sending already-streamed args), closing with `data: [DONE]`.
- **Non-streaming**: `collect_sse_response()` drains the stream into one `chat.completion` JSON. A non-`response.completed` terminal event becomes a `502` with the backend error.

Shared tool-call helpers: `convert_chat_tools`, `convert_tool_choice`, `convert_chat_tool_call`, `response_function_call_to_chat`, `extract_response_tool_calls`.

### Backend impersonation headers

`backend_headers()` sends the OAuth Bearer token plus headers that mimic the Codex CLI — `chatgpt-account-id`, `originator: codex_cli_rs`, `OpenAI-Beta: responses=experimental`, pinned `User-Agent: codex-cli/<version>`. Changing these may cause the backend to reject requests.

### Pydantic models

`ChatCompletionRequest` and `ChatMessage` (both `extra="allow"` so new API fields pass through) type the request body — this is what populates the `/docs` schema. The route calls `req.model_dump(exclude_none=True)` and feeds the existing dict-based translation logic.

### Hard-coded OAuth config

```
CLIENT_ID    = "app_EMoamEEZ73f0CkXaXp7hrann"   # public codex-cli client
AUTH_URL     = "https://auth.openai.com/oauth/authorize"
TOKEN_URL    = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
```

Baked in; don't change without understanding the OAuth client registration. Port 1455 must be free during login and is published by `docker-compose.yml`.
