"""Tests for the forwarder module: URL building, stream options, usage extraction."""

from __future__ import annotations

import pytest

from beans_proxy.forwarder import (
    Forwarder,
    _normalize_base_for_caller,
    build_upstream_url,
    extract_model_from_json,
    extract_model_from_sse,
    extract_usage_from_json,
    extract_usage_from_sse,
    inject_stream_options,
    is_passthrough_path,
)


def test_build_upstream_url_appends_path():
    assert (
        build_upstream_url("https://api.example.com/v1", "/chat/completions", "")
        == "https://api.example.com/v1/chat/completions"
    )


def test_build_upstream_url_preserves_query():
    assert (
        build_upstream_url("https://api.example.com/v1", "/chat/completions", "a=1&b=2")
        == "https://api.example.com/v1/chat/completions?a=1&b=2"
    )


def test_build_upstream_url_handles_trailing_slash():
    assert (
        build_upstream_url("https://api.example.com/v1/", "/chat/completions", "")
        == "https://api.example.com/v1/chat/completions"
    )


def test_build_upstream_url_handles_empty_caller_path():
    assert build_upstream_url("https://api.example.com/v1", "", "") == "https://api.example.com/v1/"


def test_normalize_strips_duplicate_v1_when_base_ends_with_v1():
    # /api/v1 base + /v1 caller -> /api
    assert (
        _normalize_base_for_caller("https://openrouter.ai/api/v1", "/v1/chat/completions")
        == "https://openrouter.ai/api"
    )


def test_normalize_strips_duplicate_segment_when_base_is_single_segment():
    # /v1 base + /v1 caller -> root
    assert (
        _normalize_base_for_caller("https://api.example.com/v1", "/v1/chat/completions")
        == "https://api.example.com"
    )


def test_normalize_no_op_when_segments_differ():
    # Base ends with /v1, caller starts with /v2 -> no change
    assert (
        _normalize_base_for_caller("https://api.example.com/v1", "/v2/chat/completions")
        == "https://api.example.com/v1"
    )


def test_normalize_no_op_when_base_is_root():
    assert (
        _normalize_base_for_caller("https://api.example.com", "/v1/chat/completions")
        == "https://api.example.com"
    )


def test_build_upstream_url_dedupes_v1_segment():
    # /api/v1 + /v1/chat/completions -> /api/v1/chat/completions (no /v1/v1)
    assert (
        build_upstream_url("https://openrouter.ai/api/v1", "/v1/chat/completions", "")
        == "https://openrouter.ai/api/v1/chat/completions"
    )


def test_build_upstream_url_dedupes_preserves_query():
    assert (
        build_upstream_url("https://openrouter.ai/api/v1", "/v1/chat/completions", "x=1")
        == "https://openrouter.ai/api/v1/chat/completions?x=1"
    )


def test_inject_stream_options_adds_when_streaming():
    body = b'{"model":"x","stream":true}'
    out = inject_stream_options(body)
    import json as _json
    parsed = _json.loads(out)
    assert parsed["stream_options"] == {"include_usage": True}


def test_inject_stream_options_preserves_existing_stream_options():
    body = b'{"model":"x","stream":true,"stream_options":{"include_usage":false}}'
    out = inject_stream_options(body)
    import json as _json
    parsed = _json.loads(out)
    # Per OpenAI spec we just ensure the key is present; we shouldn't blindly
    # override an explicit caller value. Setdefault semantics give us that.
    assert parsed["stream_options"]["include_usage"] is False


def test_inject_stream_options_noop_when_not_streaming():
    body = b'{"model":"x"}'
    assert inject_stream_options(body) == body


def test_inject_stream_options_noop_on_invalid_json():
    body = b"not json"
    assert inject_stream_options(body) == body


def test_extract_usage_from_json_prompt_completion():
    body = b'{"id":"x","usage":{"prompt_tokens":11,"completion_tokens":22}}'
    assert extract_usage_from_json(body) == {"input_tokens": 11, "output_tokens": 22}


def test_extract_usage_from_json_input_output_aliases():
    body = b'{"usage":{"input_tokens":1,"output_tokens":2}}'
    assert extract_usage_from_json(body) == {"input_tokens": 1, "output_tokens": 2}


def test_extract_usage_from_json_missing():
    assert extract_usage_from_json(b'{"id":"x"}') is None


def test_extract_usage_from_json_invalid():
    assert extract_usage_from_json(b"not json") is None


def test_extract_usage_from_sse_uses_last_usage_chunk():
    body = (
        b'data: {"id":"1","choices":[]}\n\n'
        b'data: {"id":"2","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7}}\n\n'
        b'data: [DONE]\n\n'
    )
    assert extract_usage_from_sse(body) == {"input_tokens": 5, "output_tokens": 7}


def test_extract_usage_from_sse_no_usage_returns_none():
    body = b'data: {"id":"1","choices":[]}\n\ndata: [DONE]\n\n'
    assert extract_usage_from_sse(body) is None


def test_extract_usage_from_sse_empty_body():
    assert extract_usage_from_sse(b"") is None


def test_is_passthrough_path_exact_match():
    assert is_passthrough_path("/v1/models", ("/v1/models",)) is True


def test_is_passthrough_path_subpath():
    assert is_passthrough_path("/v1/models/something", ("/v1/models",)) is True


def test_is_passthrough_path_negative():
    assert is_passthrough_path("/v1/chat/completions", ("/v1/models",)) is False


def test_extract_model_from_json_present():
    body = b'{"id":"x","model":"openai/gpt-4o-mini","choices":[]}'
    assert extract_model_from_json(body) == "openai/gpt-4o-mini"


def test_extract_model_from_json_missing():
    assert extract_model_from_json(b'{"id":"x","choices":[]}') is None


def test_extract_model_from_json_invalid():
    assert extract_model_from_json(b"not json") is None


def test_extract_model_from_json_empty():
    assert extract_model_from_json(b"") is None


def test_extract_model_from_sse_first_chunk():
    body = (
        b'data: {"id":"1","model":"anthropic/claude-3.5-sonnet","choices":[]}\n\n'
        b'data: {"id":"1","model":"anthropic/claude-3.5-sonnet","choices":[],"usage":{}}\n\n'
        b'data: [DONE]\n\n'
    )
    assert extract_model_from_sse(body) == "anthropic/claude-3.5-sonnet"


def test_extract_model_from_sse_missing():
    body = b'data: {"id":"1","choices":[]}\n\ndata: [DONE]\n\n'
    assert extract_model_from_sse(body) is None
