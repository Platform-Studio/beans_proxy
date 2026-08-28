# Contributing To Beans Proxy

Thank you for improving Beans Proxy. Bug fixes, tests, documentation, examples, and focused feature contributions are welcome.

## Before Starting

Use a [GitHub issue](https://github.com/Platform-Studio/beans_proxy/issues) for bugs and concrete feature proposals. Use [GitHub Discussions](https://github.com/Platform-Studio/beans_proxy/discussions) for questions and early ideas. For a large change, agree on the behavior and scope with a maintainer before investing substantial work.

Security vulnerabilities must follow `SECURITY.md` and must not be reported in a public issue.

## Development Setup

Beans Proxy requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
```

Copy `.env.example` to `.env` only when testing an external agent runtime or provider integration. Never commit credentials, prompts, token usage, or real user data.

## Making Changes

- Follow the patterns in the module you are changing.
- Add focused tests for new or changed behavior.
- Keep examples synthetic, deterministic, and usable without paid services.
- Update documentation when changing commands, configuration, persistence formats, or security behavior.
- Avoid unrelated refactoring in the same change.
- Do not commit runtime artifacts such as `*.log`, `token_usage/`, or `.beans_proxy.pid`.

Run the complete test suite before submitting:

```bash
python -m pytest -q
```

## Pull Requests

A pull request should explain the problem, the chosen behavior, material tradeoffs, and validation performed. Keep commits understandable, but maintainers may squash them when merging.

All checks must pass. A maintainer may request changes for correctness, compatibility, security, maintainability, or fit with the project direction.

Unless explicitly stated otherwise, contributions intentionally submitted for inclusion are provided under the Apache License 2.0 as described in section 5 of that license. Beans Proxy does not currently require a separate contributor license agreement.
