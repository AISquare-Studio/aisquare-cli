# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Accounts, in `asq` and on the command line.** A new **Accounts** section in
  the fleet UI's sidebar opens a page with the AISquare sign-in on top and the
  Claude Code accounts under it. The AISquare card runs `aisquare login`'s
  device flow natively — the one-time code and link appear on the page, the
  browser opens when one can reach you, *Cancel* stops the wait — and *Sign
  out* revokes as `aisquare logout` does. Below it, every Claude Code account
  the CLI knows: **slot 1** is the plain `claude` of the machine; **+ Add Claude
  account** creates a numbered slot (`~/.aisquare/claude-accounts/<n>`, its own
  `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR`) and opens Claude Code's own
  login in a pane rendered right there; the page watches the directory and, the
  moment the login lands, records the account, installs aisquare's hooks into it
  and closes the pane. Each signed-in row shows the plan and the five-hour and
  seven-day usage as bars, from the endpoint Claude Code's `/usage` reads (best
  effort: if it changes, the row says `usage unavailable`). *Remove* renames a
  slot's directory beside itself as `<n>.removed-<stamp>` rather than deleting
  it. Slot 1 is never a directory of ours and is never launched with
  `CLAUDE_CONFIG_DIR=~/.claude` — Claude Code keeps the default install's
  `.claude.json` beside that directory and would re-onboard into an empty one.
  - `aisquare accounts` (`list [--usage]`, `add`, `run <slot> [claude args]`,
    `usage [slot]`, `remove <slot>`), every reporting command with `--json`;
    `run` is what a `c2` shell alias was, with the environment decided in one
    place. `aisquare launch <role> --account <slot>` and `aisquare fleet spawn
    <role> --account <slot>` run a board role on an account; the board labels
    such sessions `account N`. `doctor` gains a `claude-accounts` line naming
    any slot that still needs a sign-in. Nothing here writes into Claude Code's
    own files, and no hook or session path ever reaches the usage endpoint.
    Plan: `docs/plans/claude-accounts.md`; guide: `docs/fleet.md`.
- **`aisquare login`, `logout`, `whoami` and `aisquare auth status|token` do
  something real.** Sign-in is the OAuth 2.0 device flow (RFC 8628) against the
  AISquare identity provider: the terminal shows a one-time code and a link,
  you approve in a browser, and the CLI polls the standard token endpoint until
  the server hands over a 90-day session token, stored 0600 in
  `~/.aisquare/credentials`. Discovery-driven (`/o/.well-known/openid-configuration`),
  standard-library HTTP only, no new dependencies. Headless machines print the
  link instead of opening a browser; `--no-browser` forces that; `BROWSER=echo`
  is honoured. Esc or Ctrl-C cancels (exit 130). `AISQUARE_TOKEN` supplies a
  token read-only for CI, `--with-token` stores one from stdin, `--api-url`
  targets another server and a session is refused against a different host
  (`api_url_mismatch`). No refresh: an expired or revoked session says
  "run aisquare login". `logout` revokes on the server (RFC 7009) and forgets
  locally. `--json` puts exactly one object on stdout. Redaction learns the
  `aisq_` token shape. Contract: `docs/plans/aisquare-login.md`; guide:
  `docs/signing-in.md`. The `auth rotate` stub is gone (sessions do not rotate).

- **`project forget <id|name|codename|path>` and `project prune`** (#83), so a
  store with hundreds of dead registrations can be cleaned. Measured on the
  owner's box: 305 registered projects, most of them throwaway git worktrees,
  and the fleet UI loaded state for every one before its first frame. `forget`
  drops one registration and refuses (exit 2) while the project has live fleet
  agents; `prune --missing` drops registrations whose root is gone from disk,
  `prune --worktrees` those whose root is a linked git worktree of a repository
  that is itself registered (neither flag: both). `prune` prints its plan and
  asks at a terminal; off one it is a dry run unless `--yes`, and `--json`
  without `--yes` lists the candidates and changes nothing. A plain forget is a
  tombstone (store schema v14, `project.forgotten_at` — the `entry` and
  `prompt` tables hold foreign keys to the project row, so a project with any
  history cannot be deleted from under them): the project's context entries,
  prompt history and board rows stay in the store, hidden, and come back if the
  root is registered again. `--purge` deletes them, the ended fleet-agent rows,
  the turn metrics and `~/.aisquare/projects/<id>/`. Forgetting the ACTIVE project moves the pin
  to the most recently touched remaining project, or clears it, and says so.
- **Client decks in `docs/deck/`, one self-contained HTML file each, with the
  PDF beside it.** A one-pager, a five-page short deck and a fifteen-slide pitch
  deck, for showing the fleet to someone who has not seen it. Each HTML embeds
  its own CSS, its diagrams, its terminal captures and its three typefaces —
  subset to the glyphs it uses, under the OFL — so it has no external reference
  of any kind and opens from `file://` unchanged. `docs/deck/README.md` says
  which capture is real data and which is a worked example, because these are
  the files someone will reuse in a slide of their own.
  - **The captures are real renders of the real UI**, not mockups: `cli/ui/` and
    the board TUI were driven headless through Textual's pilot and exported to
    SVG, so the layout, the role icons and the state chips are the shipping
    code's. The board's rows are real data — the CLI was driven through an
    actual sequence of five contracts, two claims, a review, a tester's reopen
    with its reason, a routed question and a signal — and the doctor page uses
    the real check names from `services/diagnostics.py`.
  - **Each deck carries its own print stylesheet**, so a browser's Print dialog
    follows a layout authored for paper rather than paginating the scroll
    layout: printing the short deck or the pitch deck reproduces its committed
    PDF, one leaf or one slide per page. The one-pager is the stated exception —
    its web page is ~3,700px tall at full measure, four A-series pages, so its
    PDF is a separate single-A3 sheet and printing the HTML gives four A4 pages.
  - **Four checks run against the built files, and each of them found something.**
    Measuring every page element against its page box turned six overflowing
    slides into none. Checking every `pre`/`nowrap` block for horizontal
    overflow — such a block scrolls on screen and crops *silently* on paper —
    found the one-line installer losing `.sh | sh` off two pages, and a
    model-harness table losing 35px of its own right edge; spotting these by eye
    had found two of the four. Checking every hand-drawn figure label against
    its `viewBox` found two captions past the right edge. Checking every
    character against the embedded faces found the non-breaking hyphen, used 88
    times in body copy like `fail-open`, has no glyph in any of the three faces
    and had been rendering in a fallback font all along.
  - Two layout defects fell out of the same pass: `margin: 0 auto` on a CSS grid
    item cancels `justify-self: stretch`, so two diagrams sized to an SVG's
    300px default instead of their 804px track; and a `display: grid` that was
    only ever declared on `.duo` left every `.duo-wide` block silently stacking
    rather than splitting into two columns.

- **`aisquare serve` says out loud what a non-loopback `--bind` gives up.**
  0.6.0 changed the HTTP transport so that a bind outside `127.0.0.1`,
  `localhost` and `::1` runs with no Host/Origin validation — described at
  length in the entry below, and visible nowhere else. The SDK logs nothing
  when it skips that protection, the CLI printed the same startup line for
  every bind, and `--bind`'s help predated the change, so the entire
  disclosure reached changelog readers and source readers and never the person
  opening the port. Such a bind now prints a second stderr line at startup
  naming what is off and what is left — the bearer token, a long-lived
  credential (`auth rotate` is still a stub) sent in clear over plain HTTP on
  every request, so a trusted network or a TLS-terminating proxy — `--bind`'s
  help says it in a sentence, and the README's serve section covers the flag.
  The notice keys on `LOOPBACK_BINDS` in `services/mcp_server.py`. That tuple
  mirrors the literal the SDK matches on rather than being handed to it —
  `run_http` passes only `host=bind` — so a test is the only thing that can
  hold the two equal, and one drives all three spellings, plus a `127/8`
  address deliberately outside them, against the real transport. Without it,
  dropping a spelling passes every test while the CLI starts announcing an
  exposure the SDK is in fact still preventing.

- **Collective Intelligence test bed — retrieval in front of the agent, off by
  default (experimental).** When a prompt is submitted, aisquare can ask a CI
  server whether the workspace already knows something relevant and hand that to
  the agent *before* it starts exploring, through the `UserPromptSubmit` hook
  installed since day one. **Nothing runs unless `AISQUARE_CI=1`, a URL, a token
  and a run id are set:** with the switch off there is no request, no connection
  and no measurable latency, and any unrecognised value of the switch is off.
  - The CLI speaks **hook contract v2**, the server's frozen contract. Its seven
    schemas and their fixtures are vendored byte for byte
    (`tests/fixtures/ci_contract/v2/`); every request the CLI can emit is
    validated against the server's schema with `jsonschema` in the suite, and
    the models refuse what the schemas refuse — an unknown key, a scope id in an
    id field, `allow`/`block`/`substitute`, an `inject` with no briefing.
  - **The server's delivery descriptor decides delivery.** Fetched once per
    session and cached until it expires, it says which hooks call the server,
    where, under what ceiling, and whether the `collective_intelligence_recall`
    MCP tool is exposed in `aisquare serve`. It carries no architecture or arm,
    so the CLI is structurally unable to know which arm it is running.
  - **The ceiling is wall clock.** The descriptor's `client_safety_ms` bounds the
    whole exchange — a server dribbling bytes cannot hold the hook past it, a
    response that lands late is a breach, bodies are capped, and nothing retries.
    `agents connect` gives the two context hooks a 120 s Claude Code timeout so
    the agent does not discard the hook's answer first.
  - `aisquare metrics show|list` (hidden) — one row per hook event, scoped to the
    current project (`--project`, `--all`). The row carries the join keys the
    server ledger pairs on (`run_id`, `session_id`, `trace_id`, `query_id`), the
    server's `status`/`action`, and a **client reason** in three groups that are
    never summed: baseline (never asked), by design (chose not to), failure
    (tried). Round-trip percentiles cover consulted turns only.
  - Each turn snapshots the working tree (`git stash create`, kept alive under
    `refs/aisquare/wip/<trace_id>`) so it can be replayed later; the object id
    travels, the ref name does not, and the row records that untracked files are
    excluded.
  - Retrieved material is framed as candidate reference — caveat before and
    after, a delimited region the payload cannot close, control characters
    stripped, a 16 384-character cap with both sizes recorded. `aisquare why` names the
    items shown without clobbering the entry counts.
  - The prompt is scrubbed at the configured `redaction` level before it leaves,
    and the level is recorded. A `ci_turn` join record is spooled through the
    Explainability client lane when shipping is configured, so server rows and
    CLI rows meet through the pipeline id.
  - `doctor` reports the switch, the URL (scheme required, credentials never
    echoed), the token and run, `GET /ready`, and the descriptor fetch — a
    rejected token, an unknown run, an expired run and a contract skew each get
    their own line and fix. Every probe is bounded.
  - Token counts are **not** recorded — hook payloads do not carry them, and
    `metrics show` says so rather than reporting a zero that reads as "no tokens
    were used". The contract pointer and the CLI's standing assumptions are in
    `docs/ci-contract.md`; the server seam is `docs/ci-integration-handoff.md`.
  - `tests/stub_ci_server.py` speaks v2 and can be run by hand
    (`python -m tests.stub_ci_server --port 8765`) to point a real session at it.
- **The CI test bed is wired to the live staging server** (`ci-api.aisquare.studio`,
  2026-09-02) — three changes the real server asked for, all off unless the
  switch is on:
  - **Refusals are read, not just counted.** A non-200 from either route carries
    an `error.v1` body live (`scope_resolution_failed` on a 401,
    `dependency_unavailable` with "has no completed build" on a 503). The code
    lands on the row's `error_codes` and the clipped sentence in the detail;
    `doctor` quotes both on its descriptor line and picks the fix from the
    status rather than from words in a message the server wrote. Nothing
    branches on `retryable`; nothing retries.
  - **The recall tool uses the server's pull route.**
    `collective_intelligence_recall` forwards to
    `POST /v1/mcp/collective_intelligence_recall` as `mcp-tool-input.v1` — so
    `token_budget` and `reason` travel instead of being reported as dropped —
    with `run_id` the descriptor's (the server has no default run and refuses
    its absence; an agent-supplied value naming another run is refused, so the
    row and the ledger always agree). `prompt` and `reason` leave scrubbed and
    clipped to the contract on both sides of the scrub. The answer is the bare
    briefing; an `empty` answer is the server's own briefing with no items,
    returned as such. The stub grew the route; the suite drives the tool end to
    end through a real in-memory MCP client.
  - **A loud, recorded staging override.** The staging descriptor still says
    `direct_api` for every run, so the descriptor-gated hooks never call.
    `AISQUARE_CI_DELIVERY_OVERRIDE=hook_push:session_start,prompt_submit;mcp_pull`
    (environment only) stands in for the delivery list **only** when the fetched
    descriptor is `direct_api`-only — ignored otherwise, ignored when malformed,
    never cached — and cannot be mistaken for the descriptor's ruling: every
    row and join record carries `delivery_source` (`descriptor` | `override`),
    `metrics list` shows it as `SOURCE`, `metrics show` counts override rows
    apart and keeps them out of the round-trip percentiles, and `doctor` warns
    on its own line whenever it is set — active, ignored, or malformed. Rows it
    produces measure nothing; it goes when the server publishes real delivery
    modes.
  - The column arrives as **schema v13**, a converging migration, because
    `user_version 11` means two incompatible things in the wild: 0.6.0 from PyPI
    stamped it for the fleet tables, and this branch stamped it for the `metric`
    table. Renumbering cannot serve both — whichever meaning keeps the number,
    the other cohort's next step hits a table that already exists and the store
    stops opening, which takes every command and every hook with it. So v11 is
    left exactly as released (it only ever runs below 11, where neither table
    can exist), v12 creates `metric` only if it is absent, and v13 gives the
    fleet tables to anyone who reached 11 or 12 down this branch. On top of
    that, v11 reached developer machines in three further shapes — the v2 table,
    no table at all (following the earlier advice to delete the v1-shaped one),
    and the v1-shaped table itself, which is renamed to `*_v1_orphaned` onto a
    free name and never dropped. Every shape is a case in
    `test_every_shape_of_user_version_11_converges_on_one_schema`, which asserts
    the end state by writing to both tables rather than by reading the version.
    Deleting the `metric` table by hand is no longer needed and no longer safe.
- **The review of the CI branch at `ee422b5`, acted on.** Every item sits where
  server-controlled bytes cross into the client, and each has a test that failed
  before its fix:
  - the injection frame could be escaped by one invisible character (a
    zero-width space, a byte-order mark, a bidi override) or an odd line break
    (U+2028, U+2029, U+0085, `\r`); a lone surrogate in a briefing turned
    `session-start` into a traceback because the write sat outside the guard;
  - the transport followed redirects and would have re-sent the bearer token to
    another origin; a multi-line token was echoed by the header parser into a
    detail `doctor` prints;
  - the strict models refused `63.0` where the schema says `integer`;
  - the v12 healing migration could wedge a store on a fixed orphan name;
  - a credential straddling the 100 000-character cut shipped in the clear;
    the descriptor's `client_safety_ms` had no client-side maximum, so a run
    could publish a ceiling past the installed hook timeout; a failed descriptor
    fetch cost a fresh 10 s probe on every prompt; snapshot refs grew forever;
  - the recall tool returned the briefing text raw, uncapped and unmeasured,
    and a locked store escaped it as an opaque crash;
  - the hook install check ignored the timeout value; an unrecorded prompt was
    silent; three `doctor` lines asserted more than their probe established.
  Also: the compare-and-set in `close_turn` and five CHECK vocabularies are now
  actually tested, the vendored-contract drift guard is pinned to the deployed
  server commit, and the cap is stated in characters, which is what it is.

- **`install.sh` — the one-line installer.** macOS, Linux and WSL2, in POSIX
  `sh` (it runs as Debian's `dash` and Alpine's BusyBox `ash`).
  [`uv`](https://docs.astral.sh/uv/) is the bootstrap, so nothing depends on the
  machine already having a Python — it brings its own 3.13 for the CLI alone,
  leaving the system Python untouched. It surveys before it writes: a machine
  that is already current prints its summary and exits 0 having installed
  nothing, and running it twice is a no-op. Installs `uv`, Python, `aisquare-cli`
  with `tiktoken`, tmux, `gh`, `git`, Node 22+ and Claude Code; then
  `aisquare init --agent claude-code`, which wires the hooks and packs the
  snapshot in the same run. Refuses to run as root outside a container, uses
  `sudo` for one command at a time, never edits your shell profile beyond what
  `uv` and the Claude Code installer do themselves, and never `--reinit` (which
  would discard `team bind` role bindings on every re-run). `--dry-run` prints
  every command and runs none — that is how you decide whether to trust it.
  Full flag table in the README; design, measurements and the rejected
  alternatives in `docs/plans/one-line-install.md`.
- **`install.ps1` — a WSL2 shim for Windows.** Not an installer: the fleet gives
  every agent a real tmux pane and Windows has no tmux, so it detects WSL2 and
  delegates into it, or prints the one command that installs WSL.
- **A container matrix for the installer** (`tests/install/`, and its own CI
  workflow). Five bare distributions — Debian 12, Ubuntu 22.04, Fedora 41, Arch,
  Alpine 3.22 — each installing a wheel built from the tree under review, plus a
  cell that runs as a **normal user with sudo** (the primary case, and the only
  one where the script's `sudo` path is exercised at all), one that installs
  Claude Code for real, and a macOS job. Each cell runs the installer four
  times: bare, with a project — where the acceptance criterion is asserted,
  *every check ok except `brain`* — then again with every package manager
  replaced by a stub that records being called, asserting the re-run installs
  nothing, moves no version, leaves `~/.claude/settings.json` byte-identical and
  calls no package manager; and finally the upgrade path, staging an exact
  `==0.5.0` pin and asserting the version moves *and* that `tiktoken` survives.
  The criterion is asserted as a *set* with the total floored rather than pinned,
  which is why it kept holding when `doctor` gained an eighteenth check. It runs
  on a schedule as well as on pushes, because four of the things the script
  fetches belong to other people.

### Changed
- **The snapshot token budget is a config knob, and the failure names its
  numbers (#82).** `aisquare project onboard` on a large repo printed only
  "codebase too large to pack within the token budget" against a hardcoded
  150 000, and the `snapshot` doctor line stayed a warning whose fix — a plain
  `onboard` — only reloaded the same verdict. The budget is now `[snapshot]
  max_tokens` in `config.toml` (`aisquare config set snapshot.max_tokens <n>`;
  the default is unchanged), the snapshot records the full-pack and
  compressed-pack sizes it measured alongside the budget, and `onboard` and
  `doctor` print the same sentence with all three numbers and both remedies —
  raise the budget, or add a `.repomixignore` — followed by the `--refresh`
  re-pack that actually re-measures. A `snapshot.json` written by 0.6.0 has no
  numbers to name and says so rather than printing zeros. Read from the config
  file alone, like `[fleet]`: no environment variable, because the config layer
  has no per-key env rung and one knob is not the place to grow one.
- **`[snapshot] ignore`: what a pack leaves out, and a built-in list it
  extends.** Repomix glob patterns, passed to `--ignore`; `aisquare config set
  snapshot.ignore '**/fixtures/**,docs/generated/**'` (a list key now takes
  comma-separated items, and `config get` prints them the same way). The
  built-ins go first whatever the operator sets — `node_modules`, `.venv`,
  `venv`, `.git`, `__pycache__`, `dist`, `build`, `coverage`,
  `.aisquare-worktrees`, `*.worktrees` — plus any nested git repository or
  worktree found below the root, detected by its `.git` entry, so another
  project's checkout is never packed into this one. The repo's `.gitignore` and
  `.repomixignore` still apply, read by Repomix itself; the too-large message
  names both knobs.
- **Over budget even compressed, the snapshot keeps the skeleton instead of
  nothing.** The 150 000 cap mirrors a server cap on a pack that is read into a
  model context. The CLI never does that — `hook session-start` hands the agent
  the skeleton, pack and index *paths* — so the cap bounded no prompt, only
  whether a snapshot existed; and the skeleton (`repomix --compress`) was built
  only when the FULL pack fit, so the repos that most needed one were the only
  ones without it (measured: 10.99M tokens full, 2.03M compressed). Now the
  compressed pack is written as `skeleton.repomix.xml` with its per-file index,
  status `skeleton_only`, every count recorded, any stale full pack removed;
  the session-start directive lists the skeleton and index and omits the full
  pack; `doctor` reports it green as `skeleton only: N tokens, F files indexed;
  full pack skipped over budget B (M tokens)` with no fix, because a fix here
  was the button pressed forever with a green tick. `max_tokens` now gates
  only the full pack. `too_large` survives only as a status loaded from a
  0.6.0 `snapshot.json`.
- **`serverInfo.version` reports this CLI's version.** mcp 1.x filled an
  omitted server version with the SDK's own package version, so clients saw
  `1.29.1` — a number that named nothing of ours — and 2.x sends the empty
  string, which 0.6.0 therefore shipped. `build_server` now passes
  `aisquare-cli`'s own version, pinned by a test over a legacy connection
  where `serverInfo` is mandatory, so an absent identity fails loudly rather
  than reading as `None`. With this, on the 2025-11-25 handshake era,
  `tools/list`, every success result and every error result are identical as
  parsed JSON between 1.x and 2.x, with two exceptions: the crash case and the
  `-32601` code, both described below. (2.x orders object keys differently, so
  the raw frames are not byte-for-byte equal; the error texts themselves are.)
  On the 2026-07-28 era every result also carries a `_meta` serverInfo stamp,
  which this version now populates; no 1.x served that era, so there is
  nothing to compare it with.
- **The serve suite proves what it says it proves.** Three gaps, each of which
  let a mutation pass:
  - `call_remote` drove the server through `Client(server)` at its default
    mode, which for an in-process server is a `DirectDispatcher` pair —
    2026-07-28, no initialize handshake, no JSON-RPC framing — while its
    docstring claimed a wire-shaped round trip. It now asks for
    `mode="legacy"`, the path the removed
    `create_connected_server_and_client_session` took: memory streams, a
    handshake, framing, results sieved at the 2025-11-25 surface. Both it and
    the modern-path test now assert the protocol version they negotiated, so
    swapping either mode fails instead of silently testing the other era.
  - Nothing exercised `run_http` at all. Dropping its `host` argument left
    every test green while `--bind 0.0.0.0` reverted to answering every LAN
    client with `421`. `test_http_answers_by_bind_host_and_token` now pins
    every combination that matters — a LAN `Host` is 200 on `0.0.0.0` and 421
    on `127.0.0.1`, each of the three loopback spellings rejects a LAN `Host`
    and still answers its own client, a `127/8` address outside the tuple is
    served unchecked, and a missing token is 401 on either kind of bind before
    any Host check runs — driven through the ASGI lifespan the way uvicorn
    drives it, so `_BearerGuard`'s lifespan pass-through is pinned along the
    way.
  - The `ClaimLostError` arm of the MCP error guard had no test. It now has
    one, with the truth in its docstring: no tool can reach that arm today —
    `next_task` moves on when a claim is lost and nothing calls `claim_task` —
    so the test pins the mapping for the day a tool claims by ref.
- **The 0.6.0 entry below is corrected in place.** Six of its statements
  about the mcp SDK were measurably wrong — the version range in which the
  loopback protection existed, which transports encode JSON, the scope of a
  wire-parity claim, what a fresh install resolves to, the SDK's own word for
  a 2026-era `_meta`, and which spellings count as loopback. The tag is
  immutable, so the repo's copy is the only one that can be made true, and a
  reader of 0.6.0 looks there rather than here. Everything those follow-ups
  *add* is in this section instead, so 0.6.0 does not advertise behaviour it
  never shipped.

### Fixed
- **The wheel goes to the program that can use it — Claude Code's fullscreen
  TUI first.** The root of "scroll not working" (reported 2026-09-08 from WSL2
  + Windows Terminal). Claude Code's fullscreen TUI turns on the alternate
  screen (`?1049`) and mouse reporting (`?1000` + `?1006`) and scrolls its own
  transcript on the wheel; the agent pane spent every notch on tmux's history
  — which the alternate screen does not have — so nothing moved and the program
  never saw the wheel. The pane now reads tmux's `mouse_any_flag` /
  `mouse_sgr_flag` / `alternate_on` / `pane_in_mode` with every frame and
  routes each notch: a program tracking the mouse gets the notches as the SGR
  (or, as raw bytes via `send-keys -H`, X10) events it asked for, at the
  pointer's pane cell, coalesced into one tmux call per frame; a fullscreen
  program that does not track the mouse is left alone and the user told once
  (arrow keys would land in its prompt); a view already scrolled into history
  always comes back with the wheel; a pane in tmux copy mode stays tmux's;
  everything else scrolls tmux history as before. A pane that dies under the
  wheel reports `(pane gone)` like every other path.
- **Agent panes scroll from the keyboard, and show that they are scrolled.**
  Reported from WSL2 + Windows Terminal (2026-09-08): "scroll not working". The
  wheel was the only way into a pane's history, and a scrolled pane looked
  identical to a quiet live one. Now shift+PgUp / shift+PgDn — or alt+PgUp /
  alt+PgDn, since many terminals keep shift's for their own scrollback — scroll
  a screen at a time, shift+Home and shift+End go to the top and back to live,
  through the same owner decision as the wheel: on a Claude Code pane they
  scroll Claude's transcript rather than pulling stale shell lines over it. A
  `[↑k/history]` marker sits in the top-right corner while the view is in tmux
  history, tracking history that grows under a frozen view, and leaves with
  the offset. None of the keys reach the agent; any other key still returns
  the view to live.
- **Self-invocation is no longer shadowed by a project's own `aisquare/`
  package (#81).** The CLI re-runs itself as `python -m aisquare …` — for
  `init`, `doctor` and `project onboard` from the fleet UI, for every fleet
  window, for the detached distiller, and as the last-resort hook command.
  `-m` puts the current directory first on `sys.path`, so from any repo whose
  root holds a top-level `aisquare/` package — the explainability SDK's own
  repo ships one — every one of those died with `No module named
  aisquare.__main__`, and a fleet window did so even with `PYTHONSAFEPATH`
  exported by the spawner, because a window inherits the tmux server's
  environment. All four now build their argv through one helper that passes
  the interpreter `-P` (the flag form of `PYTHONSAFEPATH`, Python 3.11+): it
  ends with that process, so a coder's own `python -m pytest` inherits
  nothing, and it needs no environment to travel. `aisquare doctor` gains a
  `self-invocation` row that warns when the directory would shadow a
  hand-typed `python -m aisquare`.
- **`doctor` now checks WHICH `aisquare` the Claude Code hooks run, not just
  that hooks are there** (#84). A hook was recognised by its text, so every
  hook on a box could name `…/aisquare-cli/.venv/bin/aisquare` — a 0.3-era
  editable checkout — while the live install was 0.6.0, and `doctor` said
  "all lifecycle hooks installed" for weeks. Per config dir it now resolves the
  program each hook names and compares it to this install by path, or by
  running `<path> --version` when the path differs; a stale, missing or
  unreadable binary turns the row into a warning that names the dir, the
  hook's path and version, this install's path and version, and the one-line
  fix (`aisquare agents connect claude-code --config-dir <dir>`).
- **`doctor` discovers Claude Code config dirs on disk** — `$CLAUDE_CONFIG_DIR`,
  `~/.claude` and every `~/.claude*` whose `settings.json` carries aisquare
  hooks — and grades them the same way, labelled "found on disk, not connected
  in this home". A fresh `AISQUARE_HOME` previously knew no sites, so a
  `~/.claude3` reached through `CLAUDE_CONFIG_DIR` was invisible until someone
  ran `agents connect --config-dir` for it. Still read-only: `doctor` never
  rewrites `settings.json`.
- `--json project list` objects now carry the `name` the table shows, so a
  script can pick a project by name (#83).
- **The selected project row in the fleet sidebar showed its folder glyph and an
  empty highlighted band — no name, no codename.** Any project whose basename is
  wider than the sidebar's title column (25 cells at the default width;
  `AISquare-Explainability-SDK` is 27) was affected, in every theme. The row's
  Rich `Text` asks for `no_wrap` and an ellipsis, but Textual keeps only the
  text and its spans and lets the widget's CSS `text-wrap` decide; its default
  wraps, so the name landed on a second line that the one-line row clipped. The
  sidebar's one-line rows — project title, agent rows, the path subtitle, the
  Doctor lines — now declare `text-wrap: nowrap; text-overflow: ellipsis`, so a
  long name is cut with `…` where it stands. (#86)
- **A retention test went red on `main` on a calendar date, with no code
  change.** `test_snapshot_refs_older_than_the_retention_are_pruned_when_a_new_one_is_taken`
  dated the ref it expects to SURVIVE pruning at a literal `2026-09-02`, five
  days inside the seven-day `WIP_REF_TTL_DAYS` window on the day it landed. On
  2026-09-09 that ref turned exactly seven days old, `_prune` dropped it as
  designed, and the assertion failed — five of the six CI jobs red on an
  unmodified tree, and the first branch to run afterwards wearing the blame.
  The date is now computed as one day before the run. Its sibling `old`
  fixtures stay literal deliberately: they only ever need to be OUTSIDE the
  window, and `2026-01-01` always will be. Checked in the other direction too —
  moving the fixture to eight days ago still fails the test, so the assertion
  is still the one doing the work.

- **The ceiling holds on mcp 2.2.0.** Released after 0.6.0 measured its floor,
  and admitted by the same `>=2.1,<3` pin, so a fresh install already resolves
  to it — CI's `check` jobs install it and are green, which is what proves it
  rather than the local venv, still pinned at 2.1.1.
- **`--show-token` and the startup line print a URL a client can dial.** Both
  interpolated the bind verbatim, so `--bind ::1` — one of the three spellings
  that keep the transport's Host validation — printed `http://::1:8747/mcp`,
  which is not a URL at all, and `--bind 0.0.0.0` printed a listen address no
  client can reach. IPv6 literals are bracketed — that half is unambiguous —
  and a wildcard bind is replaced by this machine's name, which is a better
  starting point than `0.0.0.0` without being a promise: whether that name
  resolves, and to something reachable rather than back to loopback, is the
  operator's network to know. The bind is printed alongside and added to the
  JSON as a `bind` field, so nothing is hidden either way. Pre-existing, but
  newly consequential: before mcp 2 a LAN client was refused with 421 before
  the URL ever mattered.
- **A broken mcp install is no longer reported as the wrong problem.** The
  serve guard had two branches — extra missing, or mcp out of range — and a
  third case fell into the second. `find_spec` on a dotted name imports the
  parents, and `mcp.server` imports `sse_starlette` at package-import time, so
  a venv holding mcp 2.1.1 with `sse-starlette` uninstalled or broken raised
  inside the probe, was read as "no such module", and told the user to install
  the mcp they already had. mcp pins `sse-starlette>=3.0.0` with no upper
  bound, so an ordinary `pip install` can reach this. The guard now reports
  the failing import by name and says to reinstall the extra.
- **`doctor` told new users to install a different project.** Three
  remediations named `aisquare`, which on PyPI is the *Explainability SDK*
  (1.2.0), not this CLI (`aisquare-cli`): `install` on both its branches
  (`pipx install aisquare`), `tiktoken` (`pipx inject aisquare tiktoken` —
  which also names a pipx environment that exists on no machine that followed
  the documented install), and `explainability sdk`
  (`pip install "aisquare[explainability]"`, from a stale constant that
  shadowed the correct, editable-aware hint one import away). So the checks
  whose whole job is "this machine is not set up properly" answered it with
  commands that install somebody else's package — and, because the SDK ships
  its own `aisquare/__init__.py` into the directory this package occupies, into
  the exact dependency shape `pyproject.toml` carries twelve lines warning
  about. Every hint is now built from `core.version.DISTRIBUTION`, and a new
  class-level guard sweeps the real `doctor()` output so a fourth instance
  fails the build instead of shipping. The `--force-reinstall aisquare` row is
  untouched: it repairs the SDK's own package root and means the SDK.
- **`doctor --fix` *ran* the install its own advice forbids.** `install_sdk()`
  shelled out to `pip install aisquare[explainability]` while every printed hint
  was being corrected away from that exact form, so the two halves of one code
  path disagreed. It now installs through our own extra
  (`aisquare-cli[explainability]`), which is also the only form that carries the
  `>=1.1` floor — the SDK release where `AgentRunTracer` accepts `run_id`. The
  bare form had no floor and could resolve an SDK too old for the lane the
  install exists to enable, and succeed while doing it.
- **`explainability sdk` remediations read as self-contradictions on a
  checkout.** `install_hint()` returns a command on a normal install and a
  *sentence* on an editable one, so an unconditional `Install it: …` prefix
  rendered as "Install it: this is an editable checkout — installing the extra
  here shadows it and every command dies…", telling the operator to do the thing
  the rest of the line says will break their machine. The prefix is now
  conditional on there being a command to prefix. Affected all three SDK rows,
  one of them before this release.
- **The `repomix` check was green on a machine that cannot pack a snapshot.**
  `npx` merely *existing* was the whole test, while repomix 1.18.0 declares
  `node >= 22` — and Debian 12 ships Node 18, Ubuntu 22.04 ships 12. On those,
  the line read `ok` and the first `project onboard` failed at run time. The
  check now reads Node's version through a registered spawn seam
  (`core/snapshot.py::node_version`) and warns below the floor, naming the
  version found. The floor is read **per path**: `npx --yes repomix` fetches the
  latest release, so the constant applies there, while an installed `repomix`
  is judged by its own `engines.node` — a pinned `repomix@0.2` on Node 18 packs
  fine and must not be warned about, and a repomix that *raises* its floor must
  not be under-warned. No Node on PATH at all is now its own warning rather
  than "untested", because `repomix` and `npx` are both `#!/usr/bin/env node`
  scripts and neither can run without one; a Node that is present but will not
  report a version stays untested. The advice points at nodejs.org or a version
  manager rather than the package manager whose `nodejs` *is* the old one.
- **`fleet reap --server-down` — the fix `doctor` prescribes now exists.** After
  a reboot (or `kill-server`) `doctor` reported "N recorded live but the private
  tmux server 'asq' is not running" and pointed at `aisquare fleet reap`, which
  reconciled nothing: a server that does not answer is, by design, not evidence
  that its panes are dead. Measured on one box: 10 rows reported, 0 reaped, the
  same advice printed again. The flag is the operator's word that the server is
  genuinely gone — and it acts only where tmux itself says so (`no server running
  on …` / `error connecting to … (No such file or directory)`), never on a
  protocol mismatch after an in-place tmux upgrade, a wedged server or a missing
  binary, all of which hold live agents. One probe per socket per sweep, so a
  server coming up mid-`reap --all` cannot split the answer. `doctor` decides
  with the same predicate, names `reap --all` (its scan is machine-wide) and
  offers the flag only with its condition attached.

## [0.6.0] - 2026-09-03

**The fleet UI: bare `asq` opens one view over every project, agent and
session.** This is the first release where the CLI has a front door — a
two-pane, mouse-driven terminal UI over your projects, the manager agent in
each, and the agents that manager spawns, every one of them a real Claude Code
session you can type into. Everything it does is still a plain command with
`--json`, and both halves below it — memory and orchestration — work exactly as
they did without ever opening it.

Shipping ahead of feature-complete on purpose, to make internal testing easier;
known gaps are listed in `docs/plans/fleet-tui.md` and land as 0.6.x.

### Added
- **The fleet: `asq` with no arguments opens one view over every project,
  agent and session.** A two-pane, mouse-driven UI — a navigator of projects,
  the agents running in each and a Doctor summary on the left; onboarding, a
  project's **manager** (an agent you task in prose, which plans, spawns and
  steers the others), any agent's *real* Claude Code session, the board and
  doctor findings with their fixes on the right. Never a chat relayed through
  us: agents run as windows of a per-project session on a private tmux server
  (`tmux -L asq`, bundled config, tmux ≥ 3.2), so they outlive the UI and
  `aisquare fleet attach` shows the same session from any terminal. New `fleet`
  group — `spawn · ls · status · tell · stop · attach · reap · rename · pause ·
  resume`, `--json` everywhere — and an explicit `ui` command. Fleet roles
  `manager` (the planner with fleet authority), `tester` (the fleet's name for
  `runner`) and `reviewer` (read-only PR review), which `launch` accepts too.
  Store schema v11: `fleet_agent`, and a per-project `codename`
  (`adjective-animal`, deterministic from the project id; `fleet rename`
  changes it) that names the tmux session and the `fleet/<codename>/…`
  branches. A `[fleet]` config section in which every value is a default —
  permission mode `auto` for every role, a worktree per coder and reviewer,
  four agents per project, `F12` as the escape key, Claude's native agent
  teams off in fleet launches — overridable per spawn or
  in config. Scripts, pipes and `--json` callers of bare `aisquare` still get
  usage and exit 2. User guide: `docs/fleet.md`; the plan and its decisions
  log: `docs/plans/fleet-tui.md`. Delivered in phases (plan §9); the plan's
  Decisions log records what has landed.
- **CI runs the suite against a machine that looks like a developer's.** The
  `check` job installs `.[dev]` into a pristine runner — no `~/.aisquare`,
  nothing listening on any port, no optional extra — while anyone who followed
  the setup guide has all three. Three fixtures in this repo were green here and
  red for them, and the third was introduced while fixing the second, which is
  what moves this from "be careful" to "have a job".
  - **Two variants, because the leaks need opposite proxy states.** Measured in
    CI, not assumed: with both regressions reintroduced on a throwaway branch,
    `proxy-down` errored on the three `test_runbook_json_paths.py` tests and
    `proxy-up` on `test_json_stdout_is_empty_or_parseable[proxy-down]` — disjoint,
    neither catching the other's, while all three `check` variants stayed green.
    One configuration would have covered half the class and looked like it
    covered the class.
  - **Both variants assert their own premise, on both sides of the suite**, using
    `aisquare explainability status` rather than a `curl` at a literal port: it
    probes whatever the config says, so it cannot drift from what the suite
    reads, and it checks the `service`/`mode` contract rather than "something
    answered 200". A variant whose distinguishing condition never held, or which
    lost it midway, is a job reporting the other variant twice — so it fails
    instead, before or after the suite, saying which.
  - The ambient environment has **one definition**, exported through
    `$GITHUB_ENV`: `AISQUARE_HOME` plus the explainability variables
    `tests/conftest.py` goes out of its way to clear, whose own comment notes an
    operator's shell has them sourced. A per-step home could be dropped from the
    step that runs pytest, degrading the job to `check` twice, green. Deliberately
    NOT a job-level `env:` block — that was tried and rejected: `runner.temp` is
    unavailable there and GitHub rejects the whole workflow file for it, a
    near-silent failure that yields a run with zero jobs and no log, and reads
    from outside as "CI has not started".
  - **The variant name is checked against a closed set** before anything
    dispatches on it. Every dispatch is `[ "$AMBIENT" = "proxy-up" ]`, so any
    other value — including the empty string, which is what deleting the export
    or renaming the matrix key produces, since an unknown `matrix` property
    expands to `""` with no error — is silently proxy-down, and the matrix reports
    both of its names having run one ambient.
  - It reuses `tests/proxy_stub.py` rather than inlining a server: `probe_proxy`
    checks `service` and `mode`, so a stub is a contract, and the copy nobody
    runs locally is the one that drifts.
  - Two of the three conditions above are reproduced, and the job says so: the
    extra cannot be installed where the suite runs (it shadows an editable
    checkout), so `package` covers that axis at import level instead.
- **The packaging job installs the extra.** `pip install
  "aisquare-cli[explainability]"` is the line the setup guide gives people and
  nothing tested it. The SDK shares this package's top-level import name, so both
  distributions land in one `site-packages/aisquare/` and the last writer wins
  the shared `__init__.py` — a build that read `__version__` off that module died
  at import with the extra installed, and no job would have caught it. The job
  asserts both packages import and names the six `explainability` subcommands,
  because `--version` cannot distinguish a build that has this integration from
  one that does not.
- `tests/test_ci_covers_the_ambient_environment.py` pins all of the above. Its
  checks assert the **order and placement** of things inside the job rather than
  the presence of a string in the file, because an independent review mutated the
  literal version and found five ways to make the job vacuous with every guard
  green — drop the home from the step that matters, hardcode the stub's port,
  delete either premise assertion, run pytest ahead of the listener, or invert
  the variant condition so the two run under each other's names. Each of those is
  caught now, and the edits a maintainer would legitimately make — reordering the
  variants, adding a third, rewriting the matrix in block style — do not cause a
  false failure.

### Changed
- **`aisquare serve` runs on mcp 2.x.** The `serve` and `dev` extras require
  `mcp>=2.1,<3` (was `>=1.10,<2`). mcp 2.0.0 renamed `FastMCP` to `MCPServer` and
  deleted `mcp.server.fastmcp`, the module this CLI imported, so the Dependabot
  bump (#73) went red on mypy and the `<2` pin was the only thing keeping a
  fresh install green. The port is confined to `services/mcp_server.py`, the
  `serve` dependency guard, and the two test files that drive them; the nine
  tools and their wording are unchanged. What a client can observe differently
  is listed below — a second protocol era (2026-07-28, which no 1.x could
  serve), a crash's detail kept off the wire, `serverInfo.version` and
  `-32601` for an unknown method, on both transports — and, on HTTP alone, no
  Host/Origin validation on a non-loopback `--bind`.
  - **The error-wording contract survives, on the seam the SDK now provides.**
    mcp 2 still folds a tool's `ToolError` into `Error executing tool <name>:
    <msg>`, so the handler that unwraps our own message back out is still
    needed. It moves from the removed `_mcp_server.call_tool()` decorator to
    `add_request_handler("tools/call", …)` on `_lowlevel_server`, which is what
    the SDK's own migration guide names for replacing a protocol handler (and
    what the SDK itself uses to wrap this method for extensions). A remote
    agent still sees `error: reopen requires a note (the feedback)`, verbatim,
    as an `isError` result — `tests/test_serve.py` asserts every one of those
    strings through a real client session.
  - **A crashed tool is now logged server-side.** New in mcp 2.1, not chosen
    here: the SDK tells a crash apart from a deliberate failure by type
    (`UnexpectedToolError`) and keeps the crash's detail off the wire, so the
    agent sees `Error executing tool <name>` and nothing else. In 1.x the
    message rode along in the result, which was the only place it went. The
    replacement handler logs the traceback on the server (stderr, which is
    never the protocol channel on either transport) where the SDK's own handler
    would have, so a bug in a tool is still readable somewhere. A rejected
    argument set is the caller's mistake, not a crash, and is not logged as
    one. `test_a_crashed_tool_is_an_error_result_logged_server_side` pins both
    halves: nothing of the exception on the wire, all of it in the log.
  - HTTP transport settings moved off the server object: `host` is passed to
    `streamable_http_app()`, whose only use for it is deciding whether
    DNS-rebinding protection auto-enables, and the port is uvicorn's alone, as
    it already was. **That decision now follows the actual bind, which changes
    one thing on HTTP.** From mcp 1.23.0 (2025-12-02, "Auto-enable DNS
    rebinding protection for localhost servers") the SDK decided in its
    constructor from the default host (`127.0.0.1`), and the pre-change code set
    `settings.host = bind` only afterwards, so Host/Origin validation with a
    loopback-only allowlist was on for every bind — `--bind 0.0.0.0` answered
    every LAN client with `421 Invalid Host header` (measured against the
    pre-change tree on 1.23.0 and 1.29.1). On 1.14 through 1.22 the protection
    defaulted to off and the LAN bind worked (measured on 1.14.0 and 1.22.0);
    below 1.14 this server did not construct at all — `FastMCP` ran
    `issubclass` on the string annotations `from __future__ import annotations`
    leaves behind — so the old `>=1.10` floor was never right either. 1.23.0
    predates both the pin and the module docstring's LAN use case (2026-07), so
    the mcp a fresh install resolves to — the newest the pin admits — never
    supported that use case; an environment already holding a 1.14–1.22 did,
    since pip leaves a satisfied requirement alone. In 2.x a bind spelled
    exactly `127.0.0.1`, `localhost` or `::1` — `LOOPBACK_BINDS` in
    `services/mcp_server.py` — keeps the protection (Host allowlist
    `127.0.0.1:*`, `localhost:*`, `[::1]:*`); anything else — `0.0.0.0`, a LAN
    address, another `127/8` address such as `127.0.0.2`, even `LOCALHOST` or a
    hosts-file alias, since the match is on the string — runs with no
    Host/Origin validation, so LAN clients work and the bearer token is the
    sole gate there. It is checked outermost, before anything else in the app,
    and a DNS-rebinding page cannot present it, which is why that trade is
    acceptable — with one caveat the operator has to own: the token is a
    long-lived credential (`auth rotate` is still a stub) sent in clear over
    plain HTTP on every request, so a non-loopback bind belongs on a trusted
    network or behind a TLS-terminating proxy — which nothing in this release
    says outside this entry; see `[Unreleased]`. An operator who wants a Host
    allowlist on such a bind as well passes
    `transport_security=TransportSecuritySettings(...)` (from
    `mcp.server.transport_security`) to `streamable_http_app()` for that bind
    only — supplying it replaces the SDK's loopback default rather than
    extending it. Found by an independent review of this release after it
    shipped, which measured both trees.
  - The `serve` guard probes `mcp.server.mcpserver`, and its message for an
    incompatible major points the other way now — a 1.x is the one that cannot
    work — with `pip install 'mcp>=2.1,<3'`. The distribution-versus-module
    distinction it was written for (#55) is exactly what makes a 1.x a
    sentence rather than a traceback. It tells majors apart, not minors: the
    pin is what keeps a 2.0.x out, and pip reports that at install time.
  - `tests/test_serve.py` drives the server through `mcp.client.Client`, the
    in-memory replacement for the removed
    `create_connected_server_and_client_session`, and reads `is_error`: field
    names are snake_case in 2.x. (Which of the SDK's two in-memory paths that
    takes, and why it matters, is a correction made under `[Unreleased]`.)
  - Also inherited from 2.x, and not the project's to change: a server with no
    version of its own reports an empty `serverInfo.version`, where 1.x
    substituted the SDK's own package version (corrected under `[Unreleased]`);
    a request for an unknown method is answered with the JSON-RPC-specified
    `-32601 Method not found` (was `-32602 Invalid request parameters`); and
    synchronous tool bodies run on a worker thread rather than inline on the
    event loop. Each of the nine opens its own store session per call and
    touches nothing thread-affine, so nothing crosses.
  - The floor is measured, not guessed: against every 2.x release on PyPI at
    the time, the serve suite, the stdio idle-deadline suite and mypy strict
    are green on 2.1.0 and 2.1.1, and 2.0.0 and 2.0.1 fail on the
    `UnexpectedToolError` import — the distinction above did not exist yet, so
    `>=2.1`. (2.2.0 has since shipped inside the same `<3` ceiling; see
    `[Unreleased]`.)


## [0.5.0] - 2026-08-27

First release carrying the explainability integration. 0.4.0rc2 shipped from
`main` before any of it landed, so this is the first version a developer can
`pip install` and connect.

**The CLI sends `X-AISquare-Key`, so a hosted proxy works and no local one is
needed.** Session wiring emitted `X-Agent-Name` and `X-Pipeline-Id` and never the
workspace key — which a hosted proxy authenticates on and *is* the tenant for. So
tracing could only ever reach a loopback sidecar, and every developer had to
install the SDK, start a process, and keep it alive across reboots.

A hosted proxy is already deployed for both deployments
(`explainability-api.aisquare.studio:9443`, and the `stg-` equivalent). Pointing
at one is now a flag — `enable --proxy-url
https://explainability-api.aisquare.studio:9443`.

Verified end to end against production: three real `claude` sessions traced
through the hosted prod proxy with nothing listening on 9090, a board note
shipped through the client lane, and `doctor --live` green on proxy, gateway and
ingest.

The key is resolved through the ACTIVE TARGET and passed into `wire_session`,
never resolved inside it. Resolving locally means `resolve_api_key()`, which is
env-first over a hardcoded `EXPLAINABILITY_API_KEY` — correct only when the
target names that variable, and the source of the incident where a staging key
reached a prod gateway. `tests/test_one_key_resolver.py`'s AST guard caught the
first attempt doing exactly that.

**Where the key ends up is a deliberate trade, recorded here rather than left to
be discovered.** The header is exported into the launched agent's environment, so
the agent and every subprocess, MCP server and tool it starts can read it, and
`explainability env` prints it because its output exists to be `eval`'d. It is a
write-scoped ingest credential — it sends spans and reads nothing — which is what
makes that acceptable. Three things bound it: the proxy strips the header before
forwarding upstream, `spawn.TRACING_ENV_VARS` already keeps it out of aisquare's
own subprocesses, and a non-loopback proxy without `https` is refused rather than
traced. A local proxy needs no key at all.

Onboarding drops from eleven steps to ten, and daily use to `aisquare launch`.
A local sidecar is still fully supported — `--proxy-url http://127.0.0.1:9090`,
which needs no code — for model traffic that must not leave the machine, or a
self-hosted deployment with no proxy tier.

### Added
- **`docs/planner-findings-loop.md` — the find→fix loop, and the one thing
  that blocks it.** The write half is done: a traced session opens a Run keyed
  by an id the board also knows, so a finding can be traced back to the
  session, the role and the task that was open at the time. The read half needs
  a read-scoped credential, and the page makes that a five-minute unblock
  rather than a morning of discovery — it carries the falsified hypotheses (the
  403 is not about which studio is pinned), the exact env names to add, the
  gateway routes confirmed to exist, and the loop step to paste into the
  planner's prompt. The loop is driven from our own `joins.jsonl` rather than
  by polling the gateway, because `runs` has no `since` and we already know
  every Run we started. A test pins the page's field table against what
  `record_join` actually writes, in both directions, and is verified to fail
  when a row is renamed.
- **The correlation spine: one session, one Run, one key.** Tracing already
  sent an `X-Pipeline-Id`, but it was a random UUID — so a gateway Run and the
  board row for the very same session had nothing in common, and the two
  datasets could not be joined at all. The board keys a session by the id the
  *agent* reports, which means the launcher is the only place the two can be
  made equal: it now mints the id, starts the agent on it
  (`claude --session-id <uuid>`), and traces under that same id.
  - Applies to `aisquare launch`, `aisquare team spawn --exec`, **and** the
    printed `team spawn` command — the default, and the one a human actually
    pastes. The printed form takes its id from the same run-time `eval`, so it
    is still fresh per paste; nothing is ever burned into the banner.
  - Only when tracing is enabled **and** the wiring actually traced. With
    tracing off (the default) the argv is byte-identical to before, and an
    untraced fallback is exactly the launch you would have got anyway — an id
    pinned on a launch with no Run to join is risk bought for nothing.
  - Skipped, loudly, where it cannot be honoured: `--continue` and a bare
    `--resume` name a session that does not exist yet, and an agent that is
    not `claude` (or an install named after it) may not know the flag. Those
    still trace, unjoined, with the reason on stderr — a flag the agent
    rejects would cost the launch, and nothing may. `AISQUARE_PIN_SESSION_ID=0`
    opts out entirely.
  - A `--session-id` or `--resume <id>` you passed yourself is read, never
    doubled: your id is already the board's.
  - Every traced launch appends one JSON line to
    `~/.aisquare/explainability/joins.jsonl` — session id, agent name,
    pipeline id, started at — so board events can be joined to Runs without
    dashboard access. Unwritable log ⇒ a warning, never a failed launch.
- **`config.redaction.level` finally does something, and what it does is keep
  a pasted credential off the network.** The setting has existed since the
  first release with nothing reading it — so `strict` changed no behaviour
  anywhere, which is worse than having no setting, because an operator who set
  it believed they were protected. It is now honoured on the explainability
  shipping path: prompts and board events are scrubbed on their way into the
  spool, before anything is written to a file whose purpose is to be uploaded.
  - `off` ships as typed. `standard` (the default) removes credentials — vendor
    token shapes (`sk-`, `ghp_`, `glpat-`, `xox*-`, `AKIA`, `AIza`), JWTs, PEM
    private-key blocks, `Authorization`/`Bearer` values, `NAME=value` where the
    name says secret, and `user:pass@host` in a URL. `strict` adds identity:
    email addresses, and `/home/<user>` → `~`.
  - `standard` deliberately keeps file paths, hostnames and ports. A pasted key
    is an incident; a path is the substance of an engineering prompt, and
    redacting those by default would gut the dataset in exchange for a risk
    nobody has articulated. An over-match is a sentence the dataset cannot
    learn from, so a test pins that ordinary prose comes back byte-identical.
  - An assignment keeps its key name (`EXPLAINABILITY_API_KEY=[redacted]`), and
    every removal is marked — a silent scrub is indistinguishable from a user
    who typed nothing.
  - **Local capture is untouched.** `aisquare log` and the board row keep
    exactly what was typed; this is about what crosses the network, and
    rewriting someone's own history would make it useless for the debugging it
    exists to support.
  - The `init` consent line now names the level, so whoever says yes learns
    what leaves the machine.
- **The tracing boundary, written down before anyone measures against it**
  (`docs/explainability-tracing-boundary.md`). A Run is a **process**, not an
  agent: identity rides in process-level environment (`ANTHROPIC_BASE_URL` +
  `ANTHROPIC_CUSTOM_HEADERS`), so an in-process Claude Code Task subagent or
  Workflow step inherits the parent's identity verbatim and cannot carry its
  own. Per-role and per-session numbers are real and verified against staging;
  per-subagent numbers **do not exist**, and a query that appears to return one
  is reading root-level spans and attributing them to whichever subagent the
  reader assumed — a plausible number rather than an error, which is why this
  is a data-correctness note and not a docs nicety. Task fan-out is countable
  (`Tool:Agent` spans); a Workflow's is not recoverable at all. Separation
  needs a separate **process**, which is exactly what `aisquare launch` and
  `aisquare team spawn` give you. A test pins the page's mechanical claim
  against the code, so it cannot rot quietly.
  - **`aisquare explainability status` and `doctor` state the active level**,
    status directly under the spool counts — "how much am I sending" and "what
    is in it" are one question. Both surfaces render the same sentence from one
    source so they cannot drift, and both say plainly that the scrub applies to
    what LEAVES: local capture keeps what you typed. `off` renders as the
    setting it is, never as a failed check — doctor makes decisions visible, it
    does not overrule them. The setting spent its whole life being read by
    nothing, so being able to SEE it is what makes it trustworthy.
- **`aisquare explainability status` honours `--json`.** It printed human text
  under `--json` while `team status` and `explainability env` both returned
  real JSON — and this is the command a cutover gets scripted against, so every
  check in the runbook was a grep against prose. The payload carries every
  field the human view shows; `key` splits into `key_env`/`key_set` (never the
  key itself) and the spool counts nest under `shipping` as numbers. A test
  compares the two views so one cannot quietly gain a field the other lacks.

### Fixed
- **Two test fixtures read the developer's real machine, and a running proxy
  broke both.** `test_json_stdout_is_machine_readable`'s proxy-down state
  asserted its own premise against the *configured* proxy URL — which is 9090,
  the port its own docstring says to avoid because it is "somebody else's
  long-running proxy". It now points that state at a privileged port nothing can
  be listening on. Both leaks were invisible on CI and fire on any machine that
  has followed the onboarding runbook, which is now a single command.
- **`tests/test_runbook_json_paths.py` read the developer's real
  `~/.aisquare`.** Its payload fixture was module-scoped, which runs outside the
  function-scoped `isolated_home` isolation, so it sampled whatever the machine
  happened to hold while its docstring claimed "a machine with nothing
  configured". Cold on CI, which is why it stayed green — and on a machine that
  had followed the onboarding runbook, a stopped proxy made `status` exit 1 and
  errored three tests that have nothing to do with proxies.
- **`doctor --fix` could brick the checkout it was run in — including the test
  suite's own interpreter.** The entry below documents that hazard and warns
  about it; `apply_fixes` then went ahead and performed exactly that install,
  because `running_editable()` was wired to the paths that *advise* an install
  and not to the one that *performs* one. On an editable checkout the install is
  now refused outright, ahead of the consent check: `--yes` is consent to a
  repair, and a CLI that can no longer start is not one.
  - The suite is a caller. `doctor --fix --yes` appears in four tests, so pytest
    pip-installed the SDK into `sys.executable` mid-run, over the network, and
    every test spawning a subprocess afterwards graded a shadowed CLI — 15
    failures, all of them collected after the test that caused it, none of them
    in it. The environment stayed broken after pytest exited.
  - `pytest_sessionfinish` now fails any run that ends with a distribution
    installed into its own interpreter that was absent at the start. Not
    specific to pip or to that command: anything writing a distribution there
    invalidates the whole run, and the honest report is "these results do not
    describe this tree" rather than one unlucky test's traceback.
  - Three tests were passing for reasons nobody chose, and now state their
    premises instead of inheriting them: the `--reinit` help assertion read a
    Rich options panel that TRUNCATES below ~70 columns (green at 80, red at 60
    — and CI runs narrower than a developer's terminal); the "healthy install"
    doctor control asserted `ok` against whatever the ambient interpreter held,
    which was `ok` only because an earlier test had installed the SDK, making
    alphabetical order load-bearing; and the `pgrep` decoy was a single-command
    `sh -c`, which dash execs, replacing the shell's argv — the part holding the
    phrase the decoy exists to supply — with `sleep 30`.
  - `test_import_cost_of_the_integration` no longer swallows its subprocess's
    stderr. `check=True` reports the return code and discards the message, so a
    red CI said "returned non-zero exit status 1" while the interpreter had been
    printing the root cause all along.
- **Our own install advice could brick an editable checkout.** On a normal
  install `aisquare-cli[explainability]` is safe: both distributions land in one
  site-packages directory, their subpackages merge, and only the top-level
  `__init__.py` collides — which the CLI survives. An EDITABLE install differs
  in kind: the editable hook is a `.pth` line appending the checkout's `src/` to
  `sys.path`, and site-packages is searched FIRST, so the SDK's real `aisquare/`
  package does not merge with the checkout — it shadows it, and every command
  dies with `ModuleNotFoundError: No module named 'aisquare.cli'`.
  - Measured in all three directions: reinstalling editable does **not** recover
    it, only `pip uninstall aisquare` does, and a non-editable install with the
    extra is unaffected.
  - The advice now depends on the install shape. An editable checkout is told
    what would happen, the exact symptom to search for, and the one command that
    recovers it. This is the only moment the warning can be delivered — once the
    extra is in, the CLI cannot start, so no check of ours would ever run to
    explain it.
- **The two explainability lanes could point at different deployments, and
  `status` reported the wrong one.** The proxy lane resolves a target —
  `enable --target prod --gateway-url … --key-env PROD_KEY` — and `status` and
  `doctor` report what it resolves to. The client lane (the spool and
  `explainability ship`) did not: it read the top-level `gateway_url` and a
  hardcoded `EXPLAINABILITY_API_KEY`, ignoring the active target.
  - That splits at the moment it costs most. Configure shipping while a staging
    shell is sourced — which is what the cutover runbook has you do — then
    switch the proxy lane to prod: model traffic goes to prod, CLI insights keep
    going to staging, and `status` prints the prod gateway because the line a
    human reads resolves the target. Both halves look healthy and nobody is
    told.
  - Shipping now resolves through the active target, so one switch moves both
    lanes. The key comes from the variable the target NAMES; a differently
    named key in the shell no longer satisfies it, because shipping prod
    sessions with a staging key is worse than not shipping them — it refuses
    and says which variable it wanted.
  - **Every "on" state now names the destination**: `shipping: on →
    https://prod.example — …`, and `--json` carries `shipping.gateway`. Counts
    alone cannot reveal a split brain — "2 sent" reads identically whichever
    gateway it went to — and the state that matters most mid-cutover is
    "buffering", not the happy one.
  - A machine that never made a target is unaffected: the top-level
    `gateway_url` and the stored key file remain the fallback.
  - **The stored key file no longer crosses deployments.**
    `~/.aisquare/explainability-key` holds ONE unlabelled key, which is right
    for the single-deployment machine `init --explainability` produces and
    wrong the moment a target names its own variable. Follow the CLI's own
    "or write \<key file\>" advice while on staging, switch to prod with
    `PROD_KEY` unset, and the STAGING key was handed to the PROD gateway — the
    reverse being worse, a prod key disclosed to a staging host. The file now
    answers only when the active target has not named a variable of its own,
    and the refusal stops advising a file it would ignore.
- **Concurrent first opens of a fresh store could corrupt the migration,
  permanently.** Several sessions launching together onto a machine that has
  never run aisquare could raise a NON-transient `duplicate column name:
  account` out of `_migrate` — and the damage did not heal: the column existed
  while `user_version` still read 8, so every later attempt at migration 8
  failed on that database forever.
  - Time-of-check / time-of-use. The version was read, the migration chosen,
    and only THEN the transaction started — so another opener could advance the
    schema in between and this one applied an **old migration to a newer
    database**. Instrumentation caught a thread running migration index 9
    against a database that read version 8 on two independent connections.
  - Fixed by taking the write lock first and re-reading the version **under**
    it. `executescript` cannot be used for the transactional part — it issues an
    implicit `COMMIT` before running, releasing a lock taken beforehand — so
    statements are split with `sqlite3.complete_statement`, SQLite's own
    tokenizer, and a test compares the resulting schema against what
    `executescript` built, object for object: 29 objects, identical.
  - The guard asserts the invariant, not the race: reproducing the failure needs
    luck (0–2 of 15 twelve-way races), so a racing test would be the
    load-sensitive kind this suite has twice had to repair. It traces a real
    first open and pins that the version is re-read after every write lock and
    before any DDL — verified red against the pre-fix ordering.
  - `docs/store-migration-race.md` records the two hypotheses that were wrong
    (`executescript` breaking the transaction; the connections disagreeing about
    journal mode), both measured and both falsified, so the route is not
    rediscovered.
- **`aisquare doctor --live` now probes a proxy you configured, even with
  tracing off.** With tracing off nothing probes the proxy, which is right for
  the default case and wrong for the flag whose entire meaning is "make the
  network calls": mid-cutover there was no way to confirm the proxy you just
  started answers *before* enabling tracing. Under `--live` it is probed and
  reported informationally — **never as a failure**, because nothing is being
  traced so nothing is broken — and each answer carries what it means rather
  than only what happened. An **unconfigured** default is still never dialled,
  `--live` or not: nobody asked about that address, and a test forbids the
  socket. Plain `doctor`, plain `status` and the tracing-on red path are
  unchanged in every state.
- **Rich was deleting bracketed text out of everything the CLI printed.** Rich
  reads `[...]` as a style tag and removes it, and almost every line this CLI
  prints interpolates data it does not control — paths, git refs, role names,
  config values, binary names, URLs, remembered context text. Two independent
  lanes hit it the same night from different directions: the serve hint reached
  users as `pip install 'aisquare-cli'` with the extra name gone, and the
  doctor's detail column ate the SDK's `[present]` so a configured key read
  exactly like a missing one. Neither raised — both printed a confident wrong
  answer, which is worse.
  - Fixed once, at the console factories, so the safe behaviour is what the
    next call site inherits rather than something ninety of them each have to
    remember. An AST scan counted **87 render sites carrying interpolated
    data**; all are covered by construction. It reaches Rich **tables** too,
    which parse cell text the same way — `aisquare context list` was mangling
    remembered entries.
  - **Deliberate styling is untouched.** `style=` arguments, `Column(style=…)`,
    `header_style` and `rich.text.Text` all bypass the markup parser. The six
    sites that styled text with inline tags now carry that styling structurally
    instead, so the data never reaches a parser — and a test asserts a styled
    line is still styled, on the ANSI Rich actually emits.
  - A test walks the package AST and fails if a `Console` is built outside the
    factories, because that is the one way the default gets bypassed.
- **A machine that never configured tracing reported a failure it did not
  have.** `aisquare explainability status` printed `probe: proxy unreachable at
  http://127.0.0.1:9090/health: <urlopen error [Errno 111] Connection refused>`
  on a stock install. Nothing was wrong with that machine: the shipped default
  points at loopback and nothing is listening, which is exactly right for an
  install that has never asked for tracing. But it read as broken, and the
  first thing anyone does with a line like that is go debug a proxy that was
  never meant to exist yet.
  - The line now distinguishes **not configured** (informational — the default
    is not consulted while tracing is off) from **configured and down**
    (unmistakably red, and still carrying its remediation, because launches
    keep working while silently going untraced). A cold `status` also stops
    dialling the default address at all: nothing to probe means nothing to wait
    for.
  - `status` and `doctor` now render **one sentence from one function**. They
    had already drifted — doctor knew to stay quiet while tracing was off and
    status did not — so the same machine read green in one surface and broken
    in the other.
  - The default `proxy_url` is unchanged and the exit-code rule is unchanged:
    non-zero only when tracing is on and the proxy would not take a session.
    The default being unreachable was never the bug; the wording was.
- **Model probes, gbrain and the detached distiller inherited the launching
  session's tracing identity.** Identity is process-level — it rides in
  `ANTHROPIC_BASE_URL` and `ANTHROPIC_CUSTOM_HEADERS` — and a child gets the
  parent's environment unless told otherwise. So `team spawn`'s availability
  probe, which runs a real `claude -p` per alias, posted a Run wearing
  whichever role happened to be probing: junk data in the dataset, attributed
  to a teammate who never asked a question. Fixed at the source rather than
  leaning on the proxy's junk-run suppression, because the traffic is ours not
  to send. gbrain gets the same treatment — its own env builder already guards
  `ANTHROPIC_API_KEY`, which is the tell that an Anthropic path exists — as
  does the detached `team distill` worker, which outlives the process that
  started it and could otherwise attach to a Run that had already ended.
  Credentials and `PATH` still travel; the strip is only the identity.
- **Every process this CLI starts now carries a written tracing ruling, and it
  is enforced.** `core/spawn.py` holds the inventory — all eleven
  `subprocess`/`exec` call sites, each `traced` or `excluded` with a reason —
  and a guard test walks the package's AST on every run, failing when a call
  site exists that the registry has not ruled on. A docstring inventory drifts
  silently the first time someone adds a `subprocess.run`; this one fails the
  build. Recorded alongside it: Claude Code subagents and Workflow agents run
  *in-process* and inherit their session's environment verbatim, so they
  collapse into the parent's identity. Process is the identity boundary, and
  no launcher change can move it.
- **A proxy URL the agent cannot parse is now refused before it can reach
  one.** `ANTHROPIC_BASE_URL` is the one value in this wiring that costs a
  *launch* rather than a trace: the agent parses it before it can report
  anything, so a malformed one dies at the first request with `API Error:
  Invalid URL` and exit 1. `wire_session` now checks the value it is about to
  set — scheme and host, nothing about reachability, which is still the
  probe's job — and launches untraced with the reason instead. The check is
  deliberately independent of the probe: the probe *happened* to reject an
  unparseable URL as "unreachable", which is both a misleading message (it
  blames the network for a typo in config) and an accident a caller with its
  own `prober` sails straight past. Refused, never repaired — a value we
  invented is a value nobody configured.
- **A corrupt `ANTHROPIC_BASE_URL` already in your environment is now named
  before it kills the launch.** That one is *not* ours to remove — overriding
  the operator's routing is forbidden, and we cannot know it is wrong for them
  — so we still stand down. But the agent is about to fail with a message that
  points nowhere near the cause, so the stand-down now says which value it
  deferred to and that it will not work. Stale shells from before the quoting
  fix are exactly this case.
- **The launcher was about to write a variable the SDK routes on.** Our
  identity marker was called `AISQUARE_AGENT_NAME` — which the Explainability
  SDK already reads as the registered routing identity, and which operators
  set in their own env file. This module even had a constant for it already,
  beside the gateway URL and the API key. Setting it from the launcher would
  have silently overridden the operator's routing, the exact thing the
  reserved-var guard refuses to do for `ANTHROPIC_*`. The marker is now
  `AISQUARE_TRACE_AGENT_NAME`, unambiguously ours, and a test pins that the
  two are different and that the SDK's variable is never written.
- **The run-key marker is named for what it holds.**
  `AISQUARE_SESSION_ID` became `AISQUARE_PIPELINE_ID`. The old name is what
  let a careful reader key spans on it as though it were the board's session
  id — which it is not on any launch that could not be pinned, so those spans
  opened a second Run beside the model traffic. Renamed in the same commit as
  `core.insights.RUN_KEY_ENV_VAR`, which duplicates it to stay off the heavy
  import path; the drift test between them guarantees the pair moves together.
- **Every agent below the first was launching under its PARENT's identity.**
  A traced session's environment carries the wiring that traced it, so
  `aisquare launch` run from inside one hit the "not overriding your routing"
  guard, reported *untraced* — and then handed the child the parent's
  `X-Pipeline-Id` anyway, because standing down leaves the inherited variables
  in place. So the child was not untraced at all: its traffic was filed into
  the parent's Run under the parent's role. That is the whole shape of the
  morning's collective-intelligence work — agents spawning agents — and it
  would have produced one Run wearing one identity for an entire tree.
  A parent's identity is now disowned before the child wires its own, at both
  launch seams. Only ever *ours*: a gateway the operator exported has no
  marker beside it, is not ours, and still makes us stand down untouched.
- **A role bound to a wrapper is now joined, not just traced.** The
  session→Run join moved off the launcher and onto the hook that runs *inside*
  the agent — the one place that holds both halves, since Claude Code hands it
  the board session id and the launcher left the pipeline id in the
  environment. It needs nothing from the binary, so a wrapper that has never
  heard of `--session-id` joins exactly like the default agent. Pinning the id
  with `--session-id` survives as a strict extra for the one program verified
  to accept it, narrowed from "anything named claude*" to exactly `claude`,
  because since #57 an unknown flag can be a dead launch and the hook seam
  already guarantees the join. One row per session, both halves always real.
- **`aisquare launch` ignored the active target's overrides.**
  `explainability enable --target prod --proxy-url …` writes per target, and
  the wiring only ever read the top level — so a launch silently used the
  wrong proxy while reporting success, which is worse than config that is
  plainly absent. Both launch seams now fold the active target down first, and
  a broken target definition costs the override rather than the launch.
- **A pruned-but-alive session stayed invisible while its write path kept
  working** (#47). A live session whose wakeup cadence stretched past the stale
  threshold got retired by `team prune` — and then never came back, because
  only `SessionStart` cleared `ended_at` while every subsequent proof of life
  (prompt heartbeat, end of turn, permission prompt) went through writes that
  did not. Meanwhile its notes landed with verifiable receipts, `team role`
  succeeded and its claims held, so `board`, `team status`, `watch` and
  `doctor` — all of which read liveness as `ended_at IS NULL` — showed nothing
  while the session worked on. Operators read row-absence as death: on the
  board that filed this, one healthy session was pruned on a cadence artifact
  and then presumed dead a second time *because* the severed row masked its own
  recovery. `end_session` had documented the repair all along ("a wrongly
  retired presence row is repaired by the session's next heartbeat"); now it
  happens. A heartbeat is evidence and prune's retirement was an inference from
  silence, so the evidence wins — and the restore keeps the row's role, label
  and focus rather than letting a planner rejoin as `unassigned`. Nothing
  resurrects on its own: a session that really ended stays ended, and prune
  still retires a row that has genuinely gone quiet.
- **The tracing exports were bash-only, and silently misattributed every
  session started from `/bin/sh`.** `aisquare explainability env` quoted with
  bash's `$'…'`, which dash — `/bin/sh` on Debian and Ubuntu — does not treat
  as special: the value arrived with a literal `$` in front and a literal
  backslash-n where the header separator belongs. The proxy then read one
  glued header, never saw `X-Pipeline-Id`, and filed the run under its default
  identity — the exact misattribution that command exists to prevent. Now
  POSIX single-quoted, which carries a real newline in `sh`, `bash` and `zsh`
  alike. The old test pinned the *quoting syntax*, so it passed while the
  premise was false; it now pins the round trip through a real `/bin/sh`.
- **Two spawn commands pasted into one terminal merged into a single Run.**
  The first `eval` exports `ANTHROPIC_*` into the shell, so the second one
  correctly refused to clobber what looks like the operator's own routing —
  and the second agent inherited the first's `X-Pipeline-Id` verbatim. Two
  sessions, one Run, silently; and this is the up-arrow flow, run every time
  an agent exits. The printed command now clears the previous paste's tracing
  first, keyed on a marker only our own wiring sets, so a real operator
  gateway still stops the trace exactly as before.

## [0.4.0rc2] - 2026-08-19

Two PRs on top of rc1. **#48 makes `aisquare` run on Windows at all** — the
package died on `import fcntl` before it could print `--version`, and four more
defects sat underneath that one; read the migration note under Fixed, because
hooks installed by rc1 carry broken quoting and need one `agents connect` to
become runnable. #56 adds the per-role launch profile, folding #52 + #54's
narrower `team.bins` into a single `team.profiles.<role>` map before it reached
a release. Windows is not in the CI matrix yet — the Windows branches read
`sys.platform` at call time and are exercised by monkeypatched tests on ubuntu,
but pre-existing POSIX-only assumptions in the suite need fixing before a
`windows-latest` job can go green.

### Added
- **Per-role LAUNCH PROFILE — the third launch axis, and deliberately the
  dumbest one.** The ladder decides *what* model a role runs on, `--bin` (#52)
  decides *which* executable runs it, and a profile carries *whatever else* the
  operator wants on the command — verbatim. Three axes because they change for
  three different reasons; **one config map**, because they describe one role.
  - `aisquare team bind <role> [--bin CMD] [--env KEY=VALUE ...] [--arg ARG ...]`
    is the one-time setup, with `--unset KEY`, `--clear`, and a bare
    `aisquare team bind` to print the bindings. Everything a role launches with
    is stored under `team.profiles.<role>` — `bin`, `env`, `args`. #52's
    narrower `team.bins` (role → executable) was a strict subset of
    `profiles.<role>.bin`, so it is **deleted rather than deprecated**: it
    reached no release, no config file anywhere holds the key, and a
    hand-written one still loads because unknown keys are ignored. One map is
    one place to look, no precedence rule to learn, and nowhere for a `--clear`
    to leave an entry still steering the role.
  - `aisquare launch <role>` and `aisquare team spawn <role>` carry the binding
    with no flag; `--env KEY=VALUE` (repeatable) adds to or overrides it for a
    single launch. Env merges **per key**, so one variable can be changed
    without discarding its siblings; args **append**.
  - Values may use `~` and `$VAR`, expanded at launch — so one binding follows
    you across machines with different homes. An undefined `$VAR` is left
    verbatim rather than blanked, because a silently empty `CLAUDE_CONFIG_DIR`
    starts a fresh unauthenticated profile that surfaces as a login failure
    hours later instead of the typo it is.
  - **Nothing here interprets what you bind.** Parallel agent installs reached
    through shell aliases are just two env entries; a proxy, a region, or a
    wrapper's own variables work identically, without the CLI learning about
    any of them. Reaching these installs via `--bin` cannot work — an alias is
    not an executable, so `shutil.which("claude2")` is `None`.
  - `team harness` and `spawn`'s banner report which env keys a role carries
    and where each came from (keys only — the values are paths and tokens, and
    a banner is a terminal).

### Fixed
- **`aisquare` runs on Windows (#48).** `core/brain.py` imported `fcntl` at
  module scope and sits on the import path of every command, so a Windows
  install died before it could print `--version` — and fixing that exposed four
  more defects underneath, each independently breaking a feature. Five fixes,
  one commit each, POSIX behaviour unchanged throughout:
  - The brain lock goes through a platform-appropriate primitive — a
    non-blocking `msvcrt` byte-range lock there, `flock` here — behind one
    contract both backends share.
  - Hook commands are quoted for the shell that will actually run them, and
    the matcher that recognises them is the exact inverse. Those two halves
    disagreeing was a two-sided bug: `shlex.quote` wrapped every Windows path
    in single quotes `cmd.exe` has no syntax for, so no hook could launch,
    while `shlex.split` ate the path separators as escapes, so
    `hooks_installed()` always returned `False` — `doctor` reported hooks
    "missing or outdated" with all five sitting in `settings.json`, `connect`
    appended duplicates and `disconnect` could remove nothing.
  - `repomix`/`npx` run through the path `shutil.which` already resolved.
    `CreateProcess` does not apply `PATHEXT`, so a bare name raised
    `FileNotFoundError` and `project onboard` could never pack — which also
    makes `doctor` honest, since it probed with `shutil.which` alone and
    reported repomix available on a machine where packing could not work.
  - A redirected console is reconfigured to UTF-8. Windows streams fall back
    to the ANSI codepage when not attached to a console, which cannot encode
    the `✓`/`⚠`/`→` this CLI prints, so `aisquare doctor > out.txt` exited 1
    on `UnicodeEncodeError` while the same command run interactively was fine.
  - Every `subprocess.run` capturing text decodes as UTF-8 with
    `errors="replace"` rather than the locale codec, which raised
    `UnicodeDecodeError` mid-pack and silently lost repomix's token count.

  *Migration:* hooks installed by an earlier release carry the broken quoting
  and are not runnable. `doctor` now recognises them and reports them
  connected, so re-run `aisquare agents connect claude-code` once to rewrite
  them.
- **`team prune` no longer releases a quiet session's in-progress claim (#49).**
  Presence and ownership now retire on different clocks: the session row still
  goes at the threshold (30m), but its `doing` claims are only returned to the
  pool after 4h of silence. For an agent, thirty minutes of silence is not
  idleness — it is one long tool call, and nothing on the board distinguishes
  that from a crashed terminal. Retiring presence early is self-healing (the
  next heartbeat re-registers the session); releasing a claim early is not,
  because a second agent picks up work the first is still doing. Pass
  `--release-claims` to orphan claims at the presence threshold when you know
  the sessions are dead. `ContextStore.end_session` gains `release_claims`.
- **`save_config` could not write an unset optional field.** TOML has no null,
  so `tomli_w` raises `TypeError` on `None` rather than writing anything — one
  optional field left unset made the whole config file unwritable. Now dumped
  with `exclude_none`, which is also the correct round-trip: the omitted key
  reloads as the model default.
- **`aisquare launch` rejected numbered seats.** A crew running `coder1`,
  `coder2`, … in the same role could not launch: the role whitelist held
  exactly three names. It now accepts a first-class role, a numbered seat of
  one (`coder1`, `validator2`), or any role bound with `team bind` — while
  still refusing a typo like `codr`, which was the footgun the whitelist
  existed to catch.

## [0.4.0rc1] - 2026-08-07

The rc/v2026.08.08 train: everything pending folded into one release —
PRs #39 + #35 (deps/CI unblockers), #38 (shared-session-row banner,
fixes #37), #41 (worktree context + session accounts), #40 (surface cut +
`aisquare launch` + multi-account), #36 (the agent harness), and
#44 + #45 (config-gated session tracing, wired at `launch` and both
`spawn` exits). Review fixes were carried on the folds and are called out
in the bullets; **the rewritten role work-cycles under Changed are a live
behavior change** for existing planner/coder/runner sessions.

### Added
- `aisquare team prune` — retire ghost sessions and return their orphaned
  claims to the pool (#18).
- `aisquare serve --stdio` gains an idle deadline — `--close-after SECONDS`
  (env `AISQUARE_SERVE_CLOSE_AFTER`, flag wins; default 300; `0` = run
  forever): the daemon exits 0 on its own once no client message has arrived
  for that long, so clients killed mid-handshake can no longer strand
  orphaned daemons (#19). Pipe-EOF still exits immediately; HTTP mode is
  unaffected. This retires the `pkill`/`xargs` workarounds from #19.
- `AISQUARE_DB_BUSY_MS` — busy-timeout knob for the context store (default
  5000), so tests can wedge the store without waiting out the full timeout.
- **Delivery self-check (#22)** — the pull side of #20's receipts.
  `aisquare team verify RECEIPT` re-proves a write by seq or event id
  (prefix ok): found on your board → the event, exit 0; missing → exit 1
  (`not_found`, with a `hint` naming the board that holds it when it lives
  elsewhere). `aisquare team log` grows combinable filters — `--by`,
  `--mine` (with `--as`), `--since 15m|2h|ISO`, `--since-seq` (cursor
  semantics), `--kind`, `--task`. MCP parity: `team_log` gains
  `by_session` (literal `me` supported) and a new eighth `verify` tool.
  The injected session protocol now points at the receipt → verify loop.
- **First-class signals (#23)** — named board states instead of prose
  tokens. `aisquare team signal NAME VALUE --as SID` sets (single-token
  name/value), `team signal NAME` reads (value, set_by, set_at, seq),
  `team signals` lists; all with `--json`. Every set emits a `signal`-kind
  event whose payload carries structured `name`/`value`/`prev`/`set_by`
  fields — watchers filter `team log --kind signal --since-seq N` and key
  on fields, never text, so "NOT READY" prose can no longer trip a `ready`
  watcher. State lives in the existing `team_meta` table (no migration),
  the pipe event and state blob commit atomically, and sets follow the
  #20 receipt/read-back contract (`team verify` works on signal seqs).
  MCP: one combined `signal(name, value?)` tool — nine tools total.
- Sessions record **which agent config dir (account) they run under**, derived
  from the transcript path in the hook payload (so it works whether or not the
  agent exports `CLAUDE_CONFIG_DIR` to hook subprocesses). The board and the
  `board -w` TUI label sessions with the account name once more than one is in
  play, making a rate-limited account's terminals identifiable at a glance.
  Schema v9 adds `team_session.account`.
- `aisquare launch <planner|coder|runner>` — starts an agent session already
  attached to the project's team board, replacing the `AISQUARE_ROLE=coder
  claude` env-var-prefixed launch. Validates the role, opts the repo in
  explicitly, then `exec`s the agent so signals, job control and the TTY are
  unchanged. Extra arguments are forwarded (`aisquare launch coder --model
  opus`); `--command` launches an agent other than `claude`. The
  `AISQUARE_ROLE` variable still works.
- `aisquare launch --account <dir>` — run a role under one of several parallel
  agent installs by pointing at its config directory (sets
  `CLAUDE_CONFIG_DIR`). Fails on a directory that does not exist, since a typo
  would otherwise start a fresh unauthenticated profile. Shell aliases like
  `claude1` cannot be passed to `--command` — aliases are not executables —
  so `--account` is the supported route for multi-account setups.
- **Config-gated session tracing** (`[explainability]`, default **off**) —
  with `explainability.enabled = true`, `aisquare launch` wires the session
  through the AISquare explainability proxy: `ANTHROPIC_BASE_URL` plus the
  `X-Agent-Name`/`X-Pipeline-Id` identity pair (a forwarded `--session-id`
  becomes the pipeline id, so board rows and dashboard Runs share a key).
  Every failure fails **open** — dead or wrong-mode proxy, user-owned
  `ANTHROPIC_*` vars, template typos, header-unsafe roles, even an unreadable
  config file cost the trace, never the launch. Hidden
  `aisquare explainability status|env` commands inspect the wiring; `env`
  emits `$'…'`-quoted exports so the header newline survives `eval`.
  `aisquare team spawn` joins at both exits: `--exec` wires the same env seam
  as `launch`, and the printed command is prefixed with
  `eval "$(aisquare explainability env <role>)"` so a **fresh** pipeline id
  mints per run — an id burned into the printable would be reused on every
  paste and merge those sessions into one dashboard Run.
- **The agent harness** — `aisquare team spawn <role>` resolves each role to
  the strongest model its ladder serves (probe-verified with a 24h cache;
  `--refresh` forgets every cached verdict, `--no-probe` trusts the ladder)
  and an effort level (session base from `AISQUARE_EFFORT`/`CLAUDE_EFFORT`
  shifted by a per-role offset; `max` and `ultracode` are first-class).
  `aisquare team harness` prints the whole roster's resolution. Sessions
  self-report model and effort from the SessionStart payload (schema v10 adds
  `team_session.model`/`effort`), and the board and TUI flag a session whose
  model falls outside its role's ladder as `⚠ off-ladder`.

### Changed

- **Store-error honesty (#20 hardening).** Write receipts quote the board's
  `project_id` instead of its directory name (names collide across
  checkouts). `store_locked` now means genuinely retryable lock/busy
  contention only; other database failures (no such table, readonly, disk
  full, corruption) surface as a distinct `store_error` — both carry the
  real cause in a `detail` field under `--json`, and nothing tracebacks.
  `note --task` rejects a task from another project's board (the guard
  `--needs` already had), the store's setup-retry budget scales with
  `AISQUARE_DB_BUSY_MS` (no more 15s floor on a wedged fresh database), and
  the knob clamps at SQLite's 32-bit ceiling so oversized values can no
  longer silently disable the busy handler.
- Roadmap commands are now **hidden from `--help`**: `auth`, `login`,
  `logout`, `whoami`, `sync`, `connectors`, `capture`, `policy`, `enforce`,
  `open`, `upgrade`, `uninstall`. They remain registered and still report the
  not-implemented contract (exit 70) when invoked — only the listing changes.
  `aisquare --help` lists only entries that work.
- The README is split into **Part 1 — Memory (start here)** and **Part 2 —
  Orchestration (advanced)**, with an explicit note that orchestration is
  optional, so the light half of the product no longer reads as heavy.
- **The injected role work-cycles are rewritten — a live behavior change for
  every existing planner/coder/runner session**, picked up on the next prompt
  with no relaunch: the planner's tasks carry an explicit contract (objective,
  why, acceptance criteria, boundaries); a **coder blocks instead of
  guessing** when a claimed task has no usable contract (`task block` with
  what's missing, rather than inventing scope); a **runner reopens
  underspecified tasks** with `task reopen --reason` instead of rubber-
  stamping them; and a new **validator** role gates the assembled deliverable
  once before handoff. Expect formerly-silent sessions to push back on vague
  tasks — that is the feature.

### Fixed

- **Parallel agent installs are now tracked per config directory.** The
  registry recorded a bare agent name, so `agents list` and `doctor` only ever
  inspected `$CLAUDE_CONFIG_DIR` or `~/.claude`. With several accounts
  connected, a sibling install whose hooks had been removed still reported a
  healthy `✓ claude-code: Claude Code connected`. `agents.json` now records
  every connected directory; `doctor` checks them all and names the ones
  missing hooks, and `agents list` gains a `HOOKS IN` column. Disconnecting one
  directory no longer marks the agent disconnected while others remain hooked.
  Registries in the old format are migrated on read.

- Unknown subcommands fail loudly instead of silently (#21): the usage error
  now carries a did-you-mean over the failing group's real verbs (root and
  alias groups included), and when `--json` was parsed before the failure the
  error arrives as one JSON object on stdout (`unknown_command` with
  `did_you_mean`, or `usage` for unknown options) with exit code 2 — so a
  typo can no longer masquerade as an empty result in pipelines. A `--json`
  trailing the typo falls back to the human path by design; lead with
  `--json` for guaranteed machine-readable errors.

- **Team writes cannot lie about success (#20).** `--as`-attributed commands
  (`note`, `task add`, `task next`, …) now deliver to the acting *session's*
  board — never the cwd's — warning loudly when the two disagree. Every
  event-emitting write is read back through a fresh store connection before
  `✓` is printed; the `✓` line carries a receipt (`seq N on <board>`) and
  `--json` output gains a top-level `delivered: true` (plus `warning` on a
  board mismatch). Unconfirmed writes exit 1 with `delivery_unconfirmed` (the
  payload's `ref` names the write), and a locked store maps to a clean
  `store_locked` error instead of a traceback. A failure can leave a
  durable-but-unconfirmed write — check `aisquare log` for the reported ref
  before retrying, or a retried note/claim may duplicate work.

- The global output flags — `--json`, `--verbose`/`-v`, `--quiet`/`-q`,
  `--no-color` and `--profile NAME` — are accepted anywhere on the command
  line: before or after the subcommand, on every command including nested
  groups. Boolean flags OR across positions (duplicates are idempotent);
  `--profile`'s last occurrence wins. `--version` stays root-only (#24).
- **Git worktrees now share their principal repository's context pool.** A
  linked worktree's `.git` is a *file*, so the marker walk in
  `workspace.find_project_root` stopped inside the worktree and handed it its
  own project id — a feature branch checked out beside the repo saw an empty
  context pool, even though team traffic (which already asked
  `git rev-parse --git-common-dir`) correctly shared one board. Both paths now
  use the same git-aware resolution, so several feature branches side by side
  share one context pool, one snapshot and one board — which is what the README
  already promised.

## [0.2.0] - 2026-07-07

### Added

- **Agent Orchestrator** — shared working memory for parallel Claude Code sessions on one
  problem (planner / coders / runner). Sessions register automatically through
  hooks; each prompt delivers a compact delta of what teammates did. Works with
  a single Claude account (sessions are per-terminal) or several installs.
  - Shared tasks: idempotent `task add` (safe to re-emit), **atomic**
    single-winner `claim`, `next --role --claim` for looped worker sessions,
    the `review` → `done` / `reopen --reason` verification cycle, and
    dependencies (`--needs`) so `next` only hands out ready work.
  - `note` / `board` / `team` groups; role work-cycles auto-injected per
    session (planner/coder/runner) — no standing prompts to paste.
  - Live session states on the board — working / waiting for input /
    needs-you — driven by the new `Stop` and `Notification` hooks.
  - `board --watch`: an interactive TUI (`[tui]` extra) — task table +
    bot-style live feed + click-for-detail bar, theme browser (`t`,
    autosaved), local screenshots (`s`), feed autoscroll toggle (`a`) and a
    select-text mode (`v`/`c`). Rich full-screen fallback without the extra.
  - **Long-term memory (gbrain)**: durable events (decisions, results, task
    outcomes, reopen feedback) distill into a per-project gbrain brain via a
    detached, flock-guarded worker; `recall` searches it. Never on the hot
    path; degrades silently when gbrain is absent.
  - **`serve`** (`[serve]` extra): the orchestrator as an MCP server (stdio or
    bearer-token HTTP) so remote Claude clients — e.g. a browser-debugging
    agent in the Claude desktop app — join as attributed virtual sessions.
  - Multi-repo executions via `AISQUARE_TEAM_HUB`; worktree-safe project
    identity (`git rev-parse --git-common-dir`); `agents connect --config-dir`
    for parallel `CLAUDE_CONFIG_DIR` installs.
  - **Semantic recall**: with `AISQUARE_BRAIN_EMBED=1` (and an
    `OPENAI_API_KEY`) distilled pages are embedded and `recall` uses gbrain's
    hybrid vector+keyword search, falling back to keyword when unavailable.
    The embedding schema is fixed at brain-creation time, so the knob must be
    set before the first distill; `doctor` flags a knob-vs-schema mismatch and
    points at the rebuild (`team distill --all`).
  - Env knobs (no config gating): `AISQUARE_TEAM`, `AISQUARE_ROLE`,
    `AISQUARE_TEAM_HUB`, `AISQUARE_TEAM_DELTA`, `AISQUARE_TEAM_LEASE_MIN`,
    `AISQUARE_BRAIN`, `AISQUARE_BRAIN_EMBED`, `AISQUARE_BRAIN_EMBED_MODEL`.

## [0.1.0] - 2026-06-29

First release — a portable memory layer for coding agents.

### Added

- **Context store** (local SQLite): `remember` and the `context` group — `add`,
  `list`, `show`, `edit`, `remove`, `search` (FTS5), `promote`, `import`,
  `export`, `preview` — across `user` and `project` pools, with sync-ready
  metadata (soft-delete tombstones, `updated_at`) and time-sortable,
  prefix-addressable ids.
- **`inject` / `why`** — assemble in-scope context for an agent session and
  explain the last injection.
- **Projects** — `init`, the `project` group (`info` / `list` / `switch` /
  `link`), and `onboard`, which packs a Repomix codebase snapshot (full pack +
  skeleton + per-file index).
- **Claude Code integration** — `agents` group with `connect` that installs
  `SessionStart` + `UserPromptSubmit` hooks (injecting context and capturing
  prompts), plus detection (`scan` / `list` / `status`) and `disconnect`.
- **Diagnostics & config** — `status`, `doctor` (dependency + setup health with
  fixes), the `config` group, and `log` (captured prompt history).

[Unreleased]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.4.0rc2...v0.5.0
[0.4.0rc2]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.4.0rc1...v0.4.0rc2
[0.4.0rc1]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.2.0...v0.4.0rc1
[0.2.0]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AISquare-Studio/aisquare-cli/releases/tag/v0.1.0
