"""Tests for the persistence layer."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from beans_proxy.usage import UsageStore, _sanitize_key


def test_sanitize_key_keeps_safe_chars():
    assert _sanitize_key("sk-task-12345") == "sk-task-12345"


def test_sanitize_key_encodes_unsafe():
    sanitized = _sanitize_key("weird/key with spaces?")
    assert "/" not in sanitized
    assert " " not in sanitized
    assert "?" not in sanitized


def test_sanitize_key_empty_falls_back():
    assert _sanitize_key("///")  # non-empty result


async def test_append_creates_file(tmp_usage_dir):
    store = UsageStore(tmp_usage_dir)
    await store.append("sk-a", {"started_at": "t", "ended_at": "t", "input_tokens": 1, "output_tokens": 2})
    data = await store.read("sk-a")
    assert data == [{"started_at": "t", "ended_at": "t", "input_tokens": 1, "output_tokens": 2}]


async def test_read_missing_returns_empty(tmp_usage_dir):
    store = UsageStore(tmp_usage_dir)
    assert await store.read("nope") == []


async def test_concurrent_appends_preserve_all_records(tmp_usage_dir):
    store = UsageStore(tmp_usage_dir)

    async def worker(i: int):
        await store.append(
            "concurrent",
            {
                "started_at": f"t{i}",
                "ended_at": f"t{i}",
                "input_tokens": i,
                "output_tokens": i * 2,
            },
        )

    await asyncio.gather(*(worker(i) for i in range(50)))
    data = await store.read("concurrent")
    assert len(data) == 50
    seen = {d["input_tokens"] for d in data}
    assert seen == set(range(50))


async def test_corrupt_file_returns_empty(tmp_usage_dir):
    p = Path(tmp_usage_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / "broken.json").write_text("{ not valid json")
    store = UsageStore(tmp_usage_dir)
    assert await store.read("broken") == []
