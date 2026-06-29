# Security Policy

## Supported versions

aisquare-cli is pre-1.0; only the latest released version on PyPI receives fixes.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately via one of:

- GitHub's [private vulnerability reporting](https://github.com/AISquare-Studio/aisquare-cli/security/advisories/new) (preferred), or
- email **anmol@aisquare.studio**.

Please include steps to reproduce and the affected version. We aim to acknowledge
reports within 3 business days and to ship a fix or mitigation as quickly as is
practical, crediting reporters who wish to be named.

## Scope notes

aisquare stores context locally under `~/.aisquare/` and (with `agents connect`)
writes hooks into your agent's settings. It does not transmit your data anywhere;
captured prompts and snapshots stay on your machine. API keys passed to
`aisquare init --api-key` are written to `~/.aisquare/credentials` with `0600`
permissions.
