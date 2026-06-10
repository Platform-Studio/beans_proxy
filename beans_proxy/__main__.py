"""CLI entrypoint: `python -m beans_proxy`."""

from __future__ import annotations

import uvicorn

from .config import load_settings
from .app import create_app


def main() -> None:
    settings = load_settings()
    app = create_app(
        target_url=settings.target_url,
        target_api_key=settings.target_api_key,
        usage_dir=settings.usage_dir,
        log_file=settings.log_file,
        passthrough_prefixes=settings.passthrough_paths,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
