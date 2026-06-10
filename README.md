# beans_proxy

A lightweight proxy for LLM API calls that records token usage by API key. See
[spec.md](spec.md) for the full design.

## What it does

You point Cline / Copilot CLI / any OpenAI-compatible client at Beans Proxy
using a per-task pseudo-API key (e.g. `sk-task-12345`). Beans Proxy forwards
the request to the upstream LLM API (OpenRouter), and writes a record of
input/output token usage — keyed on the pseudo-API key — to
`token_usage/<pseudo_key>.json`. Downstream systems can then read the JSON
files directly, or hit `GET /usage/{pseudo_key}` to retrieve them.

## Quick command reference

```bash
# Start the proxy in the background (loads .env in the current dir)
./beans.sh                       # default: start in background
./beans.sh --foreground          # run in foreground (Ctrl-C to stop)
./beans.sh status                # is it up?
./beans.sh stop                  # stop the background instance

# Cline — sticky config in ~/.cline/data/settings/providers.json
./run_cline.sh sk-task-12345 "fix the login bug"

# Copilot CLI — env vars per invocation, no sticky config
./run_copilot.sh sk-task-12345 "fix the login bug"

# Read spend back, either way
curl http://127.0.0.1:8000/usage/sk-task-12345
cat token_usage/sk-task-12345.json
```

That's the whole flow. Details, including the isolation trade-offs, are in
the sections below.

## Cline vs Copilot CLI at a glance

Both integrate with Beans Proxy, but they have meaningfully different
isolation models:

| | Cline ([run_cline.sh](run_cline.sh)) | Copilot CLI ([run_copilot.sh](run_copilot.sh)) |
|---|---|---|
| Mechanism | `cline auth --provider openai-compatible --baseurl …` persists in `~/.cline/data/settings/providers.json` | `COPILOT_PROVIDER_*` env vars read on every invocation |
| Stickiness | **Sticky.** Last `cline auth` wins; subsequent runs use the most recent config | **Not sticky.** Each invocation is fully isolated by its env vars |
| Parallel invocations with different pseudo keys | Need `CLINE_DATA_DIR=~/.cline-task-N` per process to avoid clobbering | Just set the env var per invocation; runs side-by-side cleanly |
| Reverting to non-proxy behavior | Re-run `cline auth` and pick your real provider, or delete `~/.cline` | Unset the env vars (or open a new terminal) |
| Default model assumed | `openai/gpt-4o-mini` (any OpenRouter model works) | `openai/gpt-4o-mini` (any OpenRouter model works) |
| Authentication with proxy | Pseudo key is the `apiKey` field in the persisted provider entry | Pseudo key is the `COPILOT_PROVIDER_API_KEY` env var |
| `Authorization: Bearer` sent to proxy | Yes (Cline sends Bearer + pseudo key) | Yes (Copilot sends Bearer + pseudo key) |

For one-off / interactive use, both work. If you want to run multiple
pseudo-keyed agents in parallel without cleanup, prefer **Copilot CLI** — its
env-var model is purpose-built for this. Cline is the right choice if you
already use it inside VS Code.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# edit .env to set BEANS_PROXY_TARGET_URL and BEANS_PROXY_TARGET_API_KEY
```

## Run

The proxy is managed by [beans.sh](beans.sh), which activates the local
`.venv`, reads `.env`, and talks to `python -m beans_proxy` for you.

```bash
./beans.sh                # start in the background (default)
./beans.sh --foreground   # run in the foreground (Ctrl-C to stop)
./beans.sh status         # is it up?
./beans.sh stop           # stop the background instance
./beans.sh restart        # stop, then start again
./beans.sh --help         # full usage
```

The default (background) mode writes logs to `beans_proxy.log` and a pid to
`.beans_proxy.pid`, then polls `GET /healthz` for up to 10 seconds to confirm
the server is up. If you'd rather skip the wrapper, the underlying command is:

```bash
. .venv/bin/activate
python -m beans_proxy
```

The proxy listens on `BEANS_PROXY_HOST:BEANS_PROXY_PORT` (defaults
`127.0.0.1:8000`).

## Configuration (env vars / `.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `BEANS_PROXY_TARGET_URL` | yes | — | Base URL of upstream LLM API. The caller's full path is appended to it. See "Path composition" below. |
| `BEANS_PROXY_TARGET_API_KEY` | yes | — | API key sent to upstream. |
| `BEANS_PROXY_HOST` | no | `127.0.0.1` | Bind address. |
| `BEANS_PROXY_PORT` | no | `8000` | Bind port. |
| `BEANS_PROXY_USAGE_DIR` | no | `token_usage` | Where per-key JSON files are stored. |
| `BEANS_PROXY_LOG_FILE` | no | `beans_proxy.log` | Log file path. |

### Path composition

The proxy appends the caller's path to `BEANS_PROXY_TARGET_URL`. To avoid
double-prefixes (e.g. `/api/v1/v1/chat/completions`), the proxy de-duplicates
the last path segment of the base URL when the caller's path starts with the
same segment:

| `BEANS_PROXY_TARGET_URL` | Caller path | Upstream path |
|---|---|---|
| `https://openrouter.ai/api/v1` | `/v1/chat/completions` | `/api/v1/chat/completions` |
| `https://api.example.com/v1` | `/v1/chat/completions` | `/v1/chat/completions` |
| `https://api.example.com` | `/v1/chat/completions` | `/v1/chat/completions` |
| `https://openrouter.ai/api` | `/v1/chat/completions` | `/api/v1/chat/completions` |

For OpenRouter, the recommended config is `BEANS_PROXY_TARGET_URL=https://openrouter.ai/api/v1` — it works correctly with the standard OpenAI-style caller path `/v1/chat/completions` with no double prefix.

## Endpoints

- `POST /v1/chat/completions` (and any other path the caller sends) — forwarded to upstream. Streaming requests are augmented with `stream_options.include_usage: true` so the final token counts are emitted. Always recorded.
- `GET /v1/models` (and other configured passthrough paths) — forwarded, not recorded.
- `GET /usage/{pseudo_key}` — returns the full recorded usage array for the key.
- `GET /healthz` — health check.

## Using with Cline

There are two ways: a one-shot shell wrapper ([run_cline.sh](run_cline.sh)) and
the manual Cline UI config. The wrapper is the recommended path because it
configures Cline for you and forwards your model/prompt through to the proxy.

### Quick start: the wrapper script

```bash
# from the beans_proxy repo, with the proxy running
./run_cline.sh sk-task-12345
```

This will:

1. Configure Cline's `openai-compatible` provider to point at
   `http://127.0.0.1:8000` with the pseudo key as the API key.
2. Launch `cline -P openai-compatible -m openai/gpt-4o-mini` (interactive TUI).

Variants:

```bash
# One-shot task (no interactive TUI)
./run_cline.sh sk-task-12345 "fix the login bug"

# Use a different model
./run_cline.sh sk-task-12345 anthropic/claude-3.5-sonnet "review this code"

# Plan mode
./run_cline.sh sk-task-12345 openai/gpt-4o-mini "design the migration" -p

# Headless JSON output
./run_cline.sh sk-task-12345 openai/gpt-4o-mini "list TODOs" --json
```

Positional args: `$1` is the pseudo key, `$2` (optional) is the model id,
everything else is forwarded to `cline`.

### Manual config (Cline UI)

If you'd rather click through the Cline settings panel:

- API Provider: **OpenAI Compatible**
- Base URL: `http://localhost:8000`
- API Key: `<pseudo-key from your orchestration framework, e.g. sk-task-12345>`
- Model ID: any OpenRouter model, e.g. `openai/gpt-4o-mini`

### ⚠️ The "sticky config" repercussion

Cline persists the provider config in
`~/.cline/data/settings/providers.json`. **Once you point Cline at the
proxy, that config stays in place.** This has a few consequences:

- **Subsequent `cline` invocations** (with or without the wrapper) will
  still route through the proxy until you reconfigure Cline. The pseudo
  key from your last invocation is what gets sent to the proxy.
- **To switch back to a non-proxy Cline**, re-run `cline auth` and pick
  your real provider from the TUI, or run `cline auth -p openrouter
  -k <your-openrouter-key> -m <model>` (or whatever your default was).
- **Multiple parallel Cline invocations with different pseudo keys are
  not isolated** — they all share `providers.json`, so the last `cline
  auth` wins. To run them in parallel without clobbering each other, give
  each one its own `CLINE_DATA_DIR`:

  ```bash
  CLINE_DATA_DIR=~/.cline-task-a ./run_cline.sh sk-task-a "do thing" &
  CLINE_DATA_DIR=~/.cline-task-b ./run_cline.sh sk-task-b "do thing" &
  ```

  Each isolated Cline has its own `providers.json` and session history.

## Using with Copilot CLI

The Copilot CLI supports a "Bring Your Own Model Provider" mode that lets you
point it at any OpenAI-compatible endpoint. We use that to route Copilot
through the proxy, where every call gets recorded against a pseudo key.

The env vars Copilot reads for this are **`COPILOT_PROVIDER_*`** — *not* the
generic `OPENAI_API_BASE` / `OPENAI_API_KEY`. This is what makes the proxy
work: Copilot hits `BEANS_PROXY_TARGET_URL` as if it were OpenAI, and Beans
Proxy swaps the auth header to your real OpenRouter key.

| Env var | What it does |
|---|---|
| `COPILOT_PROVIDER_BASE_URL` | The proxy URL, e.g. `http://127.0.0.1:8000` |
| `COPILOT_PROVIDER_TYPE` | `openai` (default) — works for any OpenAI-compatible endpoint |
| `COPILOT_PROVIDER_API_KEY` | The pseudo key (e.g. `sk-task-12345`); recorded against spend |
| `COPILOT_MODEL` | The model id to use, e.g. `openai/gpt-4o-mini` (required) |

### Quick start: the wrapper script

```bash
./run_copilot.sh sk-task-12345
```

The script sets the four env vars above and `exec`s `copilot` with any
remaining args:

```bash
# One-shot prompt
./run_copilot.sh sk-task-12345 "summarize this directory"

# Custom model
./run_copilot.sh sk-task-12345 anthropic/claude-3.5-sonnet "review this code"

# Plan mode
./run_copilot.sh sk-task-12345 openai/gpt-4o-mini "design the migration" -p

# Headless with JSON output
./run_copilot.sh sk-task-12345 openai/gpt-4o-mini "list TODOs" --json
```

Positional args: `$1` is the pseudo key, `$2` (optional) is the model id,
everything else is forwarded to `copilot`.

### Manual config (env vars)

If you'd rather set the env vars yourself:

```bash
export COPILOT_PROVIDER_BASE_URL="http://127.0.0.1:8000"
export COPILOT_PROVIDER_TYPE="openai"
export COPILOT_PROVIDER_API_KEY="sk-task-12345"
export COPILOT_MODEL="openai/gpt-4o-mini"

copilot
```

### Why this is the easy mode

Unlike Cline, the Copilot CLI **does not persist these settings** — it reads
the env vars on every invocation, so each call can use a different pseudo
key without any "sticky config" cleanup.

This means you can run multiple parallel Copilot CLI invocations and each
one will be cleanly attributed to its own pseudo key in
`token_usage/<key>.json`:

```bash
COPILOT_PROVIDER_API_KEY=sk-task-a ./run_copilot.sh sk-task-a "do task A" &
COPILOT_PROVIDER_API_KEY=sk-task-b ./run_copilot.sh sk-task-b "do task B" &
```

### Reverting to GitHub-hosted Copilot

Just unset the four `COPILOT_PROVIDER_*` env vars (and `COPILOT_MODEL`) in
the current shell, or open a fresh terminal. There's no global state to
clean up — Copilot falls back to its normal GitHub-authenticated behavior
automatically.

## Testing

```bash
. .venv/bin/activate
python -m pytest tests/ -v
```

There are 41 tests covering URL building, stream-options injection, usage
extraction (JSON and SSE), atomic concurrent writes, failure accounting, the
read endpoint, and end-to-end proxy behavior against a local mock upstream.

## File layout

```
beans_proxy/
  __init__.py
  __main__.py        # `python -m beans_proxy`
  app.py             # FastAPI routes
  config.py          # Pydantic settings / env loading
  forwarder.py       # upstream HTTP, stream options, usage extraction
  logging_setup.py
  usage.py           # atomic JSON persistence
tests/
  conftest.py        # in-process mock upstream
  test_app.py        # end-to-end
  test_forwarder.py
  test_usage.py
scripts/
  smoke.py           # live end-to-end smoke run
spec.md
.env.example
pyproject.toml
```

