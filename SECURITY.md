# Security Policy

## Supported Versions

Until Beans Proxy reaches 1.0, security fixes are made on the latest released version and the `main` branch. Older pre-1.0 releases are not maintained separately.

## Reporting A Vulnerability

Do not open a public issue or discussion for a suspected vulnerability.

Use GitHub's [private vulnerability reporting form](https://github.com/Platform-Studio/beans_proxy/security/advisories/new).

Include the affected version or commit, impact, reproduction steps, and any suggested mitigation. Remove real credentials, personal data, prompts, logs, and unrelated private material from the report.

We aim to acknowledge a report within five business days and provide a status update within ten business days. Resolution timing depends on severity and complexity. We will coordinate disclosure and credit with the reporter unless anonymity is requested.

If private vulnerability reporting is temporarily unavailable, contact a repository maintainer privately through the [Platform-Studio GitHub organization](https://github.com/Platform-Studio). Do not transmit secrets in an initial message.

## Security Scope

High-value areas include:

- exposure or mishandling of upstream API keys, pseudo-keys, environment variables, prompts, logs, or token-usage files;
- unauthorized access to forwarded upstream requests or usage data;
- path traversal or access outside configured usage and log directories;
- unsafe request forwarding or header handling;
- dependency or installation-chain compromise; and
- denial-of-service risks in the proxy or pricing loader.

The query endpoint is intentionally unauthenticated and should only be exposed on trusted networks.
