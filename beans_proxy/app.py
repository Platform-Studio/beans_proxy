"""FastAPI application: HTTP routes that wire the proxy together."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .forwarder import ForwardResult, Forwarder
from .logging_setup import configure_logging, get_logger
from .usage import UsageStore, now_iso

# Pseudo-API keys can be arbitrary strings; we accept anything URL-safe enough
# to be in a path. We do NOT enforce a prefix or format.
_PSEUDO_KEY_PATTERN = re.compile(r"^[^/]+$")


def _pseudo_key_from_request(request: Request) -> str | None:
    """Extract the pseudo-API key from the Authorization header.

    Returns None if the header is missing or malformed (caller will get 401).
    Accepts both "Bearer X" and bare "X" for compatibility.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    if len(parts) == 1:
        return parts[0].strip() or None
    return None


def _filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop hop-by-hop and content-encoding headers from the upstream response."""
    drop = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _record_for_result(
    result: ForwardResult,
    started_at: str,
    ended_at: str,
) -> dict[str, Any]:
    """Build a usage record from a ForwardResult per the spec."""
    if result.usage is not None:
        record: dict[str, Any] = {
            "started_at": started_at,
            "ended_at": ended_at,
            "input_tokens": int(result.usage.get("input_tokens", 0)),
            "output_tokens": int(result.usage.get("output_tokens", 0)),
        }
    else:
        record = {
            "started_at": started_at,
            "ended_at": ended_at,
            "input_tokens": 0,
            "output_tokens": 0,
            "error": result.error or "unknown_error",
        }
    # Include the upstream model id when the response carried one. It's
    # metadata the user explicitly asked to record per-call.
    if result.model:
        record["model"] = result.model
    return record


def create_app(
    target_url: str,
    target_api_key: str,
    usage_dir: str | Path = "token_usage",
    log_file: str | Path = "beans_proxy.log",
    passthrough_prefixes: tuple[str, ...] = ("/v1/models",),
) -> FastAPI:
    """Create the FastAPI app.

    Parameters are passed in directly so tests can build a fully-isolated app
    without touching environment variables.
    """
    configure_logging(log_file)
    log = get_logger()

    store = UsageStore(usage_dir)
    forwarder = Forwarder(
        target_url=target_url,
        target_api_key=target_api_key,
        passthrough_prefixes=passthrough_prefixes,
        log=log,
    )

    app = FastAPI(title="Beans Proxy", version="0.1.0")

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        log.info("request %s %s", request.method, request.url.path)
        return await call_next(request)

    # Declare specific routes BEFORE the catch-all so they take precedence.
    @app.get("/usage/{pseudo_key}")
    async def get_usage(pseudo_key: str) -> Response:
        if not _PSEUDO_KEY_PATTERN.match(pseudo_key):
            return JSONResponse(
                {"detail": "invalid pseudo key"}, status_code=400
            )
        data = await store.read(pseudo_key)
        return JSONResponse({"pseudo_key": pseudo_key, "usage": data})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy_root(path: str, request: Request) -> Response:
        # The catch-all should never match /usage/* or /healthz, but if for any
        # reason it does, return 404 rather than forwarding an internal route.
        if request.url.path == "/healthz" or request.url.path.startswith("/usage/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        caller_path = request.url.path
        caller_query = request.url.query
        return await _handle_proxy(request, caller_path, caller_query, forwarder, store, log)

    return app


async def _handle_proxy(
    request: Request,
    caller_path: str,
    caller_query: str,
    forwarder: Forwarder,
    store: UsageStore,
    log: Any,
) -> Response:
    # Auth: extract pseudo key, but do not validate format (per spec).
    pseudo_key = _pseudo_key_from_request(request)
    if not pseudo_key:
        return JSONResponse(
            {"detail": "missing or invalid Authorization header"},
            status_code=401,
        )

    body = await request.body()

    # Passthrough for non-billable endpoints: forward, but don't record.
    if forwarder.should_passthrough(caller_path):
        result = await forwarder.forward(
            request.method, caller_path, caller_query, dict(request.headers), body
        )
        return _build_passthrough_response(result)

    started_at = now_iso()
    result = await forwarder.forward(
        request.method, caller_path, caller_query, dict(request.headers), body
    )
    ended_at = now_iso()

    # Always record, even on failure (per spec).
    record = _record_for_result(result, started_at, ended_at)
    await store.append(pseudo_key, record)

    log.info(
        "recorded pseudo_key=%s in=%d out=%d error=%s",
        pseudo_key,
        record["input_tokens"],
        record["output_tokens"],
        record.get("error"),
    )

    # For streaming responses, replay the SSE body verbatim.
    if result.is_stream or "text/event-stream" in result.content_type.lower():
        return Response(
            content=result.body,
            status_code=result.status_code,
            media_type="text/event-stream",
            headers=_filter_response_headers(result.headers),
        )

    # Non-streaming JSON: pass the upstream body back. We also try to surface
    # a useful error message in the JSON body if the upstream gave us one.
    return Response(
        content=result.body,
        status_code=result.status_code,
        media_type=result.content_type,
        headers=_filter_response_headers(result.headers),
    )


def _build_passthrough_response(result: ForwardResult) -> Response:
    if result.is_stream or "text/event-stream" in result.content_type.lower():
        return Response(
            content=result.body,
            status_code=result.status_code,
            media_type="text/event-stream",
            headers=_filter_response_headers(result.headers),
        )
    return Response(
        content=result.body,
        status_code=result.status_code,
        media_type=result.content_type,
        headers=_filter_response_headers(result.headers),
    )


def build_app_from_settings(
    target_url: str,
    target_api_key: str,
    usage_dir: str | Path,
    log_file: str | Path,
    passthrough_prefixes: tuple[str, ...] = ("/v1/models",),
) -> FastAPI:
    """Helper that builds the app, used by the CLI entrypoint."""
    return create_app(
        target_url=target_url,
        target_api_key=target_api_key,
        usage_dir=usage_dir,
        log_file=log_file,
        passthrough_prefixes=passthrough_prefixes,
    )


# ---------------------------------------------------------------------------
# Convenience: format an ISO timestamp in UTC. Re-exported for tests/CLI.
# ---------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
