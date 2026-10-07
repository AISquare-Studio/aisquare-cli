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

- **State** lives under `~/.aisquare/` (or `$AISQUARE_HOME`): the configuration,
  each project's snapshot, and the SQLite store `context.db`, which keeps every
  prompt you submit verbatim (only the copies that leave the machine are
  scrubbed). Explainability, semantic recall and added Claude accounts keep
  their spool, inbox, per-project brain and account directories here too, and a
  local Explainability proxy buffers traces in `claude_proxy_inbox.db` with the
  workspace key on every row. Unlike the credentials below, all of this gets
  your umask's default permissions, so another account that can enter your home
  directory can read it; `chmod 700 ~/.aisquare` prevents that.
- **Credentials:** `aisquare login`, `aisquare init --api-key` and
  `aisquare serve` write `~/.aisquare/credentials`. An Explainability workspace
  key lives in `~/.aisquare/explainability-key`, and a project's own key in
  `~/.aisquare/projects/<id>/explainability-key`. Each is restricted to your
  user; when that cannot be applied (it can fail on Windows), the CLI says so.
- **Claude Code accounts:** each account you add (`aisquare accounts add`, or the
  Accounts page in `asq`) is a Claude Code config directory under
  `~/.aisquare/claude-accounts/<n>/`. On Linux, Claude Code keeps that account's
  sign-in there; on macOS it uses the Keychain. `aisquare accounts remove`
  renames the directory to `<n>.removed-<stamp>` with the sign-in still in it,
  so delete it when you no longer need it.
- **Agent settings:** the one-line installer (unless `--no-agent`),
  `aisquare init --agent claude-code` and `aisquare agents connect` write hooks
  into Claude Code's `settings.json`, and every Claude account you add gets them
  in its own. They run on every session and are built to fail open;
  `aisquare agents disconnect claude-code` removes them.
- **The Collective Intelligence test bed**, while it is on, keeps each prompt's
  uncommitted changes (tracked files only) as a git object under
  `refs/aisquare/wip/` in your repository. Refs older than seven days are
  dropped the next time it takes a snapshot there.

## What leaves your machine

Besides Claude Code's own traffic, two downloads happen by default. Neither
carries your code or your prompts:

- Packing a project's snapshot (`aisquare init` and `aisquare project onboard`
  do it, and so does the one-line installer when run inside a repository) runs
  Repomix. With no `repomix` on PATH, that is `npx --yes repomix`, which fetches
  the latest Repomix from the npm registry and runs it over the project. Install
  a pinned `repomix` to choose the version, or skip the snapshot with
  `aisquare init --no-onboard`.
- With tiktoken installed (the one-line installer adds it), the first snapshot
  token count downloads its encoding from OpenAI's public blob store
  (`openaipublic.blob.core.windows.net`) into the temp directory, and again once
  that is cleared.

The CLI also reads each signed-in Claude account's plan usage, the plain
`claude` included, with that account's own sign-in, from the endpoint Claude
Code's `/usage` reads: for the Accounts page, `aisquare accounts usage` and
`aisquare accounts list --usage`, and when a launch or a hand-over picks an
account by headroom.

Everything else leaves only once you turn it on:

- **Signing in** (`aisquare login`) sends this machine's hostname as the device
  name, and from then on the CLI calls the AISquare API as you. Choosing where a
  project's traces go lists your workspaces and studios, mints an ingest key
  named after this machine and the project's folder, and binds the CLI's agent
  names to the studio; `aisquare logout` and `aisquare project forget --purge`
  revoke that key. For a project with a destination, `aisquare whoami`,
  `aisquare explainability status`, `aisquare doctor --live` and the Accounts
  page (once a minute while it is open) read that workspace's credit balance.
- **Explainability** sends your sessions to its gateway (AISquare's, unless you
  self-host one): model traffic, your prompts and board events, with
  credentials scrubbed from the CLI's part at your `redaction` level. A proxy
  records the model traffic, because `ANTHROPIC_BASE_URL` points Claude Code at
  it. With AISquare's hosted proxy, which a destination chosen while signed in
  uses, Claude Code's requests, carrying its own Anthropic sign-in or API key,
  and the full responses pass through AISquare's server. With a local proxy
  (`127.0.0.1:9090`, the default), requests go from your machine to Anthropic,
  and only the traces reach the gateway. `aisquare doctor --fix` can also
  install the Explainability SDK from PyPI; it asks first unless you pass
  `--yes`.
- **The Collective Intelligence test bed** (`AISQUARE_CI=1`, or `enabled` under
  `[experiment]`) sends each prompt, scrubbed at your `redaction` level, with
  the repository and branch name, the session ids and a commit id of your
  working tree, to the server you configure, with `AISQUARE_CI_KEY` as a bearer
  token; over plain HTTP, key included, if its URL is `http://`.
- **Semantic recall** (`AISQUARE_BRAIN_EMBED=1`, through the separate gbrain
  tool) sends distilled notes, and each `recall` query, to the embedding
  provider `AISQUARE_BRAIN_EMBED_MODEL` names (OpenAI by default).
- **`aisquare serve`** listens on 127.0.0.1 by default. Bound to any other
  address, its bearer token is the only gate, and it travels over plain HTTP.

A report that any of these sends more than it says, that a file holding a
secret is readable by another user, or that the hooks can be made to run
someone else's command is exactly what we want to hear.
