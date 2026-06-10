"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import gzip
import os
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    return _free_port()


@pytest.fixture
def tmp_usage_dir(tmp_path) -> str:
    return str(tmp_path / "token_usage")


@pytest.fixture
def tmp_log_file(tmp_path) -> str:
    return str(tmp_path / "beans_proxy.log")


class _RecordingUpstream:
    """A tiny test double that mimics an OpenAI-compatible upstream.

    Records every request received and lets the test control the response
    (status, JSON body, or SSE body). Run in a background thread so the proxy's
    AsyncClient can connect to it.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._response_factory: Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]] | None = None
        self._sse_factory: Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]] | None = None
        self.port: int | None = None
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def set_response(self, factory: Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]]) -> None:
        self._response_factory = factory

    def set_sse_response(self, factory: Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]]) -> None:
        self._sse_factory = factory

    def start(self) -> None:
        port = _free_port()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        srv.settimeout(0.2)
        self.port = port
        self._server = srv

        def serve():
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                t.start()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        # Give the server a moment to start accepting.
        time.sleep(0.02)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            conn.settimeout(1.0)
            # Read headers
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 65536:
                    break
            header_block, _, rest = data.partition(b"\r\n\r\n")
            lines = header_block.decode("iso-8859-1").split("\r\n")
            request_line = lines[0]
            method, path, _ = request_line.split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            content_length = int(headers.get("content-length", "0") or "0")
            body = rest
            while len(body) < content_length:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                body += chunk

            # Parse query string
            query = ""
            if "?" in path:
                path, query = path.split("?", 1)

            recorded = {
                "method": method,
                "path": path,
                "query": query,
                "headers": headers,
                "body": body,
            }
            self.requests.append(recorded)

            # Decide response: SSE factory if Content-Type hints, else JSON factory
            factory = self._sse_factory if self._sse_factory else self._response_factory
            if factory is None:
                status, hdrs, resp_body = 200, {"content-type": "application/json"}, b"{}"
            else:
                status, hdrs, resp_body = factory(recorded)

            header_lines = [f"HTTP/1.1 {status} OK"]
            for k, v in hdrs.items():
                header_lines.append(f"{k}: {v}")
            header_lines.append(f"Content-Length: {len(resp_body)}")
            header_lines.append("Connection: close")
            response = ("\r\n".join(header_lines) + "\r\n\r\n").encode("iso-8859-1") + resp_body
            conn.sendall(response)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def upstream():
    srv = _RecordingUpstream()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def gzip_factory():
    """Returns a function that wraps a response factory in gzip + Content-Encoding.

    Usage:
        upstream.set_response(gzip_factory(lambda req: (200, {"content-type": "application/json"}, b'{"x":1}')))
    """
    import json as _json

    def wrap(factory):
        def wrapped(req):
            status, headers, body = factory(req)
            compressed = gzip.compress(body)
            headers = {**headers, "content-encoding": "gzip", "content-length": str(len(compressed))}
            return status, headers, compressed
        return wrapped

    return wrap
