# Changelog

All notable changes to Beans Proxy will be recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- OpenAI-compatible FastAPI proxy with pseudo-API-key attribution.
- Per-key JSON persistence for token usage and calculated USD cost.
- Streaming usage capture through `stream_options.include_usage`.
- Model recording for streaming and non-streaming responses.
- Startup-loaded OpenRouter pricing catalog indexed by model ID and canonical slug.
- Graceful startup behavior when OpenRouter pricing cannot be loaded.
- Atomic, serialized usage-file writes for concurrent requests.
- `./beans.sh` lifecycle commands for starting, stopping, restarting, and checking the proxy.
- Public project documentation for contributing, governance, security, support, and community conduct.

### Changed

- Usage records now include input, output, and total cost when the model has a known OpenRouter price.
- Existing usage records are preserved and are not backfilled or recalculated.

### Security

- Caller authorization is replaced before forwarding requests, so pseudo-API keys are not sent to the upstream provider.
- Runtime logs, token usage files, environment files, and PID files are excluded from version control.

No public release has been tagged yet. Release links will be added when the first version is published.
