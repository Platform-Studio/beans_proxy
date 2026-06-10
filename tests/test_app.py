"""End-to-end tests for the FastAPI app via httpx.AsyncClient + ASGITransport.

We mock the upstream LLM API using a local socket server (the `upstream`
fixture). The proxy is configured to point at that local server."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from beans_proxy.app import create_app


def _build_client(app):
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_non_streaming_chat_completion_records_usage(upstream, tmp_usage_dir, tmp_log_file):
    def handler(req):
        body = json.loads(req["body"])
        return 200, {"content-type": "application/json"}, json.dumps(
            {
                "id": "chatcmpl-x",
                "model": body.get("model", "x"),
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 34},
            }
        ).encode("utf-8")

    upstream.set_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-task-1"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "hi"

    # Upstream received the request with the upstream key, not the caller's
    assert upstream.requests, "upstream received no request"
    sent = upstream.requests[-1]
    assert sent["headers"]["authorization"] == "Bearer upstream-key"
    assert sent["path"] == "/api/v1/chat/completions"

    # Usage was recorded
    usage_path = Path(tmp_usage_dir) / "sk-task-1.json"
    assert usage_path.exists()
    data = json.loads(usage_path.read_text())
    assert len(data) == 1
    assert data[0]["input_tokens"] == 12
    assert data[0]["output_tokens"] == 34
    assert "error" not in data[0]


async def test_streaming_chat_completion_injects_stream_options_and_records(upstream, tmp_usage_dir, tmp_log_file):
    """Stream requests should have stream_options injected, and the proxy
    should extract usage from the final SSE chunk."""
    def handler(req):
        body = json.loads(req["body"])
        assert body.get("stream_options") == {"include_usage": True}, body
        sse = (
            b'data: {"id":"1","object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"id":"1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":9}}\n\n'
            b'data: [DONE]\n\n'
        )
        return 200, {"content-type": "text/event-stream"}, sse

    upstream.set_sse_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-task-2"},
            json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        body = r.text
        assert "data: [DONE]" in body

    usage_path = Path(tmp_usage_dir) / "sk-task-2.json"
    data = json.loads(usage_path.read_text())
    assert data[0]["input_tokens"] == 7
    assert data[0]["output_tokens"] == 9


async def test_passthrough_endpoint_does_not_record(upstream, tmp_usage_dir, tmp_log_file):
    def handler(req):
        return 200, {"content-type": "application/json"}, json.dumps(
            {"data": [{"id": "model-a"}]}
        ).encode("utf-8")

    upstream.set_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-task-3"},
        )
        assert r.status_code == 200
        assert r.json() == {"data": [{"id": "model-a"}]}

    usage_path = Path(tmp_usage_dir) / "sk-task-3.json"
    assert not usage_path.exists(), f"unexpected file at {usage_path}"


async def test_upstream_5xx_without_usage_writes_failure_record(upstream, tmp_usage_dir, tmp_log_file):
    def handler(req):
        return 500, {"content-type": "application/json"}, json.dumps(
            {"error": {"message": "boom"}}
        ).encode("utf-8")

    upstream.set_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-task-4"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 500
        assert r.json() == {"error": {"message": "boom"}}

    usage_path = Path(tmp_usage_dir) / "sk-task-4.json"
    data = json.loads(usage_path.read_text())
    assert data[0]["error"] == "upstream_5xx"
    assert data[0]["input_tokens"] == 0
    assert data[0]["output_tokens"] == 0


async def test_upstream_5xx_with_usage_writes_record_no_error(upstream, tmp_usage_dir, tmp_log_file):
    def handler(req):
        return 429, {"content-type": "application/json"}, json.dumps(
            {"error": {"message": "rate limited"}, "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
        ).encode("utf-8")

    upstream.set_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-task-5"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 429

    usage_path = Path(tmp_usage_dir) / "sk-task-5.json"
    data = json.loads(usage_path.read_text())
    assert data[0]["input_tokens"] == 3
    assert data[0]["output_tokens"] == 4
    assert "error" not in data[0]


async def test_get_usage_endpoint_returns_recorded_data(upstream, tmp_usage_dir, tmp_log_file):
    def handler(req):
        return 200, {"content-type": "application/json"}, json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        ).encode("utf-8")

    upstream.set_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-task-6"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        r = await client.get("/usage/sk-task-6")
        assert r.status_code == 200
        body = r.json()
        assert body["pseudo_key"] == "sk-task-6"
        assert len(body["usage"]) == 1
        assert body["usage"][0]["input_tokens"] == 1


async def test_get_usage_endpoint_missing_key_returns_empty_array(upstream, tmp_usage_dir, tmp_log_file):
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )
    async with _build_client(app) as client:
        r = await client.get("/usage/never-seen")
        assert r.status_code == 200
        assert r.json() == {"pseudo_key": "never-seen", "usage": []}


async def test_missing_authorization_returns_401(upstream, tmp_usage_dir, tmp_log_file):
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )
    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": []},
        )
        assert r.status_code == 401


async def test_arbitrary_pseudo_key_no_prefix_required(upstream, tmp_usage_dir, tmp_log_file):
    """Spec: pseudo keys are arbitrary strings; no prefix check."""
    def handler(req):
        return 200, {"content-type": "application/json"}, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        ).encode("utf-8")

    upstream.set_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer not-sk-prefixed-123"},
            json={"model": "x", "messages": []},
        )
        assert r.status_code == 200

    files = list(Path(tmp_usage_dir).glob("*.json"))
    assert any("not-sk-prefixed" in p.name for p in files), files


async def test_gzipped_upstream_response_is_decompressed_and_recorded(
    upstream, tmp_usage_dir, tmp_log_file, gzip_factory
):
    """Upstream may return content-encoding: gzip. The proxy must decompress
    the body before parsing for usage and before sending it back to the caller."""
    def handler(req):
        return 200, {"content-type": "application/json"}, json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            }
        ).encode("utf-8")

    upstream.set_response(gzip_factory(handler))
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-gzip"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        # Body sent back to caller should be plaintext, not gzipped.
        assert r.headers.get("content-encoding") is None
        assert r.json()["choices"][0]["message"]["content"] == "ok"

    usage_path = Path(tmp_usage_dir) / "sk-gzip.json"
    data = json.loads(usage_path.read_text())
    assert data[0]["input_tokens"] == 3
    assert data[0]["output_tokens"] == 4


async def test_model_is_recorded_for_non_streaming_response(
    upstream, tmp_usage_dir, tmp_log_file
):
    def handler(req):
        return 200, {"content-type": "application/json"}, json.dumps(
            {
                "id": "x",
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        ).encode("utf-8")

    upstream.set_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-model-ns"},
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200

    data = json.loads((Path(tmp_usage_dir) / "sk-model-ns.json").read_text())
    assert data[0]["model"] == "openai/gpt-4o-mini"
    assert data[0]["input_tokens"] == 1


async def test_model_is_recorded_for_streaming_response(
    upstream, tmp_usage_dir, tmp_log_file
):
    def handler(req):
        sse = (
            b'data: {"id":"1","model":"anthropic/claude-3.5-sonnet","choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"id":"1","model":"anthropic/claude-3.5-sonnet","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7}}\n\n'
            b'data: [DONE]\n\n'
        )
        return 200, {"content-type": "text/event-stream"}, sse

    upstream.set_sse_response(handler)
    app = create_app(
        target_url=f"http://127.0.0.1:{upstream.port}/api/v1",
        target_api_key="upstream-key",
        usage_dir=tmp_usage_dir,
        log_file=tmp_log_file,
    )

    async with _build_client(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-model-s"},
            json={"model": "anthropic/claude-3.5-sonnet", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200

    data = json.loads((Path(tmp_usage_dir) / "sk-model-s.json").read_text())
    assert data[0]["model"] == "anthropic/claude-3.5-sonnet"
    assert data[0]["input_tokens"] == 5
    assert data[0]["output_tokens"] == 7
