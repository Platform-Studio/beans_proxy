"""HTTP forwarder: relays requests to the upstream LLM API.

Responsibilities:
- Build the upstream URL by appending the caller's path/query to the configured base.
- Swap the Authorization header for the upstream API key.
- Inject `stream_options.include_usage` into streaming request bodies.
- For streaming: forward SSE chunks, extract the final `usage` from the terminal
  chunk, then return it.
- For non-streaming: forward the JSON body, extract `usage` from the response.
- Detect a failure (network error, non-2xx, timeout) and return a structured
  failure result so the caller can record a failure entry per the spec.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import zlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


@dataclass
class ForwardResult:
    """Outcome of forwarding a request to the upstream API.

    On success: `error is None`, `usage` populated if present in the response.
    On failure: `error` is a short string tag (e.g. "upstream_5xx"), `usage` may
    still be set if the upstream body included a `usage` field.
    """

    status_code: int
    usage: dict[str, int] | None = None
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    content_type: str = "application/json"
    is_stream: bool = False
    model: str | None = None

    @property
    def is_success(self) -> bool:
        return self.error is None


def _normalize_base_for_caller(base: str, caller_path: str) -> str:
    """Drop a redundant path segment from the base URL.

    If the base's path ends with a single non-root segment (e.g. ``/v1``) and
    the caller's path starts with the same segment, strip that segment from
    the base. This lets users set ``BEANS_PROXY_TARGET_URL`` to
    ``https://openrouter.ai/api/v1`` while callers send ``/v1/chat/completions``
    without producing ``/api/v1/v1/chat/completions``.

    Rules:
    - Only triggers when the base path has exactly one trailing segment
      beyond any leading segments (e.g. ``/api/v1`` -> strip ``v1``;
      ``/v1`` -> strip ``v1``; ``/`` -> no change).
    - The caller path must start with ``/<segment>`` or be exactly
      ``/<segment>`` for the strip to apply.
    - Multi-segment base paths like ``/api/v2/extra`` are not stripped.
    """
    base_parts = urlsplit(base)
    base_path = base_parts.path.rstrip("/")
    if not base_path:
        return base

    # Don't try to strip from multi-segment paths like /api/v1 — we only
    # handle the case where the base is /<one-segment> or ends with a single
    # segment that the caller is also providing.
    segments = [s for s in base_path.split("/") if s]
    if not segments:
        return base
    last = segments[-1]
    # Restrict to a single trailing segment only; multi-segment bases are
    # left alone since we can't safely guess which part overlaps.
    if len(segments) != 1:
        # Multi-segment base: if the *last* segment matches the caller's
        # first segment, strip the last segment. This handles
        # "https://openrouter.ai/api/v1" with caller "/v1/...".
        caller = caller_path or ""
        if not caller.startswith("/"):
            caller = "/" + caller
        first_caller_segment = caller.lstrip("/").split("/", 1)[0]
        if not first_caller_segment or first_caller_segment != last:
            return base
        new_segments = segments[:-1]
        new_base_path = "/" + "/".join(new_segments) if new_segments else ""
        return urlunsplit(
            (base_parts.scheme, base_parts.netloc, new_base_path, "", "")
        )

    # Single-segment base: strip it if the caller also starts with it.
    caller = caller_path or ""
    if not caller.startswith("/"):
        caller = "/" + caller
    first_caller_segment = caller.lstrip("/").split("/", 1)[0]
    if first_caller_segment != last:
        return base
    return urlunsplit((base_parts.scheme, base_parts.netloc, "", "", ""))


def build_upstream_url(base: str, caller_path: str, caller_query: str) -> str:
    """Append the caller's path/query to the configured base URL.

    - If `caller_path` is empty or just "/", it normalizes to the base's path
      (typically "/").
    - Preserves the caller's query string verbatim.
    - If the base's last path segment duplicates the caller's first segment
      (e.g. base ``/api/v1``, caller ``/v1/...``), the duplicate is dropped
      from the base to avoid ``/api/v1/v1/...``.
    """
    base_parts = urlsplit(base)
    base = _normalize_base_for_caller(base, caller_path)
    base_parts = urlsplit(base)
    # Strip any trailing slash from the base path; the caller's path is appended.
    base_path = base_parts.path.rstrip("/")
    caller = caller_path or ""
    if not caller.startswith("/"):
        caller = "/" + caller
    new_path = base_path + caller
    return urlunsplit(
        (base_parts.scheme, base_parts.netloc, new_path, caller_query, "")
    )


def is_passthrough_path(path: str, passthrough_prefixes: tuple[str, ...]) -> bool:
    """Return True if the caller's path is one we should pass through untracked."""
    for prefix in passthrough_prefixes:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return True
        if path == prefix.rstrip("/"):
            return True
    return False


def inject_stream_options(body: bytes) -> bytes:
    """Inject `stream_options: {include_usage: true}` into a JSON body.

    If the body is not valid JSON, return it unchanged (caller's responsibility
    to surface that as a failure). We only inject when `stream: true` and the
    caller did not already set `stream_options`.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(data, dict):
        return body
    if not data.get("stream"):
        return body
    if "stream_options" not in data or not isinstance(data.get("stream_options"), dict):
        data["stream_options"] = {"include_usage": True}
    else:
        data["stream_options"].setdefault("include_usage", True)
    return json.dumps(data).encode("utf-8")


def normalize_custom_tools(body: bytes, caller_path: str) -> tuple[bytes, list[str]]:
    """Apply the temporary Copilot custom-tool workaround when narrowly applicable.

    TODO: Remove this compatibility rewrite when Copilot CLI emits the standard
    OpenAI chat-completions custom-tool shape. Copilot currently nests the
    built-in ``apply_patch`` definition under ``custom`` and its grammar fields
    under ``format.grammar``; some OpenAI-compatible providers reject that shape.
    """
    if not body:
        return body, []
    normalized_path = "/" + caller_path.strip("/")
    if normalized_path not in {"/chat/completions", "/v1/chat/completions"}:
        return body, []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body, []
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        return body, []

    model = data.get("model")
    if not isinstance(model, str):
        return body, []
    model_parts = model.lower().split("/")
    if len(model_parts) > 1 and model_parts[0] != "openai":
        return body, []
    model_name = model_parts[-1]
    openai_model = model_name.startswith("gpt-") or model_name == "codex"
    openai_model = openai_model or model_name.startswith(("codex-", "o1-", "o3-", "o4-"))
    openai_model = openai_model or model_name in {"o1", "o3", "o4"}
    if not openai_model:
        return body, []

    rewritten_tools: list[str] = []
    for tool in data["tools"]:
        if not isinstance(tool, dict) or tool.get("type") != "custom":
            continue
        custom = tool.get("custom")
        if not isinstance(custom, dict) or custom.get("name") != "apply_patch":
            continue
        if any(key in tool for key in ("name", "description", "format")):
            continue
        custom_format = custom.get("format")
        if not isinstance(custom_format, dict) or custom_format.get("type") != "grammar":
            continue
        grammar = custom_format.get("grammar")
        if not isinstance(grammar, dict):
            continue
        if not isinstance(grammar.get("syntax"), str) or not isinstance(
            grammar.get("definition"), str
        ):
            continue
        if any(key in custom_format for key in ("syntax", "definition")):
            continue

        flattened_format = {
            key: value for key, value in custom_format.items() if key != "grammar"
        }
        flattened_format.update(grammar)
        tool.pop("custom")
        tool.update({key: value for key, value in custom.items() if key != "format"})
        tool["format"] = flattened_format
        rewritten_tools.append("apply_patch")

    if not rewritten_tools:
        return body, []
    return json.dumps(data).encode("utf-8"), rewritten_tools


def extract_usage_from_json(body: bytes) -> dict[str, int] | None:
    """Extract a `usage` dict from a non-streaming OpenAI-style response body."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    return _coerce_usage(usage)


def extract_usage_from_sse(body: bytes) -> dict[str, int] | None:
    """Extract `usage` from a streamed SSE response body.

    OpenRouter/OpenAI streaming emits chunks like:
        data: {"id":"...","object":"chat.completion.chunk", ... "usage": null}
        ...
        data: {"id":"...","object":"chat.completion.chunk","choices":[], "usage": {...}}
        data: [DONE]

    The final usage-bearing chunk has an empty `choices` array and a non-null
    `usage`. We scan the body for the last `data: ` line that parses and has a
    non-null `usage`.
    """
    if not body:
        return None
    last: dict[str, int] | None = None
    for raw_line in body.splitlines():
        line = raw_line.decode("utf-8", errors="ignore").strip() if isinstance(raw_line, bytes) else raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage:
            coerced = _coerce_usage(usage)
            if coerced:
                last = coerced
    return last


def extract_model_from_json(body: bytes) -> str | None:
    """Extract the `model` field from a non-streaming OpenAI-style response."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    model = data.get("model")
    if isinstance(model, str) and model:
        return model
    return None


def extract_model_from_sse(body: bytes) -> str | None:
    """Extract the `model` field from a streamed OpenAI-style SSE response.

    The model id is the same on every chunk of a given response, so we return
    the first one we see.
    """
    if not body:
        return None
    for raw_line in body.splitlines():
        line = raw_line.decode("utf-8", errors="ignore").strip() if isinstance(raw_line, bytes) else raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        model = obj.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def extract_model_from_json(body: bytes) -> str | None:
    """Extract the `model` field from a non-streaming OpenAI-style response."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    model = data.get("model")
    if isinstance(model, str) and model:
        return model
    return None


def extract_model_from_sse(body: bytes) -> str | None:
    """Extract the `model` field from a streamed OpenAI-style SSE response.

    The model id is the same on every chunk of a given response, so we return
    the first one we see.
    """
    if not body:
        return None
    for raw_line in body.splitlines():
        line = raw_line.decode("utf-8", errors="ignore").strip() if isinstance(raw_line, bytes) else raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        model = obj.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _coerce_usage(usage: dict[str, Any]) -> dict[str, int] | None:
    """Pull out the integer token fields we care about, if present."""
    in_t = usage.get("prompt_tokens", usage.get("input_tokens"))
    out_t = usage.get("completion_tokens", usage.get("output_tokens"))
    if in_t is None and out_t is None:
        return None
    try:
        return {
            "input_tokens": int(in_t) if in_t is not None else 0,
            "output_tokens": int(out_t) if out_t is not None else 0,
        }
    except (TypeError, ValueError):
        return None


def decompress_body(body: bytes, content_encoding: str | None) -> bytes:
    """Decompress an upstream response body based on its Content-Encoding.

    Returns the body unchanged if no recognized encoding is present.
    Handles 'gzip', 'br' (best-effort, requires `brotli` to be installed
    — falls back to the raw body), and 'deflate' (raw deflate and zlib).
    """
    if not body or not content_encoding:
        return body
    encoding = content_encoding.strip().lower().split(",")[0].strip()
    if encoding == "gzip":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError):
            return body
    if encoding == "deflate":
        # Per RFC 7230, 'deflate' historically meant zlib-wrapped; some
        # servers send raw deflate. Try zlib first, then raw.
        try:
            return zlib.decompress(body)
        except zlib.error:
            try:
                return zlib.decompress(body, -15)
            except zlib.error:
                return body
    if encoding == "br":
        try:
            import brotli  # type: ignore[import-not-found]
            return brotli.decompress(body)
        except Exception:
            return body
    if encoding == "zstd":
        try:
            import zstandard  # type: ignore[import-not-found]
            return zstandard.ZstdDecompressor().decompress(body)
        except Exception:
            return body
    return body


@dataclass
class Forwarder:
    """Forwards HTTP requests to the upstream LLM API."""

    target_url: str
    target_api_key: str
    passthrough_prefixes: tuple[str, ...] = ("/v1/models",)
    timeout: float = 300.0
    log: logging.Logger | None = None

    def __post_init__(self) -> None:
        if self.log is None:
            self.log = logging.getLogger("beans_proxy.forwarder")

    def upstream_url_for(self, caller_path: str, caller_query: str) -> str:
        return build_upstream_url(self.target_url, caller_path, caller_query)

    def should_passthrough(self, caller_path: str) -> bool:
        return is_passthrough_path(caller_path, self.passthrough_prefixes)

    async def forward(
        self,
        method: str,
        caller_path: str,
        caller_query: str,
        headers: dict[str, str],
        body: bytes,
    ) -> ForwardResult:
        """Forward an HTTP request to the upstream and return a ForwardResult."""
        url = self.upstream_url_for(caller_path, caller_query)
        body, rewritten_tools = normalize_custom_tools(body, caller_path)
        if rewritten_tools:
            self.log.warning(
                "temporary Copilot compatibility workaround rewrote custom tool(s) "
                "before forwarding: path=%s tools=%s",
                caller_path,
                ",".join(rewritten_tools),
            )
        is_stream = self._body_wants_stream(body)
        if is_stream:
            body = inject_stream_options(body)

        # Strip the caller's Authorization; we always send the upstream key.
        fwd_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        fwd_headers["Authorization"] = f"Bearer {self.target_api_key}"
        # Don't propagate hop-by-hop / content-length headers from the client.
        for h in ("content-length", "host", "connection", "accept-encoding"):
            fwd_headers.pop(h, None)

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Stream so we can pass the response back to the caller as-is.
                req = client.build_request(
                    method, url, headers=fwd_headers, content=body
                )
                response = await client.send(req, stream=True)
                # Read the full body. For streaming responses this means we buffer
                # the SSE — acceptable for a single-call proxy.
                chunks: list[bytes] = []
                async for chunk in response.aiter_raw():
                    chunks.append(chunk)
                raw_body = b"".join(chunks)
                await response.aclose()
                elapsed = time.monotonic() - started

            content_type = response.headers.get("content-type", "application/json")
            content_encoding = response.headers.get("content-encoding")
            upstream_status = response.status_code

            # We always decompress before parsing for usage, and before
            # returning the body to the caller, so downstream consumers (and
            # our own extractor) see plaintext bytes. Drop Content-Encoding
            # from the headers we forward back.
            resp_body = decompress_body(raw_body, content_encoding)
            fwd_response_headers = {
                k: v for k, v in response.headers.items() if k.lower() != "content-encoding"
            }

            usage = None
            model = None
            if is_stream or "text/event-stream" in content_type.lower():
                usage = extract_usage_from_sse(resp_body)
                model = extract_model_from_sse(resp_body)
            else:
                usage = extract_usage_from_json(resp_body)
                model = extract_model_from_json(resp_body)

            if upstream_status >= 400:
                err_tag = f"upstream_{upstream_status // 100}xx"
                # Per spec, if usage is present, record it without an error tag.
                error_preview = resp_body[:2000].decode("utf-8", errors="replace")
                self.log.info(
                    "upstream returned %d in %.2fs (usage=%s body=%s)",
                    upstream_status,
                    elapsed,
                    usage,
                    error_preview,
                )
                return ForwardResult(
                    status_code=upstream_status,
                    usage=usage,
                    error=None if usage else err_tag,
                    headers=fwd_response_headers,
                    body=resp_body,
                    content_type=content_type,
                    is_stream=is_stream,
                    model=model,
                )

            self.log.info(
                "upstream %s %s -> %d in %.2fs usage=%s model=%s",
                method,
                caller_path,
                upstream_status,
                elapsed,
                usage,
                model,
            )
            return ForwardResult(
                status_code=upstream_status,
                usage=usage,
                headers=fwd_response_headers,
                body=resp_body,
                content_type=content_type,
                is_stream=is_stream,
                model=model,
            )
        except httpx.TimeoutException as exc:
            self.log.warning("upstream timeout: %s", exc)
            return ForwardResult(
                status_code=504, error="upstream_timeout", is_stream=is_stream
            )
        except httpx.HTTPError as exc:
            self.log.warning("upstream connection error: %s", exc)
            return ForwardResult(
                status_code=502, error="connection_error", is_stream=is_stream
            )

    @staticmethod
    def _body_wants_stream(body: bytes) -> bool:
        if not body:
            return False
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return False
        if isinstance(data, dict):
            return bool(data.get("stream"))
        return False
