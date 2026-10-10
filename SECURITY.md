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
the steps to reproduce. We aim to acknowledge a report within 3 business days.

## What the CLI stores, changes and sends

An inventory, to help you judge the surface. Each item names the command or
setting it comes from.

**Stored** under `~/.aisquare/`, or `$AISQUARE_HOME` if set:

- `context.db` holds your memory entries, the board, and every prompt you
  submit, verbatim. Claude Code's `UserPromptSubmit` hook captures them.
- `credentials` (from `aisquare login`, `aisquare init --api-key` and
  `aisquare serve`) and the Explainability keys (`explainability-key`,
  `projects/<id>/explainability-key`) are restricted to your user. The CLI
  warns when it cannot restrict them.
- `claude-accounts/<n>/` is one per account added with `aisquare accounts add`.
  Claude Code keeps that account's sign-in there on Linux and Windows
  (`.credentials.json`), and in the Keychain on macOS. `aisquare accounts remove`
  renames the directory and signs nothing out, so run `/logout` in the account
  first (`aisquare accounts run <n>`).
- Everything else gets your umask's default permissions. To restrict the whole
  tree to your user, run `chmod 700 "${AISQUARE_HOME:-$HOME/.aisquare}"`.

The Collective Intelligence test bed, while on, keeps each prompt's uncommitted
tracked changes as git objects under `refs/aisquare/wip/` in your repository. It
prunes them only while it is on. To delete them, run
`git for-each-ref --format='delete %(refname)' refs/aisquare/wip/ | git update-ref --stdin`.

**Changed in Claude Code's settings:** these commands install hooks that run on
every session and capture your prompts:

- `aisquare init --agent claude-code`;
- the one-line installer, unless you pass `--no-agent`;
- `aisquare agents connect claude-code`;
- each `aisquare accounts add`, into that account's own settings;
- `aisquare upgrade`, and asq's Update, which rewrite them for the new version in
  every directory this machine connected (`agents.json`), through
  `aisquare agents refresh-hooks`.

`aisquare agents disconnect claude-code` removes them from one config directory:
`$CLAUDE_CONFIG_DIR` or `~/.claude`, or the one `--config-dir` names.
`aisquare uninstall`, and asq's Uninstall, remove them from every Claude Code
directory they find: the connected ones, `$CLAUDE_CONFIG_DIR`, each `~/.claude*`
that holds them, and the account slots, removed ones included. With `--purge`,
`~/.aisquare` and its account slots are deleted too; on macOS their sign-in
tokens stay in the Keychain.

Fleet agents (`asq`, `aisquare fleet spawn`) start with
`--permission-mode auto` unless their role or the command says otherwise. In
that mode, Claude Code's classifier, not you, approves each tool call.

**Sent:**

- **Upgrades.** `aisquare upgrade --check`, and `aisquare upgrade` without
  `--version` on an install it runs, ask PyPI for the latest release
  (`https://pypi.org/pypi/aisquare-cli/json`), with a `User-Agent` that names
  the installed version. When the install's uv receipt records its own index,
  or another setting that can hold a release back (a cutoff alone is still
  compared), or cannot be read, nothing is asked and `--check` says it cannot
  tell. `aisquare upgrade` then runs `uv tool install --python <version>` (the
  Python its uv receipt records, else the running interpreter's version), which
  downloads from PyPI, or from the index the install was made from. If that
  Python is neither on the PATH nor among uv's managed Pythons, uv first
  downloads a CPython build from Astral (python-build-standalone). The run sets
  `UV_PYTHON_DOWNLOADS=automatic`, so a `python-downloads` setting in uv.toml
  (Fedora ships `manual`) does not stop that download. A `UV_PYTHON_DOWNLOADS`
  you exported is kept, so `never` or `manual` set there does.
- **The Claude Code plugin.** With no aisquare CLI installed, its hooks run
  `uvx --from aisquare-cli==<version>`, which downloads that release from PyPI
  on the first session. If no Python 3.11 to 3.13 is installed, uv first
  downloads a CPython build from Astral (python-build-standalone), unless uv's
  `python-downloads` setting (`UV_PYTHON_DOWNLOADS`) is `manual` or `never`.
- **Snapshots.** Packing a snapshot (`aisquare init`, `aisquare project
  onboard`) runs `npx --yes repomix` when no `repomix` is installed, which
  fetches the latest Repomix from npm. With tiktoken installed, the first token
  count downloads its encoding from OpenAI's blob store.
- **Plan usage.** Wherever the CLI shows or checks a Claude account's plan
  usage, or picks an account by headroom, it reads that usage from Anthropic with the
  account's own sign-in.
- **Signing in.** After `aisquare login`, which sends this machine's hostname,
  the CLI calls the AISquare API with your session. Choosing where a project's
  traces go mints an ingest key named after this machine and the project.
- **Explainability, when on.** Your sessions go to its gateway. Through
  AISquare's hosted proxy, Claude Code's requests, with its Anthropic
  credential, and the responses pass through AISquare's server. Through the
  default local proxy (`127.0.0.1:9090`), requests go to Anthropic as usual, and
  only the traces go to the gateway. Note that `aisquare doctor --fix` turns
  Explainability on without asking.
- **The Collective Intelligence test bed, when on** (`AISQUARE_CI=1`, or
  `enabled` under `[experiment]`). Each prompt goes to the server you
  configure, scrubbed at your `redaction` level, with the repository, branch,
  session ids and a commit id. `AISQUARE_CI_KEY` goes with it as a bearer token, in
  cleartext if the URL is `http://`.
- **Semantic recall, when on** (`AISQUARE_BRAIN_EMBED=1`). Distilled notes and
  each `recall` query go to the embedding provider you configure.
- **`aisquare serve` on a non-loopback address.** Its bearer token travels over
  plain HTTP.
