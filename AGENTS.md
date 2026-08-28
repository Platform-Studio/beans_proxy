# Beans Proxy Agent Guide

## Project

Beans Proxy is a small FastAPI proxy for OpenAI-compatible LLM clients. It replaces the caller's pseudo-API key with the configured upstream key, forwards requests, and records token usage and cost per pseudo key in `token_usage/<key>.json`.

The primary runtime path is:

`client -> Beans Proxy -> configured upstream (typically OpenRouter)`

## Setup and Commands

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```

Run the proxy through `./beans.sh`:

```bash
./beans.sh --foreground
./beans.sh start
./beans.sh stop
./beans.sh restart
./beans.sh status
```

The proxy reads configuration from `.env` or environment variables using the `BEANS_PROXY_` prefix. Required settings are `BEANS_PROXY_TARGET_URL` and `BEANS_PROXY_TARGET_API_KEY`. Common optional settings are `BEANS_PROXY_HOST`, `BEANS_PROXY_PORT`, `BEANS_PROXY_USAGE_DIR`, and `BEANS_PROXY_LOG_FILE`.

## Architecture

- `beans_proxy/app.py`: FastAPI routes, authentication, usage record assembly, and cost calculation.
- `beans_proxy/forwarder.py`: upstream URL construction, authorization replacement, streaming usage injection, response decompression, and usage/model extraction.
- `beans_proxy/pricing.py`: startup-loaded OpenRouter pricing catalog.
- `beans_proxy/usage.py`: per-key JSON persistence with per-key locks and atomic writes.
- `beans_proxy/config.py`: Pydantic settings and `.env` loading.
- `beans_proxy/logging_setup.py`: rotating file and console logging.
- `tests/`: pytest and end-to-end tests using a local socket-based mock upstream.

## Pricing

Pricing is always fetched from the fixed URL `https://openrouter.ai/api/v1/models`, independently of `BEANS_PROXY_TARGET_URL`.

At startup, parse the response once and keep only model IDs/slugs and base prompt/completion prices. Index both `id` and `canonical_slug`; store prices as `Decimal`. Ignore other model metadata and tiered `overrides`. Treat negative pricing such as `-1` as unavailable. A failed pricing fetch warns but must not prevent the proxy from starting.

For each call, prefer the resolved model from the upstream response and fall back to the request body's `model`. A known price produces USD input, output, and total cost fields. An unknown or unavailable price omits those fields and adds `error: "unknown model/no price"`.

## Request Behavior

- The caller's `Authorization` header is replaced with `Bearer <BEANS_PROXY_TARGET_API_KEY>`.
- Configured passthrough paths, currently `/v1/models`, are forwarded without usage recording.
- Streaming requests get `stream_options.include_usage: true` when needed so the final usage chunk can be recorded.
- Non-streaming request bodies are otherwise forwarded unchanged. Do not add request transformations without tests.
- Upstream failures are recorded. If no usage is available, token counts are zero and a short error tag is written.

## Testing Guidance

Run the full suite with `python -m pytest -q`. Add or update focused tests in the relevant module under `tests/` for behavior changes, especially forwarding, streaming extraction, pricing, persistence, and failure accounting.

Do not commit runtime artifacts such as `*.log`, `*.log.*`, `token_usage/`, or `.beans_proxy.pid`.

## File Layout

```text
beans_proxy/
  __init__.py
  __main__.py
  app.py
  config.py
  forwarder.py
  logging_setup.py
  pricing.py
  usage.py
tests/
  conftest.py
  test_app.py
  test_forwarder.py
  test_pricing.py
  test_usage.py
scripts/
  smoke.py
spec.md
README.md
pyproject.toml
```
