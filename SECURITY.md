# Security policy

## Supported versions

aisquare-cli is pre-1.0 and releases often. Only the latest release on
[PyPI](https://pypi.org/project/aisquare-cli/) gets security fixes, so please
check `aisquare --version` against it before you report.

## Reporting a vulnerability

**Please do not report a security problem in a public issue, pull request or
discussion.**

Report it privately, either way:

- **GitHub:** on the [Security tab](https://github.com/AISquare-Studio/aisquare-cli/security),
  choose *Report a vulnerability*. This is GitHub's private vulnerability
  reporting: only you and the maintainers see the report.
- **Email:** anmol@aisquare.studio.

Include the version (`aisquare --version`), your OS, how you installed it, and
the steps to reproduce. We aim to acknowledge a report within 3 business days
and to ship a fix or a mitigation as quickly as is practical, and we credit
reporters who want to be named.

## What it keeps on your machine

- **State** lives under `~/.aisquare/` (or `$AISQUARE_HOME`): one SQLite
  database, the configuration, and each project's snapshot.
- **Credentials:** `aisquare login` and `aisquare init --api-key` write
  `~/.aisquare/credentials`, readable by your user only. An Explainability
  workspace key lives in `~/.aisquare/explainability-key`.
- **Agent settings:** `aisquare agents connect` writes hooks into Claude Code's
  `settings.json`. They run on every session and are built to fail open.

## What leaves your machine

Besides Claude Code's own traffic (the Accounts page also reads your plan usage
from the endpoint Claude Code's `/usage` reads), nothing, until you turn it on:

- `aisquare login` talks to the AISquare API.
- Explainability sends your sessions to the gateway you configure: model
  traffic, your prompts and board events, with credentials scrubbed from the
  CLI's part at your `redaction` level.
- The Collective Intelligence test bed (`AISQUARE_CI=1`) sends each prompt,
  scrubbed, to the server you configure.
- Semantic recall (`AISQUARE_BRAIN_EMBED=1`, through the separate gbrain tool)
  sends distilled notes to OpenAI to embed them.
- `aisquare serve` listens on 127.0.0.1 by default. Bound to any other address,
  its bearer token is the only gate and travels over plain HTTP.

A report that any of these sends more than it says, that a credential is
readable by another user, or that the hooks can be made to run someone else's
command is exactly what we want to hear.
