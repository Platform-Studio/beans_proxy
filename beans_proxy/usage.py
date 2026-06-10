"""Persistence layer for token usage records.

Records are stored as JSON arrays in `<usage_dir>/<pseudo_key>.json`.
Per-key writes are serialized with an in-process lock, and each write is
performed atomically (temp file + `os.replace`) to prevent torn writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# A single, shared lock table so all Store instances serialize per-key writes.
_KEY_LOCKS: dict[str, asyncio.Lock] = {}
_KEY_LOCKS_GUARD = asyncio.Lock()


async def _lock_for(key: str) -> asyncio.Lock:
    async with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _KEY_LOCKS[key] = lock
        return lock


def _sanitize_key(pseudo_key: str) -> str:
    """Sanitize a pseudo-API key for use as a filename.

    We accept arbitrary strings (per spec), but we still need a valid filename
    on disk. Replace path separators and other unsafe characters.
    """
    safe = []
    for ch in pseudo_key:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append(f"_{ord(ch):x}")
    return "".join(safe) or "default"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UsageStore:
    """Read/write access to per-key usage JSON files."""

    def __init__(self, usage_dir: str | Path):
        self.usage_dir = Path(usage_dir)
        self.usage_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, pseudo_key: str) -> Path:
        return self.usage_dir / f"{_sanitize_key(pseudo_key)}.json"

    async def read(self, pseudo_key: str) -> list[dict[str, Any]]:
        """Return the recorded usage array for a key, or [] if the file is missing."""
        path = self._path_for(pseudo_key)
        if not path.exists():
            return []
        # File reads are quick; do them synchronously off the event loop thread
        # would be ideal, but they're small JSON arrays and Python's GIL keeps
        # things predictable. Use a thread for safety under load.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_sync, path)

    @staticmethod
    def _read_sync(path: Path) -> list[dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupt file: start fresh rather than 500 the request.
            return []
        if not isinstance(data, list):
            return []
        return data

    async def append(self, pseudo_key: str, record: dict[str, Any]) -> None:
        """Atomically append a record to the key's usage file."""
        lock = await _lock_for(pseudo_key)
        async with lock:
            path = self._path_for(pseudo_key)
            existing = self._read_sync(path) if path.exists() else []
            existing.append(record)
            self._write_atomic(path, existing)

    @staticmethod
    def _write_atomic(path: Path, data: list[dict[str, Any]]) -> None:
        """Write the JSON array atomically: temp file in the same dir, then replace."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Use delete=False + manual fsync for crash safety, then os.replace.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
