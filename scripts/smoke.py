"""Smoke-test: start the proxy, point it at a tiny local mock OpenAI server,
and exercise a non-streaming + streaming call. Run with: python scripts/smoke.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

# Ensure the package on disk is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beans_proxy.app import create_app  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_mock_openai(port: int) -> None:
    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            req = json.loads(body or b"{}")
            if req.get("stream"):
                sse = (
                    b'data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}\n\n'
                    b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":4,"completion_tokens":6}}\n\n'
                    b'data: [DONE]\n\n'
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
            else:
                resp = json.dumps(
                    {
                        "id": "chatcmpl-smoke",
                        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 22},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as srv:
        srv.serve_forever()


def main() -> int:
    usage_dir = Path("/tmp/beans_proxy_smoke")
    usage_dir.mkdir(exist_ok=True)
    for f in usage_dir.glob("*.json"):
        f.unlink()

    mock_port = _free_port()
    threading.Thread(target=_start_mock_openai, args=(mock_port,), daemon=True).start()
    time.sleep(0.1)

    proxy_port = _free_port()
    app = create_app(
        target_url=f"http://127.0.0.1:{mock_port}/api",
        target_api_key="upstream-key",
        usage_dir=str(usage_dir),
        log_file=str(usage_dir / "beans_proxy.log"),
    )

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=proxy_port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    # wait for ready
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    base = f"http://127.0.0.1:{proxy_port}"
    with httpx.Client(timeout=5.0) as client:
        r = client.post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": "Bearer smoke-key"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        print("non-stream status:", r.status_code, "body:", r.json()["choices"])

        r = client.post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": "Bearer smoke-key"},
            json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        print("stream status:", r.status_code, "last line:", r.text.strip().splitlines()[-1])

        r = client.get(f"{base}/usage/smoke-key")
        print("usage:", json.dumps(r.json(), indent=2))

    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
