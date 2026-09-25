# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The captain** — the home-level voice-and-text agent that runs every
  project's fleet for you (`docs/captain.md`; the plan as merged in
  `docs/plans/captain.md`; the acceptance runbook in
  `docs/runbooks/captain-acceptance.md`). One Claude Code session per home, on
  the home board, with no tool but its own **Actions server** (`aisquare captain
  serve --stdio`): 24 fixed tools, one `captain_action` audit event per call
  carrying the owner's words and the effect's receipt; `stop`, `spawn` and
  `restart` need `confirm=true` on the owner's own words; a refusal is said as
  `refused:` / `error:`, never faked. `aisquare captain` starts or attaches;
  `aisquare captain "text"` delivers and prints the reply (`--json`: one object);
  `aisquare captain chat` is line by line. The **attention queue** folds every
  board into one ranked list of what needs the owner (question, blocked,
  waiting, review, pull request, stale), deduplicated and resolved one item at a
  time — `aisquare captain attention | next | resolve | snooze`, with `since`,
  `log`, `actions`, and the easter eggs `uav`, `wololo`, `bt`. The **voice page**
  (`aisquare captain --voice`, `[voice]` extra): hold to talk, or always
  listening behind the wake word — only "Captain, …" reaches the captain, the
  rest is never delivered, spoken or shown (`[captain] wake_word`, `""` turns it
  off) — interim transcripts, the thinking signal, the reply spoken back
  through a Speaker adapter per platform; one spool drainer in the captain's
  server plays the captain's own `speak()` lines. A fresh captain is started
  bare and never typed into at Claude Code's trust dialog, and it takes its
  first message at its idle input box even while the fleet still reads it
  working. `press yes`/`press no` answer Claude Code's real permission chooser
  by its Yes digit and Esc (read off the pane), and a key the prompt ignores is
  said as an error. After a reboot a provably gone tmux server means a fresh
  start, anything less a fast refusal naming
  `aisquare fleet reap -P <home> --server-down`.

### Fixed

- **`aisquare serve --stdio` no longer exits in the middle of a tool call.** Its
  idle deadline (`--close-after`, `AISQUARE_SERVE_CLOSE_AFTER`) counted inbound
  client messages only, while a tool runs on a worker thread and the client sends
  nothing, so a call longer than the deadline was killed mid-flight and its answer
  lost. The clock now stands still while a call runs and counts from the end of
  the last one; an abandoned server still closes itself. Found gating the captain's
  Actions server (#217), which shares the runner.
- **`explainability use` for one project no longer re-points every project
  without a destination** (crew gate on #203, finding 1). On the machine
  `init --explainability` produces — a top-level gateway and the key file, no
  target — the default target NAME is `stg`, and so is staging's: `use` for one
  project signed in to `stg-api` created `targets.stg`, and every other
  project's launches, the doctor and the shipper resolved staging with no key.
  A target `use` writes is marked the destination's (`destination = true`): the
  machine default never resolves it, and `ensure_target` creates a missing
  target only — it no longer fills an existing target's empty fields, which
  moved whoever resolved it. `enable --target`, the Setup form's *make active*
  or any setting written for it makes the target the operator's again.
- **An API host the CLI cannot place no longer resolves to prod's gateway and
  proxy** (crew gate on #203, finding 2). A destination on a self-hosted API
  (or `http://[::1]`) got a target with no gateway, which fell back to the
  top-level one — prod's, on a prod machine — so the key minted for that
  deployment was posted to another. A destination's target borrows nothing
  from the machine: no gateway means `gateway_source = unset`, `status` prints
  `(no gateway known)`, and shipping refuses rather than misroutes.
- **The documented-commands guard no longer fails the checkout that runs the
  fleet.** `test_the_document_list_has_not_gone_stale` walks the whole
  repository for markdown with commands in a fenced block, and a root checkout
  that hosts coder worktrees under `.aisquare-worktrees/` holds one full copy of
  every document per agent — so `make check` from the root failed, reporting
  each worktree's README.md and docs pages as unlisted copies of themselves,
  while every real document passed (measured on `rc/hackathon-v1` with two
  coder worktrees; from a clean checkout or inside a worktree it passed). The
  sweep now never enters the fleet's `worktree_dir` (the `[fleet]` default) or
  any directory holding a `.git` *file* — a linked worktree wherever it was put
  — the way `core/snapshot.py` already ignores `**/.aisquare-worktrees/**`. It
  prunes as it walks, so it no longer reads every agent's `.venv` to throw the
  result away. The guard's rules and its document list are unchanged, and the
  positive control stays: the same fenced page at the repo's own level is still
  reported.


- **A schemeless gateway is refused by the writer, not only by the form.** The
  Setup form refused `stg.example`; `aisquare explainability enable
  --gateway-url stg.example` — the runbook command, four characters short —
  stored it, after which the proxy lane read **green and silent** over a gateway
  nothing could reach: a host-less URL parses with the whole string as the path,
  `is_loopback` counts an empty host as local, and the loopback-pair exemption
  fired. `configure_target` — the one writer both surfaces go through — now
  validates the gateway, the proxy and the identity template before it mutates
  anything and raises with the fix; `enable` prints it as one `✗` line and
  stores nothing. `url_problem` is the shared validator and says *which* thing
  is wrong (unparseable, bad port, no scheme, not http(s), no host) — the form
  used to answer `http://[::1` with "try https://http://[::1". The proxy lane
  no longer treats a host-less gateway as a loopback pair, and `doctor`'s config
  lane flags a stored one, so a hand-edited config or
  `EXPLAINABILITY_GATEWAY_URL` cannot reach the stranded state either.
- **The Setup form's deployment field no longer moves the machine.** It set
  `settings.target` on every save, so an operator on stg correcting prod's
  gateway had moved their machine to prod — traffic to a deployment nobody
  chose, the headline failure from the other side — and typing *only* a
  deployment name flipped the target while writing no entry, under a `✓ setup
  saved` toast. The field now names the entry the settings belong to; a **make
  active** checkbox beside it is the switch, and the toast says which target
  the machine is on. `enable --target` keeps switching: a flag typed in a shell
  is the explicit act the box is.
- **A key typed for a target that names its own key variable is refused.** The
  key file is read only for the default variable — a single unlabelled key must
  never satisfy a prod target — so key + custom variable in one save wrote a
  file nothing reads: `✓ setup saved` over `$MY_WORKSPACE_KEY is NOT set`. The
  rule is judged against the variable the target will read from after the save,
  typed today or stored last month, and the notice says where the key should go
  instead.
- **A braced prefix is refused, not repaired.** The first cut detected `}` and
  stripped at `{`, so `nishil}` passed through whole, was stored as
  `nishil}-{role}`, and every `.format` raised: `agent_names` empty, every
  launch untraced, a success line on the screen. The second stripped at either
  brace and stored what preceded it — a template the operator never typed
  (`team-{env}-{role}` became `team-{role}`) while the CLI's `--identity`
  refused the same input. The field asks for a name: a brace of either kind is
  refused with the reason and nothing is stored, and the writer refuses any
  template that cannot render or renders every role to one name.
- **The hosted-proxy suggestion respects a deliberate top-level `proxy_url`.**
  The form read the per-target value only, so a chosen `[explainability]
  proxy_url` — which `_proxy_source` already reports as `config` rather than
  `default` for exactly this reason — was shadowed by a per-target suggestion.
  `explainability_ops.chosen_proxy` is the resolver's fold minus the shipped
  default, which is the one value nobody picked.
- **The two unverifiable ambers are worded by mechanism.** A loopback sidecar is
  told *why* it may ship elsewhere — it took its destination from
  `EXPLAINABILITY_GATEWAY_URL` when it was started — with the restart and the
  deployment's own proxy spelled out. A hosted proxy on a host that is not the
  gateway's may be the deployment's own behind a load balancer or CNAME, which
  from here looks exactly like another deployment's, so it is asked the question
  and given both answers rather than ordered to repoint. The unset-gateway amber
  prints the gateway the proxy *does* report, with the `--gateway-url` command
  that adopts it.
- **`explainability status` exits 1 for a live proxy shipping to another
  deployment, and now says so.** The exit code's documented meaning was "the
  proxy would not take a session"; the destination check widened it without a
  word. Both states are "the traces are not arriving where you think", which is
  what a cutover script gating on this code asks, so the rule stands and the
  docstring, the comment and this entry carry it. Amber exits 0, and
  `probe_severity` says which — that field is now tested, with `probe_fix`.
- **The key field is cleared even when the key write fails.** A failed
  `store_api_key` returned before the field was cleared, leaving the plaintext
  live in a masked `Input` for the rest of the session.
- **Every URL this integration takes from a human now goes through one guarded
  parse.** `urlsplit` raises `ValueError` on a malformed authority — `http://[::1`
  (a typo'd IPv6 bracket) is reachable by typing — and two callers took it
  unguarded: `is_loopback` off a config value, so `aisquare doctor` tracebacked
  where `main` returns its checks normally, and `hosted_proxy_for` off a form
  field, so a Textual `Button.Pressed` handler took the fleet UI down while every
  other failure in that handler was caught and shown as a notice. `split_url`
  answers `None` instead of raising and is now the module's only parser, so a new
  caller cannot reintroduce the hazard by forgetting a `try`. An unparseable URL
  is **not** treated as loopback: that question decides whether a workspace key
  may be omitted. `probe_proxy` likewise answers rather than raising when
  `/health` returns valid JSON that is not an object (`[]`, `"ok"`) — the decode
  succeeded, so its handler was already past, and four `payload.get` reads
  followed.
- **The hosted-proxy suggestion is silent where it would be wrong, not merely
  where it is unsure.** `HOSTED_PROXY_PORT` is the hosted deployments'
  convention; the wholly-local topology's own port is the shipped `proxy_url`
  default (9090). Suggesting 9443 for a loopback gateway repointed a self-hosted
  adopter — the topology in this change's own measured repro — at a port with
  nothing on it. IPv6 hosts are re-bracketed, because `urlsplit().hostname`
  strips them and `https://::1:9443` is not a URL any client can reach.
- **The Setup form no longer overwrites a proxy the operator chose.** It tested
  the blank *field*, not the stored *value*, so a target with a deliberate
  `proxy_url` whose gateway was merely corrected had its proxy silently replaced
  — the opposite of the "a blank field changes nothing" contract printed above
  the form and asserted one layer down in `configure_target`. It also refuses a
  schemeless gateway (which parses with the whole string as the path, leaving no
  host, no suggestion, and an empty host that reads as loopback and suppresses
  the very warning that would have flagged it), refuses a prefix typed as a
  template (`nishil-{role}` would have composed to `nishil-coder-coder`, and a
  stray brace empties `agent_names` entirely), and can set `key_env`, which it
  was `configure_target`'s only caller to omit.
- **An unset gateway is no longer reported as a misroute.** `resolve_target`
  legitimately yields `gateway_url == ""`, and an empty string equals no
  deployment, so the comparison called every such machine misrouted — printing a
  sentence with a blank where a URL goes, and making `explainability status` exit
  1. Nothing is misrouted; the CLI has no second value. Amber, and it says so.
- **A hosted proxy on a host that is not the gateway's is no longer waved
  through** — the failure class this change exists to close, still open inside
  it. Such a proxy fell past the amber branch (which required a *loopback* proxy)
  to the bare green return, on the docstring's assumption that "a hosted proxy is
  addressed at the deployment, so it cannot disagree with it". That is an
  assumption about the operator's typing, and `hosted_proxy_for` is this module's
  own statement that the two share a host, so the comparison was available.
- **The proxy verdict is one severity rather than three booleans.**
  `healthy`/`problem`/`caution` could express states that mean nothing
  (`problem` and `caution` together), and only `doctor` read the third — so the
  amber rendered **green** on `explainability status` and in the fleet tab.
  `ProxyState.severity` is the `CheckStatus` vocabulary every other check already
  speaks; `problem` is derived from it, and `healthy` — a second, independently
  settable encoding of the same fact, which the misroute branch contradicted by
  setting it `False` for a proxy that *is* tracing — is gone, so `status`'s exit
  code and the fleet tab's red both branch on `problem`. Both surfaces now
  render the amber as amber **and** print its
  remediation, against this module's own rule that a line which is not ok without
  its next command is half a doctor. `status --json` gains `probe_severity` and
  `probe_fix`, so a script watching for a misroute no longer has to regex an
  English sentence.


- **`doctor` and `status` no longer call the proxy lane green without knowing
  where the proxy ships.** Both rested on one `GET {proxy_url}/health`, whose
  payload says what the process *is* — `service`, `mode`, `status` — and never
  what it does with the traffic. So a proxy pointed at a different deployment
  than the configured target read green everywhere. Measured on a real machine
  with `target = stg` and a sidecar started with the SDK's own `.env` in its
  environment: `proxy`, `gateway` and `ingest` all green, every line true, while
  the Runs from a real Claude Code session landed on `127.0.0.1:8000` — 274
  ingest batches in four hours, none of them where the operator was looking. The
  reported symptom was "I ran one query and did not receive anything on stg."
  `gateway` and `ingest` verify the *CLI's* path; the proxy carries the model
  traffic down a second one, and nothing compared them. This is the failure
  `_active_deployment` already records for the client lane ("Both halves looked
  healthy. Nobody was told") — fixed there, still open here.
  - `ProxyProbe` carries the `gateway` the proxy reports, when it reports one.
    A proxy that predates the field is not broken, merely unverifiable, and the
    two are now told apart rather than both rendered green.
  - A reported gateway that disagrees with the target is **red**, and names both
    URLs: an operator who is told only that something is wrong has to go and
    find which of two levers moved.
  - No reported gateway, a loopback proxy and a remote gateway is **amber** —
    `ProxyState.caution`, the verdict this had to grow. A sidecar takes its
    destination from whoever started it, which need not be the target this CLI
    resolved, and that is exactly the combination that stranded the traffic
    above. Green was a lie and red would have been one too.
  - The topologies that *cannot* disagree stay silent: a hosted proxy is
    addressed at the deployment, and a loopback proxy against a loopback gateway
    is the self-hosted topology working as intended.
  - Gateways are compared on scheme, host and port, not as strings, so a
    trailing slash or an explicitly written default port is not a misroute.
  - `explainability.is_loopback` is public for the second module that needs the
    same discriminator, on the precedent `stored_api_key` set.

### Added
- **The persona rides the system prompt too, and a replay keeps it.** For Claude
  Code the launch appends the persona block to the default system prompt
  (`--append-system-prompt-file`, a file under `~/.aisquare/cache/persona-prompts/`;
  the name travels as `AISQUARE_PERSONA`, never the body) beside the
  session-start briefing that survives `/clear`, so a persona holds over a long
  session as hook context alone may not (plan §9, #210). `[persona]
  system_prompt = false` keeps the briefing alone; an `--append-system-prompt`
  of either spelling on the operator's line wins. codex, aider and any wrapper
  not named `claude` have no seam this launcher knows and get the briefing
  alone, said in one dim line. `fleet restart` and `fleet switch` replay the
  ROW's persona — spawned with, or attached since — never the role's default of
  the day; one that no longer resolves steps down to the role's current
  default, then to none, each said on the receipt and never a refusal (owner
  decision, 2026-09-24; supersedes #209).
- **Stop an agent from `asq`.** The agent view's header gains a compact **Stop**
  button, and `x` with the sidebar focused stops the selected agent — both open
  one dialog (*Stop* · *Force* · *Cancel*) over `services.fleet.stop`, the same
  command `aisquare fleet stop` runs, `--force` included. Stop is offered while
  the agent has a process and on a **💤 exited** row — there it removes the dead
  window tmux kept for the last screen and the row leaves the listing (#138), the
  question says so and offers no *Force* — and **both controls ask the same
  rule**, `sidebar.STOP_STATES`, built on the `ALIVE_STATES` the card's "agents
  alive" chip already counts by, so a lost row offers none, by button or by key.
  Beside Stop sits the release train's **Restart** (#138). The call runs in a thread worker with the buttons disabled; a refusal —
  including the deliberate one where tmux cannot confirm the pane died and the
  row is left live — stays in the dialog as its own text, with the agent
  untouched, rather than closing over a stop that did not happen. A stop that
  worked toasts `✓ stopped <label> (<id>)`, re-reads the fleet, and leaves the
  stopped agent's view for the project's.
- **Attach a persona in two steps, from `asq`.** The Personas tab's *Attach to
  existing* / *Attach to new* open one **target picker**: this project's running
  **Agents** (each with the persona it runs), the **Binds** `aisquare team bind`
  pinned (with the binary and the account each environment points at) and the
  Claude **Accounts** — Agents first for "existing", Binds then Accounts for
  "new", every section selectable either way, and a filter across all three.
  Choosing an agent confirms ("it replaces mentor") and calls
  `fleet_service.attach_persona` in a thread worker; the toast says whether the
  briefing was `typed` or `noted`. Choosing a bind or an account opens the Spawn
  dialog preset with the seat, its binary or the slot, and the persona. **+ New
  bind** is `team bind` as a form, saved through `services.settings.bind_role`,
  the seat checked by the rule `spawn` applies and the binary by `PATH`, an
  account filling `CLAUDE_CONFIG_DIR`/`CLAUDE_CODE_TMPDIR` as `launch --account`
  would; the list re-reads with the new bind selected. **+ New account** opens
  the Accounts page's own add-account flow. The Spawn dialog's *Pick…* opens the
  same picker in "new" order and fills its role, binary or account without losing
  what was typed, and *Import…* beside its Persona field imports a persona and
  selects it. Measured headless in `tests/test_ui_personas.py` and
  `tests/test_ui_spawn.py`, with recorders for `list_agents`, `attach_persona`,
  `spawn`, `bind_role` and the accounts read.
- **Attach a persona to a running agent.** `aisquare persona attach <name> --to
  <label>` gives a fleet agent that is already running a persona: the name is
  checked first (an unknown one lists the known names before anything is
  touched), one `persona_attached` line goes on the board, the agent's
  `fleet_agent` row — and its board session, once joined — records it, and the
  briefing is delivered exactly as `fleet tell` delivers: typed into a waiting
  agent, a board note for a busy one, with the receipt saying `typed` or `noted`
  (`--json` carries `delivered`). Replacing a persona tells the agent which one it
  replaces. The session-start hook now asks for the persona on the fleet row
  `AISQUARE_FLEET_AGENT` names, else `AISQUARE_PERSONA`, else keeps the session's
  own. So an attached persona is briefed again after a `/clear` or a restart,
  even for an agent spawned with `--persona`, and a session with no persona
  anywhere still sees byte-identical text.
- **Import anything as a persona.** `aisquare persona import` now converts a
  source that is not already a skill — plain text, another tool's JSON or YAML
  persona, a page over `https://` — with an LLM: the fleet's own Claude Code,
  headless, under the manager role's binding first; then the Anthropic API
  through the official SDK (new optional extra `aisquare-cli[llm]`); then a
  refusal naming both fixes. The answer is structured, held to the same rules
  as a copied skill (one retry with the failed rule), kept as a draft under
  `$AISQUARE_HOME/personas/.drafts/` before anything else, shown and confirmed
  (`--yes` skips; `--json` or no terminal keeps the draft and exits
  `needs_confirmation` with its path), and saved with engine, model and
  `condensed` in `.persona.json`. New flags `--llm/--no-llm`, `--condense`,
  `--engine`, `--model`, `--yes`; new config `[persona.import]` (`engine =
  "auto" | "manager" | "api" | "off"`, `api_model`). The headless run is a ruled
  spawn seam that strips the tracing identity, and `anthropic` and the engine
  module are imported inside functions, so the CLI's startup and every hook load
  neither. Saving config now keeps a newer build's unknown key inside a section
  written under an alias, such as `[persona.import]`.
- **The Spawn dialog asks as whom.** After who runs the agent — Role (now with
  *Pick…*), Account, Binary — the dialog has a **Persona** select: `(none)` plus
  this project's personas with their layer, the role's
  `[fleet.roles.<role>].persona` preselected and followed as the role changes
  until you pick one, and the persona's description under the field. It sends
  `persona=None` while it shows the role's default, the name once one is chosen,
  and `""` for an explicit `(none)` over a role that has a default — which
  `fleet spawn` reads as "no persona", so the choice beats the config. The dialog
  takes presets, `SpawnDialog(project, persona=, role=, binary=, account=)`,
  applied at compose so the persona-first flow can open it filled in; a preset
  seat such as `coder2` or an account slot not read yet still shows. *Pick…* posts
  `PickTargetRequested`, which the dialog answers by opening the target picker.
  The Settings tab gains a persona per role, saved through `save_config` and
  re-read, showing a configured name the project lacks as `<name> (custom)`; and a
  sidebar agent row carries a dim `· <persona>` from its fleet row or, failing that,
  its session. Measured in `tests/test_ui_spawn.py` (the recorder's `persona`,
  `role`, `account` and `binary`) and `tests/test_ui_project.py` (the bytes of
  `[fleet.roles.coder] persona = "skeptic"` in `config.toml`).
- **Run an agent as a persona.** `aisquare launch <role> --persona NAME` and
  `aisquare fleet spawn <role> --persona NAME` — default: the role's new
  `[fleet.roles.<role>].persona` — start an agent as someone. The name is checked
  before anything starts (an unknown one is refused with the known names; a stale
  config default names its key) and travels as `AISQUARE_PERSONA`, never the body.
  The SessionStart hook records it on the board row (store v15:
  `team_session.persona`, `fleet_agent.persona`) and adds the persona's block to
  the team briefing once, after the role cycle and the lane rule. A session
  without a persona gets byte-identical text — pinned against the base in
  `tests/test_persona_briefing.py` — the per-prompt delta and `aisquare board`
  carry no persona text, and a persona that can no longer be loaded costs one
  line, never the team block. The board's session line reads `persona:<name>`,
  `fleet ls` shows `· <name>`, a spawn receipt ends `· persona <name>`, and a
  persona written for other roles is a receipt note, not a refusal. Found on
  the way: saving config dropped an unknown key INSIDE a `[fleet.roles.<role>]`
  or `[explainability.targets.<name>]` entry, because those tables were
  replaced wholesale; from this build on each kept entry is merged field by
  field, so a later build's role key survives this one.
- **A Personas tab in the Project view.** Every persona the project can use —
  project, user and bundled layers — in one searchable table with the layer,
  description, roles and the `⇧ shadows` / `✗ invalid` marks, and beside it a
  preview that is byte-for-byte the block an agent is briefed with, followed by
  the directory, supporting files, `.persona.json` provenance and warnings. The
  selected persona can be edited in place (a `TextArea` over the whole
  `SKILL.md`, re-parsed as you type, *Save* only while it parses, saved through
  `services.personas.save` — the writer `persona edit` uses — so a refused edit
  keeps the old bytes), exported to Claude Code's personal or project skills or
  a directory, removed after one question naming the directory, and validated.
  Bundled rows open read-only with *Save as…* into a layer. **+ Import…** is
  `persona import` as a form, run in a thread worker over the same
  `import_source` the CLI calls: its `progress` lines appear under the form and
  its `confirm` opens a draft-review modal from the worker
  (`call_from_thread(push_screen_wait, …)`, verified on Textual 8.2.8 first), so
  the LLM import path runs through the same form. **+ New** scaffolds through
  `services.personas.new` and opens the editor on it. *Attach to existing* /
  *Attach to new* post `AttachRequested`, which the tab answers by opening the
  target picker. Measured headless in `tests/test_ui_personas.py`: real catalogues
  written into the isolated home and a `git init` repository, every write and
  import a recorder, assertions on the keywords received, the rows, the preview
  text, and a `SKILL.md` left byte-identical when only the recorder saved.
- **Personas — and a persona is a Claude Code skill.** `aisquare persona`
  (`list`, `show`, `new`, `edit`, `rm`, `validate`, `import`, `export`, every
  reporting verb with `--json`) manages how an agent works — a skeptic, a mentor,
  a minimalist — as `<name>/SKILL.md` directories in three layers: the project
  (`<repo>/.aisquare/personas`), the user (`$AISQUARE_HOME/personas`) and four
  bundled ones (`skeptic`, `mentor`, `minimalist`, `careful`), the higher layer
  winning and `list` saying what it shadows. The same directory is `/name` in
  Claude Code: `persona import` copies a skill in byte for byte (a skill
  directory, a `.claude/agents` file, a Cursor rule, stdin, or a skill by name
  from `import --list`) with a `.persona.json` recording source and sha256, and
  `persona export --skill --user|--project` copies one out into Claude Code's
  skills. A round trip is byte-identical. `persona show` prints exactly the
  block an agent will be briefed with: the body, sanitised, fenced so it cannot
  close its own block, and one sentence saying a persona never overrides a
  role's cycle, the lane rule, a task's contract or evidence. A body over 4,000
  characters warns and over 12,000 is refused; a directory that does not load is
  listed, never fatal. Something that is not a skill goes through the LLM import
  path; `--no-llm` refuses it as `not_recognised`. **PyYAML** is now a core
  dependency — a skill's frontmatter is full YAML and an interchange format may
  not refuse a valid one — read with `safe_load` only and imported inside the
  parser: `python -X importtime -c "import aisquare.cli.app"` shows no `yaml`.
  `launch` and `fleet spawn` run an agent as a persona with `--persona`. Plan:
  `docs/plans/spawn-personas.md`; guide: `docs/personas.md`.
- **The Spawn dialog, in `asq`.** `＋ spawn agent` under a project used to
  toast "the spawn dialog is not built yet"; it now opens a form over the same
  `services.fleet.spawn` the CLI runs, headed with the project's name and
  codename so a spawn from the wrong row is visible before it happens. Role
  (the fleet's roles plus every `team bind` role; `manager` greyed out while
  one runs), label (prefilled the way `fleet spawn` picks it, re-prefilled when
  a task is picked unless you typed one, 🎲 for `<role>-<adjective>-<animal>`,
  live-checked against the label rule), task (the project's open tasks),
  worktree (disabled with "not a git repository" outside one), permission mode,
  account (read in a worker so the dialog opens at once), binary, extra agent
  args (`shlex`-split, a quoting error shown inline) and a first prompt. A field
  left as it opened is sent as `None` — the role's default, exactly what an
  omitted flag means — so the service resolves it from the config it reads at
  spawn time; the fields that show a role default follow the role until you
  change them. The spawn runs off the UI thread: a `FleetError` stays in the
  dialog with its reason and *Spawn* re-enables, anything else shows its class
  name instead of taking the app down, and success toasts the receipt plus each
  note and opens the new agent's pane. A started spawn cannot be taken back, so
  `Esc` waits for its answer rather than pretending to cancel it. Measured
  headless in `tests/test_ui_spawn.py` with a recorder in place of
  `fleet_service.spawn` — the keywords it received are the assertion — and a
  tmux guard that fails any test addressing a socket other than its own. Found
  on the way: a private `_running` on a Textual screen shadows the message
  pump's own flag and silently leaves every button of the screen dead; the
  dialog's flag is `_spawning`.
- **The navigator is resizable** (#137). The line between the sidebar and the
  content is a divider: drag it (the sidebar never drops below 24 columns, the
  content never below 40 while the terminal has room for both — a pane narrower
  than that is not a terminal — and a terminal that shrinks re-bounds it), or
  with the sidebar focused step it with
  `>` / `<` and put it back with `=`; a double click on the divider does the
  same. The divider is the sidebar's old right border, one column over: it
  lights up while the sidebar has focus. The width is remembered in
  `state.json` beside the theme and restored at the next launch; an agent's
  pane forwards every width change to tmux, so the agent reflows. `?` lists the
  keys.
- **Workspace credits beside where traces land** (#143). With a destination
  chosen (#142), `explainability status` and `whoami` print a `credits:` line
  for that workspace — run and build pools, today and this month, what is left
  and when it resets, the server's `low`/`exhausted` band — and `status --json`
  carries the numbers under `credits`. The Accounts page draws the same as bars
  under the AISquare card on its minute tick; the Explainability view has a
  `credits` row; `doctor --live` gains `workspace-credits`, warning before a
  fleet is spawned into a low or exhausted workspace. One request per
  workspace, cached a minute, never on a hook or session path; failures are a
  reason on the row, nothing else.
- **Pick where a project's traces land with your sign-in** (#142).
  `aisquare explainability workspaces`, `studios [--workspace W]` and
  `use <workspace>[/<studio>] [--project P] [--no-key] [--clear]` list what the
  signed-in user can see and record the choice per project (schema v21,
  `project_destination`). The deployment the session belongs to becomes the
  project's explainability target with its gateway and hosted proxy filled in
  (`stg-api` → `stg`, `api` → `prod`; nothing typed, nothing enabled behind
  your back); the one resolver consults it between `--target` and the machine
  default. The CLI obtains a workspace `ingest:write` key on your behalf and
  stores it as `key set` would — the API still refuses a sign-in token there
  (AISquare-Studio-BE#3493), so until then the line says so and `key set` is the
  way in — and binds this machine's agent identities to the chosen studio, which
  is what makes spans land there. `status` shows `destination:` (the UI's
  Explainability view `lands in`) and takes `--project`: it is the check `use`
  names once tracing is on, since `doctor` resolves only the machine's key;
  `whoami` gains a `traces:` line; `logout` forgets every key the CLI minted and
  leaves hand-attached keys alone. The key never crosses a deployment or a
  workspace: a target `use` creates names its own key variable,
  a machine key never stands in for the mint, launches take the proxy from the
  same target as the key, `key set` binds to the destination's deployment, the
  CLI never mints over a hand key, and a minted key that is replaced, cleared,
  purged with its project or left behind by a move is revoked on the host that
  minted it — a replaced one only once its replacement is recorded.
- **Project groups, pinning and manual order in the sidebar** (#140). A
  management layer only, like browser tab groups: a `project_group` table and
  `group_id` / `position` / `pinned_at` on the project row (schema v20); a
  group shares nothing and deleting one ungroups, never deletes. In the
  sidebar: drag a card onto a group header, between cards, or below the list;
  drag a group header to reorder groups; `shift+↑`/`shift+↓` move, `g` opens
  the group picker (existing, new, ungroup), `p` pins, `space` folds, `u`
  undoes the last gesture with a toast, `shift+click` marks several cards and
  `shift+g` groups them. A 📌 Pinned section at the top; group headers roll up
  their members' agents. CLI parity: `project group create|rename|delete|list|
  add|remove|move`, `project pin|unpin`, `project move --to <group|top>
  [--before|--after|--position]`, `project list --group|--pinned` (JSON
  carries `group`, `position`, `pinned`), `project onboard --group`. One
  arranger (`services.project_groups.arrange`) decides the order every
  surface shows; every change returns its way back.
- **A workspace key per project** (#141). The explainability key was one per
  machine; pointing one project at another workspace meant another shell or
  swapping the file for everyone. `aisquare explainability key set [--project
  P] [--target T]` attaches a key to a project — read from stdin or
  `--from-env VAR`, never from argv — stored owner-only in the project's data
  directory (mode 600, and restricted to your account on Windows, written by
  rename into a temp restricted while still empty, as the machine key is),
  with only the deployment and the path in the store (schema v19);
  `key show` prints the origin (never the value) and `key clear` detaches it.
  Resolution stays in the one resolver: project key → the target's variable →
  the machine file, and a key attached for one deployment is never handed to
  another. "The project" is the one a launch from here joins
  (`$AISQUARE_TEAM_HUB`, else this checkout — never the `project switch` pin)
  everywhere: `launch`, `fleet spawn`, `explainability env [--project]` and
  `register [--project]` use the project's key, `status` shows its origin,
  and each project page's Explainability tab shows the key its launches use
  (the hub's under a hub); its Setup form's one key field attaches the key to
  that project, for the deployment the form names, when *this project only* is
  ticked, and writes the machine key as before when it is not. The client lane
  (`ship`) still uses the machine key.
- **A restart is the same agent, and the UI comes back where it was** (#144).
  `fleet_agent` rows record a `launch_spec` at spawn — the binary, the
  permission mode actually passed (none included), the arguments after the
  role's own less any that chose a session, the account slot, the worktree
  choice and the whole window argv — and `fleet restart` / `fleet switch`
  replay it instead of re-reading today's config, so a role edited between
  runs cannot silently change what "the same agent" means; `fleet restart
  --permission-mode` changes the replayed mode, and the replacement's spec
  keeps it, which is the step the auto-mode notices of #150 now name for a
  running agent, since the role's config reaches only later spawns (schema v18; rows
  spawned before the spec fall back to the config as before; a recorded binary
  no longer on the PATH is resolved again when that is the same kind of
  program, and the receipt names it — otherwise the restart is refused before
  a running agent is stopped). The session itself already resumes from
  its transcript (#138). The
  shell now remembers what was open — a project, an agent, the Accounts page,
  the Doctor — and the captured-directories toggle, in the store's new
  `ui_state` table, and reopens it at the next launch when the row is still
  there; `doctor` gains `fleet-resume`, counting the exited agents whose
  transcript is on disk and would therefore continue rather than start over.
- **Usage-aware accounts: spawn where there is headroom, and hand an agent over
  when its limit hits** (#146). A new `[accounts]` section (Settings tab, or
  `aisquare config set accounts.<key>`): `pick = headroom` makes every launch
  that nothing names an account for read each enabled, signed-in account's
  five-hour window and take, in priority order, the first under `switch_at`
  (85 %) — or the one with the most room when all are over it; usage that
  cannot be read is skipped with a note, and when none can, the machine
  default decides as before. Every reading is kept (`claude_usage`, schema
  v16), so `accounts usage`, `list --usage` and the Accounts page say
  *≈ 40 min to the limit* at the current pace once two readings of the same
  window exist. When an agent's turn ends on a usage limit — Claude Code's
  `StopFailure` hook, now the sixth hook `agents connect` installs, with
  `error: rate_limit` and `You've hit your session limit · resets 12:30am` — the
  row shows **⏳ limited** with the reset time, a `limited` board line names
  `aisquare fleet switch <label>`, and the manager is woken (`limited` and
  `switched` join its wake kinds); other API errors end the turn as `waiting`
  with a `turn_failed` line. `aisquare fleet switch <label> [--to A] [--fresh]`
  stops the agent as `fleet stop` would and starts it again under the same
  label, task and worktree on the account with the most headroom, **resuming
  the same session** from its transcript (`claude --resume <path>`) when it is
  on disk, else — or with `--fresh` — with a hand-off prompt built from the
  board, the old session's claims moving onto the new session with its row. With
  `on_limit = switch` the fleet does that by itself when the limit lifts more
  than `wait_if_reset_within_minutes` (15) away — in a worker detached from
  the agent's own hook, so the window kill cannot take the hand-over down; a
  hand-over that finds no headroom leaves the agent parked with Claude Code's
  own wait-and-continue intact. A moved agent keeps its task claims (its
  session parks them, as a `/clear` does, for the same id when it resumes and
  for the new one when it starts fresh), a resumed one is told in one line to
  continue, and no `agent_exited` goes out for either; every reset a surface
  shows — the feed, the agent header, doctor — comes from the one formatter. `doctor` lists parked agents (`claude-account-limits`) and, with
  `--live`, warns when every account is over the line
  (`claude-account-headroom`). Plan: `docs/plans/claude-accounts.md` §10.
- **A Claude account can be chosen: a default, a priority order, aliases, and
  disabling — in the CLI and on the Accounts page** (#145). Several accounts
  could be added and seen and none picked: slot 1 was the default by constant,
  the order was the slot number, and a role ran elsewhere only through a
  `CLAUDE_CONFIG_DIR` buried in `team bind --env`. Now `aisquare accounts
  default <slot|alias|email>` sets the **machine default**, `--project P` a
  project's, `--role R` a role's (the same binding `aisquare team bind <role>
  --account` writes); `accounts alias 2 work` names a slot so `--account work`
  and the board's `[work]` can say it; `accounts order` and `accounts move`
  set the **priority order** `accounts list` shows (and a headroom-based pick
  will try first); `accounts disable` keeps a slot out of every automatic
  choice while `--account` still reaches it. Every launch — `aisquare launch`,
  `fleet spawn`, a manager spawning a coder — resolves its account in one
  order through one resolver: the flag, the role's binding, the project's
  default, the machine's default, and with none of those set the environment
  is left exactly as it was, so a machine that never ran `accounts default`
  notices nothing. A rung naming an account the machine no longer has refuses
  the launch with the rung named rather than running on another login. The
  arrangement lives in the store (`claude_account`, schema v15) and the
  directories stay the record of which accounts exist; a removed slot's
  default, alias and project defaults go with it, so the next `add` in that
  number inherits nothing. On the Accounts page each row carries ★ *Default*,
  ↑/↓ and *Disable*/*Enable*; the Settings tab binds an account per role; the
  agent header and `fleet ls` show the slot an agent was resolved to. `doctor`
  warns when the default is not signed in or disabled (`claude-account-default`)
  and when a role or project names a missing account (`claude-account-bindings`).
  Slot 1 is labelled `plain claude` (it was `default`, a word that now means
  the chosen account). Plan: `docs/plans/claude-accounts.md` §9.
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

- **`aisquare fleet shutdown`: the fleet's off switch, and the end of rows stuck
  at "unknown (tmux unavailable)".** Measured 2026-09-10: the only way to stop a
  whole fleet was `tmux -L asq kill-server` by hand. After it, every manager row
  kept reading `unknown (tmux unavailable)`, the UI showed dead managers with
  `(pane gone)`, and `fleet reap` reaped 0 — correctly: reap and stop refuse to
  end a row on a server they cannot reach, because an unreachable server is not
  proof a pane died (an earlier release lost live agents' worktrees to exactly
  that inference). Nothing in the CLI could say "yes, I stopped it". Now
  `shutdown` can, because the operator is saying it — and it is scoped,
  confirmable and specific about what it did. **This project by default, `--all`
  for every project** (the shape `fleet reap` already had), and it prints what it
  would end and asks first at a terminal, is a dry run off one without `--yes`,
  and under `--json` without `--yes` prints the plan (`dry_run: true`) and
  changes nothing. Agents on an answering server are stopped as `fleet stop`
  stops one (graceful `/exit` unless `--force`; `agent_exited` is now emitted by
  `stop` itself, so every stop path produces the event, not only `reap`), and
  **the ended rows' claims are released** (`release_claims=True`) instead of
  waiting out the four-hour claim orphan window. Rows whose socket had no server
  are ended as lost and counted apart (`recorded`, never "stopped"), each
  carrying the reason the service actually had — the CLI no longer asserts "its
  server was not running" over a row whose server answered. An agent that exited
  on its own between the snapshot and its turn is read back from the store and
  counted as stopped with the status it recorded, rather than reported as left
  live over an already-ended row. What is killed is the fleet's own
  `asq-<codename>` **sessions**, never the server: `kill-server`
  would take down a hand-made session, one a failed `rename` left under an old
  name, or the operator's personal server if `[fleet] tmux_socket` names it, and
  a server with nothing left on it exits by itself. A `fleet-paused` signal is
  cleared for each project the run CONFIRMED down (and said in the output), so
  the next manager does not come up staffing nothing — and KEPT, named as
  `paused_kept`, for a project with a row left live, a session left up or a
  listing that failed (review round 2). Round 2 also closed three safety holes:
  a row spawned mid-run whose pane cannot be QUERIED is left live and said so
  (a timeout is not a dead pane); the spare-this-session rule follows the pane
  to the session it actually lives in, so an `asq-*` session left under an old
  name is no longer prefix-swept over a row marked LEFT LIVE; and a socket
  whose `list-sessions` fails after a good probe is reported as a failed kill
  (`<socket>:*`), so the command exits 1 instead of printing a clean shutdown
  over a surviving session. `incomplete_projects` in the `--json` report is the
  set every one of those rules reads. The final pass asks reachability with a
  probe that RAISES on an unavailable client (`TmuxServer.reachable()`, rounds
  4-5) — a tmux that left PATH, or a shim whose interpreter is gone, after the
  initial guard is "could not ask", so a late agent on that socket is left live
  and its project stays paused rather than recorded lost. `reachable()` reads
  tmux's own words, not the exit code: `No such file or directory` / `no server
  running on` is absence, anything else (`Permission denied` on a live socket) is
  a `TmuxError` — never a row ended (round 7). Round 7 also: the inside-server
  guard parses `$TMUX` from the right (`rsplit(",", 2)`), so a comma in the
  socket path cannot slip past it; a final row scan the store refused is
  `late_scan_failed` in the report (PARTLY, exit 1, every snapshot project keeps
  its pause); and a forgotten registration's pause is skipped, not read, so
  `shutdown --all` no longer clears the tombstone `project forget` wrote.
  **Exit status is recorded only where
  tmux exposes one**: `--force` kills a live pane and records none. It refuses
  rather than guess — no usable tmux, a socket that cannot be *asked* whether a
  server is there (a wedged server's 30 s timeout used to escape as a traceback
  with no `--json` output), or a call from INSIDE the fleet's own tmux server,
  where the kill would take down the process printing the report. A row whose
  `stop` refused because its pane was seen ALIVE is left live, reported with that
  reason, its session spared, and the command exits 1. Round 9 closed the last
  four places where the report could out-run what tmux confirmed. A
  `kill-window` tmux REFUSED no longer ends the row: it was the one tmux failure
  in `stop` still wrapped in `suppress(TmuxError)`, so a forced stop over a live
  pane returned as *stopped* and released the running agent's claims to the next
  worker; it goes through `_verify_gone` like every other failure there, and an
  unconfirmed stop is LEFT LIVE with its claim and its board session intact.
  Round 10 closed the other door into that outcome: the look `_verify_gone`
  acts on read tmux LENIENTLY, so a socket that answered the probe and then
  refused this user (`Permission denied`, exit 1) read as a pane that is gone —
  row ended, claims released, agent running. It uses the strict reads now, and
  a denied socket leaves the row live with "could not be asked". The report's
  `sessions_failed` line no longer says "refused to kill" over a refused window
  kill or a query that failed, and its `pause_scan_failed` line no longer claims
  every pause is kept beside a project it has just cleared. The
  confirmation plan uses the strict `has_session_or_raise` and refuses on an
  enumeration that fails after a good probe, instead of quietly omitting the
  sessions it could not ask about — the operator confirmed one session and the
  run killed two. A `fleet-paused` signal this could not READ or CLEAR is
  reported in `pause_scan_failed` (kept, named, exit 1) instead of vanishing
  into a blanket `suppress(Exception)` that left the next manager told to spawn
  nothing under an exit-0 "done". And a row that spawns after the kill phase and
  exits on its own now has its retained `remain-on-exit` pane REMOVED with the
  row, so its window no longer holds a session — and the session a server —
  under a shutdown reporting itself complete; a session that survives that is
  spared if it holds a row left live, killed if it does not, and reported either
  way. Board notes and tasks are kept. Doctor's fleet row now decides "the
  server is gone" with `answers()`
  rather than an empty `list-sessions` (which cannot tell an empty server from an
  absent one) and *appends* a scoped `fleet shutdown --project <codename>` to the
  reap advice instead of replacing it. Twenty service tests and eight CLI
  tests, including every refusal, the mixed one-gone-one-healthy socket state, a
  row spawned mid-shutdown, and a forgotten registration's live rows — plus the
  fake tmux made faithful where those paths live (a kill that fails with no
  server up, a kill tmux REFUSES while pane, window and session all survive, an
  `answers()` that can raise, the `running` gate on every write, and a
  per-socket fake) and `fleet shutdown` added to the no-traceback sweeps'
  `UNINVOKED` list, which it was missing: a plain `make test` ran it against the
  developer's real `asq` socket and killed their live fleet. Its read-only plan
  (`--json` without `--yes`) is held to the same property by a test of its own.
  `fleet stop` returns what it released — the ended session's claims ride on a
  `StopReceipt` and are named in the output (`claims_released` under `--json`) —
  and `shutdown` counts that receipt instead of turning the release off and
  making a second one; `fleet reap` reports its releases the same way. A release
  the store refused, or one the board could not be told about, is named and
  exits 1 from all three; an interrupted shutdown kills nothing further and
  still prints how far it got (exit 130); `--all` with `--project` is refused.
- **A `ui-tester` role: user-facing work is verified in a real browser, with
  evidence — and the role brings its own browser flag.** Eight first-class roles
  now. It takes tasks titled `UI: …` from the review pool and runs their
  acceptance steps in whatever browser tooling the window has, in order: Claude
  in Chrome (the window is launched with `--chrome`), the Chrome DevTools MCP,
  a Playwright MCP; it checks which of them answer before starting; it
  measures — screenshots, computed sizes, console errors, network responses —
  and never passes a visual requirement by reading code. `task done` carries
  the evidence; `task reopen` carries a screenshot. With no browser tool it runs
  the non-browser checks and reopens the task as "UI not browser-verified in
  this window", never done. Its verdict names the branch or commit and the URL
  it verified — it gets no worktree, so a task that names neither is reopened as
  underspecified rather than measured against whatever the root holds. **Asked**
  to be read-only, not made read-only: the briefing says never edit and never
  push, and nothing in this checkout enforces it (a PreToolUse allowlist would;
  `--restricted` would not — it removes the Bash its own `task done`/`task
  reopen` need). Ladder `sonnet → opus`, like the other verifiers.
  - **The flag is the role's, not the operator's.** The operator who set this
    up passed `--chrome` in a personal alias; the next operator will not.
    `RoleProfile.default_args` now exists and `harness.role_defaults` applies it
    wherever the role starts — `aisquare launch`, `team spawn`'s printed and
    exec'd command, and therefore every fleet window, which runs `launch` inside
    tmux — only for the default `claude` binary (another agent would reject
    Claude Code's flag), never twice, and never over an explicit `--no-chrome`
    on a binding, a fleet `extra_args`, or the command line, in either spelling
    (`--no-chrome=1` opts out too). That binary question is now ONE predicate,
    `harness.is_default_agent`, shared with `--session-id` pinning: it covers
    the `claude.exe`/`.cmd`/`.ps1` shims and a binding typed with a trailing
    slash, and a withheld flag prints a note instead of degrading in silence.
    `aisquare team harness` reports the role's `default_args` in both its JSON
    row and its text line — the matrix is what an operator reads to find out
    what a role launches with. A fleet spawn now forwards `--command` for any
    binary that was CHOSEN (`resolution.source != "default"`), not just an
    explicit `--bin`: the window re-resolves in the long-lived tmux server's
    environment, which never carries `AISQUARE_BIN_<ROLE>`.
    The fleet's `[fleet.roles.ui-tester]` therefore carries no `extra_args`.
  - **What the machine can and cannot know.** A new `doctor` row, `browser
    tools`, reads what this home's Claude Code directories declare — the
    `enabledPlugins` in `settings.json`, and the `mcpServers` in `.claude.json`
    wherever the layout keeps it (BESIDE the config dir for a default install,
    inside it for a redirected one), per-project blocks included, plus the
    project's own `.mcp.json` minus any server the operator declined
    (`disabledMcpjsonServers`) — and names each tool with the directory that
    declares it. Recognition is a declared table of providers matched against
    the server's `command` and `args` as well as its name, not a substring
    guess on a user-chosen name: `@playwright/mcp` in an `args` array counts,
    `react-devtools` and `file-browser` do not. It says plainly that the Claude
    in Chrome extension cannot be detected from a terminal: the role learns at
    its first tool call. **OK either way**, with the guidance in the detail —
    nothing here is a defect, the role runs without browser tooling and
    degrades honestly, and `install.sh` word-splits its amber list, so a row
    that was amber by design on a healthy machine failed the installer.
  - **The other roles know it exists.** The planner titles user-facing tasks
    `UI: …` and writes their acceptance criteria as browser steps (URL, login,
    action, expected text/pixels/request). The runner leaves `UI:` tasks to a
    ui-tester that is on the board and NOT marked `(stale)` — presence is not
    availability, a crashed tester's row lingers (review round 2) — and
    otherwise runs the non-browser checks and reopens the task as "UI not
    browser-verified", never done. The manager spawns one for `UI:` tasks in
    review. The
    reviewer request-changes a frontend PR whose task carries no ui-tester
    evidence. The lane rule names the ui-tester among the roles that never edit.
  - **Upgrading machines are told.** The default roster gains
    `aisquare-ui-tester`, but a default does not edit an existing
    `config.toml`; `explainability register` now names any first-class role the
    RESOLVED roster lacks — `target.roles`, so a per-target `roles` override is
    read the way registration reads it — and prints the `--role` flags that
    register it — on the target the gap was measured on (`--target <name>`,
    review round 2: without it, `register --target prod` on a staging-active
    machine suggested a command that registered them in staging). On all three
    surfaces: the CLI's note, an
    `unregistered_roles` field in its `--json` payload, and the fleet UI's
    register button.
  - `tests/test_ui_tester_role.py` (49 tests): wired into every list that
    enumerates roles; the briefing's tools, order, measuring, honest degrade,
    which-build rule, reach past the head of the review pool, and unenforced
    read-only; the other roles' mentions, the runner's named verdict and the
    manager's branch/URL prompt; `role_defaults` on binary (including the
    Windows shims, a trailing slash and a wrapper named `claude`), dedupe,
    `--flag=` spellings, the generated opt-out, the withheld-flag note, and the
    one predicate shared with `accepts_session_id`; `launch` argv for ui-tester,
    coder, `--no-chrome`, `--chrome --resume`, another binary, and the identity
    planner seeing the role's own args; `team spawn`'s printed command and the
    harness matrix row; the doctor row across two config dirs with the
    directory named per tool, a default install's `.claude.json` beside the
    directory and a redirected one's inside it, the project `.mcp.json` through
    the real CLI, a declined project server, the name-only false positives, a
    plugin's marketplace half, a provider in `args`, exactly one `hook_sites`
    scan per doctor run, unconnected directories excluded, nothing declared,
    malformed files, and its place in the row order. Plus the roster-gap hint on
    all three register surfaces, and a `tests/test_fleet_docs_are_true.py` gate
    that counts the roles table against `FLEET_ROLES`.
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


- **Explainability setup is doable from the fleet UI.** The Explainability tab
  could already *see* that a machine was unconfigured — it rendered
  `$EXPLAINABILITY_API_KEY is NOT set` beside a red probe — and its five buttons
  all operated on a configuration that had to exist already. So the path to a
  first trace was four terminal commands, one of which (`--proxy-url …:9443`)
  cannot be guessed: the shipped `proxy_url` default is a loopback sidecar this
  CLI deliberately does not manage, while the hosted proxy sits beside the
  gateway. Getting that one wrong is the silent failure above. A **Setup**
  section now takes deployment, gateway URL, proxy URL, prefix and workspace key.
  - A blank field leaves the setting alone, so the same form corrects one value
    later without restating the rest.
  - The deployment field names the entry the settings belong to; **make
    active** beside it is what moves this machine, and the toast says which
    target the machine is on.
  - Type a gateway, leave the proxy blank, and the hosted proxy beside it is
    filled in — offered, never imposed: an explicit value always wins, a proxy
    already configured for the target (its own, or a deliberate top-level one)
    is never overwritten, and a self-hosted adopter with no proxy tier types
    their own. `hosted_proxy_for` returns `None` rather than assembling a URL
    out of half an answer.
  - The prefix field asks for a **name**, not a template: `nishil` becomes
    `nishil-{role}`, and a prefix with a brace in it is refused rather than
    repaired, so nobody types a format string into a form.
  - The key is written to `~/.aisquare/explainability-key` at mode 600 and the
    field is cleared — this view's own docstring already rules a key out of a
    full-screen UI, and a masked `Input` still holds its value. The file is
    read only for the default key variable, so a key typed for a target that
    names its own variable is refused, with the reason.
  - Consent stays a separate press: saving configures, **Enable** enables.
  - One writer. `explainability.configure_target` is lifted out of the Typer
    command so the form and `aisquare explainability enable` are the same write,
    rather than two that agree until they do not.
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
- **Captured is not shown** (#139). Every directory a hooked session ran in was
  auto-registered and listed — 27 projects on the reporting machine, 23 of
  them nothing but captured prompts — and `project forget` came undone on the
  next prompt, because registering was also the revival. The project row now
  carries `onboarded_at` (schema v17): hooks and the MCP server's session only
  *capture* (a row exists, prompt history and injection work, nothing is
  shown), while `init`, `project onboard`, `project link`, `project switch`,
  the sidebar's `+`, `team on` (and `serve` or a role `launch`, which turn it
  on), a fleet spawn (a `restart` or `switch` too, codename or not) or `fleet
  rename`, a project's account default (`accounts default <slot> --project`),
  and a fact written by hand (`context add --project`, `context import`) add a
  project **on purpose**. The sidebar
  and `project list` show onboarded projects only; `a` in the sidebar and
  `project list --all` show the captured ones too (marked); `project prune
  --captured-only [--older-than DAYS]` drops captured directories with no
  context entries and nothing touched in that many days; `forget` clears the
  mark so the next prompt captures silently — the row comes back captured,
  not listed; `doctor` gains a `projects` line with the hidden count, and an
  empty `project list` and `status` say how many are hidden. The migration
  adopts the rows already used on purpose (context entries, a codename,
  linked repos, board activity, a fleet agent, a snapshot on disk; a
  forgotten row never) and hides the rest.
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

- **CI runs on Windows.** The `check` job gains a `windows-latest` leg (3.12;
  the platform branches read `sys.platform` at call time, so a second
  interpreter would only re-run the same branches), and `package` runs on both
  platforms — building the wheel, smoke-testing the console scripts, and
  installing it again WITH the `explainability` extra, whose whole point is a
  collision inside one shared `site-packages/aisquare/` and therefore a
  filesystem question Windows answers differently. Getting there meant fixing
  the suite's own POSIX-only assumptions rather than skipping past them: the
  gbrain fake is now reachable through `PATHEXT`, the #20 bulk-delivery storm
  and both printed-command shell tests are ported instead of skipped, test
  file reads no longer go through the locale codec, and the #56 tilde test
  sets the variable `expanduser` actually reads on each platform.

  Merging 0.5.0 brought ~130 test files that no Windows runner had ever
  executed, and 26 of them were red. They are ported here rather than left for
  later, because a lane that is red on arrival is a lane nobody reads. The
  recurring shape is a POSIX idiom used as a test PREMISE that silently stops
  being one on Windows — which does not fail the test, it makes it pass for
  the wrong reason. `tests/fsperms.py` now owns the two that recur, and
  verifies its own effect rather than trusting the syscall's return:
  `os.chmod(dir, 0o500)` denies nothing on Windows (and nothing under root
  either), and creating a symlink needs a privilege the CI runner holds and a
  developer account does not.

  One class of assertion needed changing rather than skipping, and it is the
  subtlest of the lot: `str(path) in str(some_error)` and `str(path) in
  json.dumps(payload)` are both a raw path compared against an ESCAPED
  rendering of itself — `OSError` renders its filename through `repr()`, and
  JSON escapes backslashes. A Windows path is present in both outputs with
  every separator doubled, and matches neither. POSIX paths carry no
  backslashes, so the escaping is a no-op and the mistake is invisible there.
  Those now assert against the structured value (`exc.filename`,
  `payload["hint"]`) instead of a rendered string.

  Three skips remain, all structural rather than deferred. The stdio-daemon
  leak probe needs each process's ENVIRONMENT to tell our daemons from a
  sibling checkout's and `Win32_Process` carries only the command line, so it
  and its two self-tests are `/proc`-only. Mount-table matching needs POSIX
  path semantics, and Windows has no mount table — the Windows answer
  (`None`, through the existing fail-open) is asserted separately so the
  behaviour is pinned rather than merely skipped.

### Fixed
- **`state.json` has one reader and one writer** (`core.state_file`; review of
  #167). The board's theme, the pinned project and the navigator's width each
  carried their own read-modify-write of the file, and the copies had drifted: a
  file whose top level was not a JSON object raised from the theme's and the
  pin's readers (the fleet UI did not start), the writers replaced such a file
  wholesale — the theme and the pinned project gone without a word — and two
  processes autosaving at once (`asq` and `board -w`) shared one temp file
  name. Now a corrupt file reads as empty and is never overwritten (the save is
  refused, and the fleet UI and the board each say so once — for the width and
  for the theme); every update runs under a lock, so concurrent writers cannot
  lose each other's key; the write goes through a symlinked `state.json` rather
  than severing it, and keeps the file's mode. The pin reads strictly — a file
  that exists but cannot be read raises, rather than silently pointing every
  project command at the working directory — and unpinning what is not pinned
  is a no-op. `project switch` reports a refused pin (`state_unwritable`)
  instead of a traceback; `project forget` and `project prune` finish (rows,
  data directory) and report a pin they could not move, instead of failing
  after the purge had already happened. The fleet UI's and the board's saves
  (the width, the theme) run debounced on a thread of their own, so a held lock
  or a slow disk cannot freeze the UI; at quit every save is given one bounded
  wait together, and anything that still did not land is printed once the
  screen is gone. `config.toml`, `state.json` and the CI descriptor cache share
  one durable replace-by-rename.
- **Agent panes feel like a terminal: the hotkey audit, and the tmux pitfalls
  engineered around** (#147). Every Claude Code shortcut now has a row in
  `docs/fleet.md` saying what reaches the agent (generated from the translation
  table and pinned by tests). Fixes that fell out of it: selecting an agent
  gives its pane the keyboard (typing at what looked like Claude Code used to
  quit the UI on the first `q`); shift+Enter on a tmux below 3.5 travels as
  ctrl+J — Claude Code's newline — instead of being dropped; the private server
  runs with `prefix None` so ctrl+B is Claude Code's background-tasks key in
  `fleet attach` too, and F12 detaches that client; each spawn sets this
  shell's `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`,
  `DBUS_SESSION_BUS_ADDRESS`, `SSH_AUTH_SOCK`, `COLORTERM` and `TERM_PROGRAM`
  on its window (a window inherits the tmux server's environment, frozen at
  its first start, so image paste and agent-forwarded `gh` broke in silence
  after a re-login) and `fleet attach` refreshes the session's copy; and
  `doctor` gains `fleet terminal` — the outer terminal it recognises and
  whether it speaks the kitty keyboard protocol (what decides shift+Enter),
  whether tmux carries extended keys, and whether the running server still
  has a prefix or stale desktop variables.
- **Mouse buttons reach a Claude Code pane** (#148). Only the wheel was
  forwarded, so in the fleet UI a click did not place Claude Code's cursor, its
  `✕`, menu rows and collapsed tool results did nothing, and a drag selected
  nothing in Claude Code (tmux was measured to pass the sequences byte for
  byte — the pane never sent them). Now a program that tracks the mouse gets
  presses, drags and releases as SGR events with the modifier bits (shift 4,
  alt 8, ctrl 16 — so ctrl+click opens a link), X10 when that is what it asked
  for; the UI's own drag-select stands down while the program owns the mouse,
  **shift+drag** always selects locally and copies on release, and after a
  left-button release the UI mirrors a changed tmux paste buffer — what Claude
  Code's copy-on-select writes inside tmux when the agent's environment has no
  display — to your clipboard. Documented in `docs/fleet.md`, with the note that
  Claude Code's selection is copy-only by design.
- **Tapping a key with nothing to type in an agent pane no longer pops a
  warning toast** (#151). Terminals speaking the kitty keyboard protocol
  (kitty, ghostty, wezterm, foot, recent alacritty) report Shift, Control, Alt,
  Super and the locks pressed on their own as key events — and, because Textual
  asks for every key, Menu, PrtSc, Pause, the volume and media keys and the
  keypad's centre too. The pane looked each one up in the tmux key table, found
  nothing, and said `left_shift: tmux has no name for this key — dropped` — one
  red toast per key, three or more into an ordinary typing session, reading as
  tmux failing. None of them is a keystroke, and neither is a macOS Cmd chord
  (a command for the terminal, not a request to type the letter under it), so
  none of them is mentioned: `core.keys.translate` now says *why* a key was
  not sent, and the pane speaks from that reason. A chord you could have meant
  — a modifier held, or a function key past the twelve tmux knows — keeps its
  notice, once per key name in a pane, but as information — `no way to type
  f13 into a tmux pane` — since tmux did not fail. A chord your tmux is too
  old to carry (shift+Enter aside, which travels as ctrl+J — #147) is the one
  loss you can fix, and says so as a warning that names the version you have
  and the one it needs (`tmux 3.4 cannot carry ctrl+shift+enter — 3.5 or newer
  can`), once per key name for each tmux server; restarting an agent or
  signing in again repeats neither line. A chord on a
  character beyond ASCII that arrives without its text (`ctrl+à` from the
  AZERTY 0 key, `alt+shift+ß`) gets the quiet `no way to type` line too,
  where it used to go out under a tmux name that arrived as a bare `à` or was
  typed into the agent as `M-SS`. Plain shift is not such a chord and still
  types the capital (`shift+ф` is `Ф`; AZERTY's `é` key, which shifts to `2`,
  still types `É` — a terminal that reports the text types the `2`), but a
  letter with no case (`ש`, `क`) or a two-letter capital (`ß`) is named rather
  than typed unshifted or as `SS`. A key named after its character (`§`,
  `±`, `«`, `。`, `؟` on a non-US layout) and the keypad's operators, which a
  terminal may name without reporting their text, are typed rather than filed
  with the keys nobody pressed — except the keypad's decimal and separator,
  whose text only the layout knows: named once, never guessed. The silent
  keys are a closed set (a modifier, a lock, Menu, PrtSc, Pause, the media and
  volume keys, the keypad's centre — with or without a modifier held — and a
  Cmd chord); anything else the pane cannot type is named, so a symbol it has
  no table for is a quiet line and never a keystroke lost without a trace, and
  a raw control byte a terminal reports as a key is spelt `U+0085` in that
  line rather than sent to your screen.
- **Auto mode behind the explainability proxy is named, not mistaken for a
  model outage** (#150). Fleet agents in `auto` permission mode traced through
  the proxy were refused on every tool call — *"claude-opus-5[1m] is
  temporarily unavailable (server error), so auto mode cannot determine the
  safety of Bash"* — for hours, while the chat kept working and the API was
  up: auto mode's separate, non-streaming **classifier** request fails behind
  the proxy once the session is large, and a fleet agent's first request is
  already ~140k tokens on a machine with several MCP connectors, so the
  manager and its coders were refused from their first shell command. The fix
  is the proxy's (AISquare-Explainability-SDK#1144); meanwhile: `aisquare
  doctor` gains `explainability auto-mode` — present when a fleet role runs
  `auto` behind a configured proxy, it reads the first-turn size of recent
  sessions from their transcripts and warns above ~100k tokens or when a
  recent session was refused three times or more; `fleet spawn` carries the
  same warning on its receipt, and so do `fleet restart` and `fleet switch`,
  naming the role's mode fix for later spawns and `fleet restart <label>
  --permission-mode acceptEdits`, under the agent's own label, for the agent
  itself — a restart replays the mode it was launched with (#144), so the
  role's fix alone never reaches it; a session launched through
  the proxy and refused three times is put in 🔔 attention by its Stop hook
  with one `auto_mode_blocked` board line, or, when a restart or switch is
  taking it down, leaves both to the replacement that resumes it, judged on
  the refusals it adds; the mode fix they print is one the config
  accepts (`config set` for a role the config lists, the TOML table for one
  it leaves out); and the docs name the signature and the three ways round it
  (a non-classifier mode per role, a lighter config dir, tracing off).
- **An exited agent can be restarted from the UI, and a dead manager no
  longer blocks its own replacement** (#138). A manager whose Claude Code was
  ended with ctrl+c inside its window sat on the sidebar as 💤 forever: the
  screen derived `exited` from the dead pane while its row stayed "live" until
  a hand-run `fleet reap`, so `fleet spawn manager` refused with "already has
  a manager", wake-ups targeted a dead pane, and the agent view had no
  action. Now every listing (`fleet ls`, the UI's tick) records a dead pane
  as ended the way `reap` does — exit status, the claims a crash left held
  released, `agent_exited` on the board, the manager nudged — and `fleet
  spawn` does the same before its checks, so a dead manager is replaceable at
  once (a row a hand-over is stopping is left to it, for a few minutes at
  most). The row stays on the live listing as **💤 exited** (the word, not
  only the glyph) for a day while tmux still holds its window, and the agent
  view gains **Stop** and **Restart**.
  `aisquare fleet restart <label> [--fresh]` — and the button — starts the
  agent again under its own label with the same role, task, worktree and
  account, **resuming its session** from its transcript when that is on disk
  (`claude --resume <transcript>`), else fresh with a hand-off prompt from the
  board; a running agent is stopped first and handed over as `fleet switch`
  hands one over (its claims wait for the replacement and no exit is
  announced), a resumed one is typed one line telling it to carry on, one
  whose role, task, account or binary would refuse the restart is refused
  before it is stopped, one a hand-over is already moving is refused (and so
  is a second `fleet switch` of it), and a refused restart
  leaves the 💤 row and its last screen as they were. **Stop** on an exited row
  removes the dead window, and so does spawning the same label again once the
  replacement is up (it supersedes the old window — no two rows called
  manager); the view's buttons act on the row it shows, never on a replacement
  that took its label since. The Manager tab says *manager exited (130)* over
  its Start button instead of "no manager yet". `doctor` warns when a
  project's manager exited while its agents still run (`fleet-manager`).
- **Fleet windows are born the width they will be shown, and a headless one
  under the width at which Claude Code grows its diff panel on its own**
  (#149). Every window started at 200x50 and only shrank to its pane when the
  UI attached it; past 144 columns Claude Code's fullscreen renderer opens the
  diff panel by itself as soon as a file is edited, remembers that for later
  sessions, and inside the fleet nothing could close it — clicks are not
  forwarded (#148) and a `/diff` typed while Claude works is queued. The
  default is now 120x40 (`core.tmux.DEFAULT_WINDOW_WIDTH/HEIGHT`, under 144
  and above the 110 `/diff` needs on demand), and a spawn from the UI's
  *Start manager* is born at the size its pane will have — a pane that is
  itself 144 columns or wider still shows the panel. A window added to a
  running session keeps its size under `fleet attach` instead of following the
  attached terminal, which would leave it that wide after the detach; the
  session's first window follows it until the UI has shown it. `docs/fleet.md`
  says how to close a panel that did open: `/diff` once the agent is idle.
- **The sidebar bell rings for a real prompt, not for every notification**
  (#153). Every Claude Code `Notification` flipped a session to 🔔 `attention`,
  and 164 of the 183 bells on the reporting machine were the routine idle notice
  ("Claude is waiting for your input") — nine bells in ten with nothing to
  answer. The hook now reads `notification_type`: `permission_prompt`,
  `elicitation_dialog`, `elicitation_url_dialog` and `agent_needs_input` ring
  the bell; `idle_prompt` (and an elicitation closing) changes nothing — the
  agent is `waiting`, which the board already says; `auth_success`, the
  `quota_auto_resume_*` family, a sub-agent finishing and any type this build
  does not know become a `notice` feed line, never a bell. A payload without
  the field (an older Claude Code) is routed by its text, so the idle notice is
  quiet there too and everything else keeps its old behaviour. A `notice`, like
  the bell, is for the human board: it never reaches a teammate's prompt delta
  or a manager's wake-up. In the fleet (the sidebar, the project view,
  `fleet ls`) the bell also clears while the pane is producing output after
  the notice — a permission that was granted, or an action the classifier
  approved — not only on the next human prompt; a pane that goes quiet again
  without a Stop (a prompt dismissed with Esc) reads 🔔 again. `aisquare watch`
  and the team board show the session row itself, which keeps 🔔 until the turn
  ends.
- **A usage reset now says when, not just what o'clock** (#152). `aisquare
  accounts usage`, `accounts list --usage` and the Accounts page showed a reset
  as a bare `HH:MM`, which for the seven-day window can be six days away and
  read as tonight, and around midnight could not tell a reset in ten minutes
  from one a day out. Both surfaces now render the same string from one
  formatter (`cli.common.format_reset`): `in 12m` within the hour, `in 3h 10m
  (18:00)` later today, `in 2d 4h (Tue 02:00)` on another day — a weekly reset
  is never a bare clock time again — and `now` when the reading is already
  stale. `--json` is unchanged (ISO 8601 timestamps). A test walks both
  modules' syntax trees so a third copy of the formatter cannot quietly return.
- **Alt+letter chords reach the agent as chords.** Claude Code's alt+p (switch
  model) did nothing from a fleet pane — reported 2026-09-02 and again
  2026-09-10 — because Textual's parser reads `ESC p` as `Key("alt+p",
  character="p")` and the key table's "printable input is literal" rule sent the
  bare letter. With alt or meta held the chord is the meaning; the character is
  only how the terminal spelt it, and `translate` now says `M-p`. ASCII letters
  only, because that is all a parser ever delivers with an alt token and a
  character — measured by feeding Textual's own parser the bytes a terminal
  sends, not against hand-built events, which had promised alt+digit and
  alt+space chords a legacy terminal cannot produce (its `ESC 1` reaches the
  parser as `¡` and its `ESC Space` as a plain space, with no alt at all; they
  are typed as such). `M-1` and `M-Space` are real where the terminal sends the
  chord itself, under the kitty keyboard protocol, and reach the agent from
  there. Alt on punctuation stays the character, since through the name table it
  was dropped (`;`) or became `ESC [`, the control-sequence introducer, and every
  name this module emits was measured against a real tmux — `M-é` never was. An
  alt chord on a shifted letter keeps its case (`M-A`; a kitty `meta+P` used to
  come out as a lowercase `M-p`) except N, O and P, whose `ESC` forms are the
  SS2, SS3 and DCS introducers a program's key parser joins with the next key —
  those type the letter. Shift and ctrl keep the existing rule. A modifier tmux
  cannot spell — `super`/`hyper`, which is how macOS Cmd arrives — now drops the
  key instead of falling through to its character, so Cmd+V no longer types a
  `v`. A chord tmux has no name for (`alt+shift+1` — the shifted key is
  layout-specific) falls back to the character the terminal reported, so it
  still types what it always typed; one an old server cannot carry is dropped
  rather than mistyped. The parser's limits are documented in `docs/fleet.md`:
  a terminal that reports the text a key produced (macOS Option) has the `alt`
  token dropped by Textual, `ESC b`/`ESC f` are read as ctrl+arrows, ctrl+alt
  on a letter loses the alt, and Escape typed within ~100 ms before a letter
  reads as that chord.
- **A spawned agent is told the task it was spawned for.** `fleet spawn --task`
  recorded the task on the agent's row and named the label and branch after it
  — and stopped there: the session inside received the generic board and its
  role's standing cycle, whose `task next` hands out the *oldest* ready task. A
  coder spawned for task B took task A; two spawned together raced for the same
  one while their own sat idle; the manager ended up posting "you are coder-x,
  run task show …" notes by hand (observed 2026-09-10). `fleet spawn` already
  set `AISQUARE_FLEET_AGENT` on the window; the session-start hook now reads it,
  joins the session to its row, and puts an **ASSIGNED TO YOU** block at the top
  of the briefing saying what that task's state asks of *this* role — claim it,
  verify it, do the rework it came back from review for, clear what blocks it,
  or leave it with the verifier when it is the agent's own work already in
  review. Every role whose cycle pulls from the review pool counts as a
  verifier — `ui-tester` included, which was being told to rework the very
  work it was spawned to check. Every instruction is one the commands would
  honour: "claim it" only when `task next` would hand the task out (its needs
  done) and `task claim` would accept it — so a `doing` task whose holder's
  lease has run out is offered, not guarded — and a verifier is never told to
  claim anything: a task it reopened, or one blocked or being worked, is
  somebody else's turn until it is back in review. The one branch that tells an
  agent to stand down and ask the manager is the one that earns it: a teammate
  live on the task right now. An agent meeting its OWN claimed task after a
  `/clear` carries on — Claude Code fires `SessionEnd(reason: clear)` for the
  old id *before* the `SessionStart` of the new one, and the end hook used to
  release the claim into that gap; it now keeps a fleet agent's claims across a
  clear, and the start that follows moves every one of them (the assigned task
  and the pool work it took, in every status that keeps a claim) onto the new
  id together with the row, in one store transaction, so the board names a
  session that exists and a looper's `task next --claim` in between finds the
  task still held. `task next` puts the caller's assigned task first through
  the same query as every other candidate, so parallel spawns stop racing — for
  the tester, runner and reviewer cycles too, which now pass `--as`, and for the
  MCP server's `task_next`, which runs under the agent's window and takes the
  order (never a claim) from it. `AISQUARE_FLEET_AGENT` is inherited by every
  process the agent starts, so a nested `claude -p` reaches both the hook and
  `task next`: the row belongs to the *process* in its pane — Claude Code hands
  every hook the pid of the process that fired it (`CLAUDE_PID`), and the hook
  compares it with the pane's — so a child is neither briefed on nor able to
  claim its parent's task, whatever start it reports (`resume`, `fork` and
  `compact` are all things a child can say), and the manager's task-less row
  is not rebound under a child either; a binary that exports no pid binds its
  row on first arrival and keeps that session. An assignment ends with its
  task: once the task is done or dropped the row forgets it, so a later clear
  or compaction is not re-briefed on finished work — each such briefing used
  to tell the agent to send the manager a question note, and each note woke
  the manager for nothing — and a reopened task claimed by someone else is no
  order to stand down. `fleet spawn --task` refuses a task that is already
  `done` or `dropped`. The whole lookup is fail-open on BOTH doors — the
  briefing's and `task next`'s — as its docstring always claimed: a damaged or
  locked store, or a tmux that does not answer, costs the assignment line,
  never the board and never the work loop. The first-prompt board an agent
  gets when it meets the orchestrator late shows the claim under its new
  holder, not the old one. And `fleet spawn`'s `AISQUARE_FLEET_AGENT` no longer
  lingers in the tmux *session* environment after the first window: a window
  opened by hand in the fleet's session used to inherit the first agent's row.
  The row is written after the window starts, so `aisquare launch` inside the
  window now waits for it before starting the agent — a slow or locked store,
  a relabel or the cap check can delay the agent's start, never strip its
  briefing. A `/clear`'s hand-off proves the process twice, in two hooks; when
  tmux fails to answer the second, the bind is tried again at the agent's next
  prompt and the **ASSIGNED TO YOU** block arrives with it, once — and a row
  that ends (`fleet stop`, `fleet reap`) releases whatever its session still
  held, so a claim parked for a clear never outlives the row it was parked
  for, and a killed agent's claims go back to the pool with its row rather
  than sitting `doing` under a dead holder for the length of the lease. When
  an assignment ends, the agent header's `task …` chip goes with it; the label
  and branch keep the task's short id.
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
- **Select and copy text in an agent pane.** Reported from WSL2 + Windows
  Terminal (2026-09-03): "not able to select and copy text" — an agent printed a
  command and there was no way to take it. Drag-select was switched off on the
  widget: a Line API widget has no `render()` for Textual's default selection to
  read, and switched on alone every drag resolved to select-all, because the
  compositor takes the drag's content offset from segment metadata only the
  `render()` path stamped. The pane now stamps the row the terminal library
  reads when it resolves a press or a drag (that row only — see below), supplies
  its own extraction (a drag in the blank area below the output used to raise
  out of the handler), and paints the span itself — as cells, so a row with wide
  glyphs highlights what is copied, and tinting behind the text rather than over
  it, since the theme's selection style resolves with foreground equal to
  background. The text is copied when the gesture ends, wherever on screen it
  ends — the app sees every press and release itself and tells the panes, so a drag that
  crosses the pane's edge copies in either direction instead of depending on
  whether the neighbouring widget happens to capture the mouse. Only a
  left-button gesture that actually changed a pane's selection copies: a
  right-button drag across a standing highlight leaves the clipboard alone, and
  so does a release with nothing to do with a pane — a drag on the footer, a
  scrollbar, a button. ctrl+c copies again while a selection stands — from the
  pane or from the sidebar, through one path — and clears it; otherwise it is
  the agent's interrupt. A drag over nothing but blank cells — the empty rows
  under an agent's output — copies nothing, says "nothing to copy" and leaves
  no highlight behind, so a highlight that stands always has text for ctrl+c
  to copy; a click whose pointer slips a cell there says nothing. cmd+c is
  only ever the copy, and types nothing when there is no selection. The
  highlight does not outlive what it means: a key or a paste into the agent
  drops it, so does the agent printing something else under it, so a later
  ctrl+c is the interrupt and never a copy of text nobody selected. Double-click
  selects a word, a triple click nothing, and the pane is never selected whole
  — not by a triple click on its header either (Textual's defaults would select
  the whole pane, and the next ctrl+c would copy it instead of interrupting the
  agent). A click is a press and a release in one cell, so a drag followed by a
  click on its end cell is not a double click. The app reads every press and
  release itself, before they bubble, so a burst of input handled back-to-back
  cannot route a release with the previous gesture's button; and an empty copy
  never reaches the terminal, where an empty OSC 52 clears the clipboard.
  The `(exited 0)` notice row is tinted by the drag that copies it, like every
  other row, and so is the `[↑k/history]` marker — whatever a row displays is
  what it highlights and what it copies, cut to the columns the pane shows
  rather than to the width of a tmux window that outgrew it. A line tmux
  soft-wrapped is copied as one line (every frame carries tmux's own wrap
  marks — `capture-pane -F`, tmux 3.7 and later — so the copy joins the rows
  it shows, keeping a space that fell on the wrap, without a process of its
  own; an older tmux gets one line per row), a tab is
  expanded to the cells it occupies on screen so what is highlighted is what
  the eye sees, and an emoji or a wide glyph is one unit to the highlight, the
  cursor and the copy alike — the paint, the offsets the terminal library
  resolves a drag with and the copied text share one grapheme model of the
  row, cached beside the row's Strip rather than rebuilt for the cursor row
  on every frame. The tint is visible on reverse-video cells too — under a
  theme whose selection style names no background as well, where the fallback
  used to draw the glyph in its own background — and the cursor stays
  visible inside a highlight. Painting no longer stamps every row with
  selection offsets: that gave each segment a unique link id and made a plain
  mouse hover repaint the whole pane (120 pointer moves on a 200x60 pane: 7200
  row renders, now 0), doubled the CPU per streamed frame (8.2 → 4.0 ms) and
  held twice the memory in the strip cache; only the terminal library's own
  offset lookup is stamped now. The highlight and the clipboard read the same
  rows at the same moment, so they cannot disagree:
  under an agent that is still printing, a drag copies the text at release.
  Switching the pane to another agent drops the selection, so does hiding the
  pane behind another tab or unmounting it, and changing the theme drops the
  highlight's resolved colour so a theme picked mid-drag does not leave the
  tint in the old palette. What decides whether a frame dropped the highlight
  is the row as displayed: a `(pane gone)` notice replacing the row drops it
  like any other change of the text, while a change hidden under the
  `[↑k/history]` marker leaves it. A right click seeds no double click, so a
  right click followed by a left click in the same cell selects nothing; the
  copy key outside a pane takes the highlight made most recently when two
  panes hold one; and the end of a gesture is routed even when the app's own
  handling of it raises, so no press stays armed for the next gesture.
- **One session is ONE Run again — the launcher owns the Run's trace id.**
  Measured against a production workspace on 2026-09-09: one
  `aisquare launch coder -p …` produced TWO dashboard Runs. `5efb96de…` held the
  model traffic (157,756 tokens) under `aisquare-coder`; `6fb49942…` held the
  same session's client lane — the prompt, the board events — with zero
  tokens, same agent name, twenty seconds later. `docs/explainability-tracing-boundary.md`
  had this exact merge down as **[unverified]**; this is the measurement, and it
  came out false.
  - **Why.** The gateway materialises a Run per OTel `trace_id`
    (`trace_states.trace_id` *is* the dashboard's `run_id`). `X-Pipeline-Id`,
    the value both lanes shared, is `agent.run_id` — an attribute on a span,
    searchable, not the key. The proxy mints a random trace per pipeline
    session (`_open_pipeline_session`); `ship_once` opened `AgentRunTracer`,
    which starts a new trace unconditionally (`INVALID_SPAN` context). Two
    writers, two trace ids, two Runs — every time, by construction.
  - **The fix.** The pipeline id is now the SOURCE of the trace id:
    `trace_identity(pipeline_id)` is SHA-256 of it, first 16 bytes the trace
    id, next 8 the root span id. `wire_session` posts the Run's root span with
    those ids to `/v1/traces/ingest` **before** the agent starts
    (`explainability_ops.open_run_root`, 3 s budget, fail-open), and hands the
    proxy a `traceparent: 00-<trace>-<root>-01` — the proxy's tier-2 path,
    which parents every model span under that root. A launch whose root was
    accepted OWNS its Run, and `ship_once` attaches that session's client
    lane as a segment under the same remote root (`_ClientLaneSegment`). One
    trace, one Run, both lanes.
  - **Ownership, not the run key, decides the client lane.** The shipper takes
    the segment path only for records that carry the OWNED trace id, which
    `insights` now spools as `run_trace_id` beside `run_key` (marker
    `AISQUARE_RUN_TRACE_ID`, exported only when the root was posted; no
    session-id fallback, unlike the run key). The run key is no evidence of a
    root: it falls back to the board session id, so every plain session has
    one, and an earlier cut of this fix read that as "launched" and parented a
    whole drain under a root nobody had posted — the gateway then elected the
    orphaned segment as a pseudo-root, and the plain session that had always
    produced a well-formed Run produced a rootless one. Now a plain session
    and every fail-open launch open `AgentRunTracer` exactly as before: a Run
    of their own, with a root. Two Runs for a fail-open session is the old,
    known cost; one Run with no root is worse than the bug it was fixing.
  - **`traceparent` REPLACES `X-Pipeline-Id` on the wire.** The proxy resolves a
    request in tiers and `X-Pipeline-Id` is tier 1: present, it opens its own
    session and never reads the `traceparent` beside it, so sending both would
    change nothing. The pipeline id still travels as `agent.run_id` on the root
    and as the `AISQUARE_PIPELINE_ID` marker, so `by-agent-run-id` lookups and
    the board join are unchanged.
  - **Fail-open, in one direction.** No gateway URL, no key, or a root that was
    not accepted with 202 → the pre-fix wiring, byte for byte: `X-Pipeline-Id`,
    the proxy keys the Run, no `AISQUARE_RUN_TRACE_ID` is exported, and the
    launch line says so (`the proxy keys the run — root not posted: …`).
    Tracing still never costs a launch. A plain session with no proxy lane
    still ships through `AgentRunTracer` as before — it has nothing to join.
  - **`aisquare explainability env` writes nothing to the gateway by default;
    the printed `team spawn` command opts in with `--post-root`.** `env` is a
    print-only command, and it used to post a Run root like a launch does —
    with a gateway URL configured, every invocation minted a dashboard Run of
    one 0 ms span and zero tokens, named after the role, for a session that
    might never start (a fresh pipeline id per call without `--session-id`:
    a second terminal, a shell rc, a `--json` reader), after up to 3 s of WAN
    I/O behind a print. `wire_session` gains `post_root=False` and `env`
    passes it by default: the probe, the guards and the header pair still
    run, the root is not posted, and the delta is the proxy-keyed form —
    `X-Pipeline-Id`, no `traceparent`, no `AISQUARE_RUN_TRACE_ID`; the other
    exports are unchanged. That justification — no agent may ever start on
    the id a print minted — is true of a bare `env` and false of the line
    `team spawn` prints, where the agent starts on the very next command in
    the same shell; an earlier cut of this fix collapsed the two and put the
    default paste path, the one the CLI tells the operator to run, back on
    the two-Runs fallback. So `env` gains `--post-root` and the printed
    command evals `aisquare explainability env <role> --post-root`: the root
    is posted first exactly as `launch` and `team spawn --exec` do, the
    pasted session owns its Run — `traceparent` on the wire,
    `AISQUARE_RUN_TRACE_ID` exported — and the launch line goes to stderr,
    where an eval leaves it for the human. Same fail-open in the same
    direction: a refused root falls back to `X-Pipeline-Id` with no run key
    exported, a dead proxy to untraced, and neither costs the paste. A bare
    `aisquare explainability env <role>` still makes zero gateway calls. The
    flag is visible in `--help` rather than hidden, because the line that
    carries it is printed for a human to read and a flag the CLI disowns is
    a trap; its help text says when adding it by hand is wrong. Not the
    SessionStart hook: that path may never open a socket, and
    `tests/test_no_network_on_the_primary_path.py` pins it.
  - **The `team spawn` prelude clears every trace marker.** The printed
    command's `unset` list was hand-written and missed `AISQUARE_RUN_TRACE_ID`,
    so two pastes in one shell could share a Run: paste 1 exported it, paste 2
    cleared the other four, and if paste 2's own root post was refused the
    stale id survived and session 2's hook joined session 1's Run. The list is
    now `core.spawn.IDENTITY_ENV_VARS` — the tuple every stripping seam
    already removes — so a new marker cannot be missed again.
  - **The Run is now findable from the board row.** `joins.jsonl` grows a
    `trace_id` field (the marker `AISQUARE_RUN_TRACE_ID`, copied by the hook;
    `null` when the proxy keyed the Run rather than a derived id that was never
    used), and it is exactly the id `GET /v1/workspaces/{ws}/runs/{run_id}`
    reads back with the workspace key — measured live: the workspace routes
    accept `X-API-KEY`, only the studio-scoped ones answer 403. The
    findings-loop page's field table gains the row. The launch line prints it:
    `traced as aisquare-coder (pipeline <session>, run <trace_id>)`.
  - **The SDK's inbox stays in `~/.aisquare`.** Its delivery inbox is a SQLite
    file at a RELATIVE default path, so every drain left
    `explainability_inbox.db` (+ `-shm`, `-wal`) in whatever directory it ran
    from — a repo root by hand, `$HOME` from the cron timer step 10 of the guide
    installs. `_init_sdk` now pins `EXPLAINABILITY_INBOX_PATH` to
    `~/.aisquare/explainability/inbox.db` unless the operator set it — and
    creates that directory first (review round 2): the SDK's inbox writer opens
    SQLite without making parents, so on a fresh home the pin alone left every
    drain deferred with `unable to open database file`. An operator-supplied
    path is neither replaced nor created.
  - **A mistyped gateway URL stays fail-open** (review round 2). A URL with no
    `http(s)://` scheme made `urllib`'s `Request` constructor raise before the
    request's own error handling — and one the parser itself rejects
    (`https://[::1`) raised from `urlsplit` too (review round 3), and `http.client`'s
    own `InvalidURL` (`:badport`, an unescaped space) and `IncompleteRead` (a body
    shorter than its Content-Length, on a 202 or inside a 503's error body) escaped
    the handler as well (round 4), as did a timeout or connection reset while
    reading a 503's body (round 5) — so tracing stopped the agent from starting;
    it is now a failed root receipt (`not a usable URL: …`) and the launch falls
    back to the proxy-keyed Run with the reason on the launch line.
  - **What the live check did and did not verify.** Re-measured against
    production after the change, for an owned launch: one Run per session, the
    model spans and the prompt span under one trace id. It read tokens and
    node presence, not the Run's `status` or `end_time` — the root is posted
    already ended, and what the gateway makes of that is an open question,
    written down as a known limitation in
    `docs/explainability-tracing-boundary.md`; the fix, if one is needed, is
    gateway-side and not in this release.
  - `tests/test_one_run_per_session.py` pins each piece: the derivation, both
    wiring outcomes and the no-post cases, the root span's shape and the 202
    rule, the marker and join record, the shipper's lane choice (a launched
    session with an owned root: a segment under the derived remote parent,
    `AgentRunTracer` never opened; a plain session and each fail-open cause,
    driven through the real `insights` spool rather than a hand-built record:
    `AgentRunTracer`, never a segment; the segment closed and the context
    detached on failure), the inbox path, one launch through the CLI, the
    print-only mode (`env` with a gateway and key configured makes zero
    network calls; `team spawn --exec` still posts the root), the opt-in
    (`env --post-root` posts the root and exports the run key, and exports
    none when the root is refused), and the paste path for real: the printed
    `team spawn` command run through `/bin/sh` against a loopback proxy and
    gateway posts ONE root and starts a stub agent with `traceparent`, the
    run key and `--session-id` all naming the same id — and still starts it
    when the gateway refuses. `tests/test_harness.py` runs the printed spawn
    prelude through `/bin/sh` with a stale `AISQUARE_RUN_TRACE_ID` set and
    asserts every marker is gone.
  - **A turn's `started_at` is the moment the hook was entered.** The prompt is
    recorded and spooled before CI is consulted, and the stamp was taken after
    that store work, so `wall_ms` lost however long the store took — and
    `test_the_row_starts_when_the_turn_did_not_when_the_call_returned` flaked
    on cold runners (2026-09-09: `main` after #109 on py3.11, then this
    branch's merge commit on `ambient (proxy-up)`; 112 ms against a 100 ms
    budget). `capture_prompt` and `session_start_context` now stamp first and
    pass `began=` into `ci_augment`; a regression test holds the store block
    for 250 ms and asserts the row still starts at entry.
- **Every role's briefing now closes with a lane rule: stay in the role, and here is
  what to do INSTEAD.** Measured 2026-09-10 on a real board: an operator opened
  `aisquare launch planner`, typed "get it fixed in the same PR, update the body
  and comment", and the planner edited four files, committed and pushed — while
  two coder sessions sat on an empty task list. The planner's briefing said its
  job and "never code"; a standing note loses to a direct instruction for
  something else unless it names the trigger and the substitute action, and it
  named none. `role_cycle` now appends `_lane_rule` to every first-class role
  (a seat is briefed as its role; an unknown role gets none): three lines that
  name the role's OWN trigger and substitute from `_LANE`, a `(trigger,
  instead)` pair per role — planner (asked to fix or build): add the tasks and
  tell the human to prompt each coder tab with "check the board"; coder (asked
  to verify, review or plan its own work): do the task, verification is the
  runner's, planning the planner's; runner/tester (asked to edit): `task
  reopen` with the failure, the coder fixes; reviewer/validator (asked to
  edit): findings, the coder fixes; manager (asked to code): `fleet spawn
  coder`. Every substitute is a command that runs as written — `--as <sid>`
  and the task id are pre-filled exactly as the core cycle pre-fills its own —
  and no role reads another role's trigger, so adding a role adds one dict
  entry and rewrites no shared sentence. Reading code to understand a problem
  stays allowed. The human can still override — say once who owns it and offer
  the command — so the role is left by decision, never by accident. "Never
  merge" is added only where the role's own cycle does not already say it, and
  a role without a lane entry gets no paragraph rather than a `KeyError` the
  session-start hook would swallow with the whole team block. Pinned by
  `test_every_first_class_cycle_ends_with_its_own_lane_rule`,
  `test_the_lane_rules_substitute_commands_run_as_written`,
  `test_no_role_reads_never_merge_twice` and two role-specific tests; the
  manager-cycle test asserts its lane; README and `docs/fleet.md` say it in one
  line each.

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
- **`~/.aisquare` is no longer treated as a project root.** `.aisquare` is
  overloaded — `<project>/.aisquare` is the opt-in project marker, but
  `~/.aisquare` is where config, the context database and the agent registry
  live. The marker walk could not tell them apart, so **every markerless
  directory under `$HOME` resolved to `$HOME`** and shared one context pool.
  It also defeated the guard written against exactly that: `serve --stdio`
  refuses to activate a directory that is not a project root *because* Claude
  Desktop launches from `$HOME`, and since `$HOME` always holds `.aisquare`
  the refusal never fired — the server silently activated the home directory
  instead. An aisquare home is now recognised by its layout and skipped as a
  marker. A hand-made `<project>/.aisquare` still works, and `.git`/`.hg`
  are untouched. Not a Windows bug, though Windows shows it most (temporary
  directories live under `%USERPROFILE%`).
- **Credentials and the serve token are now restricted on Windows.**
  `chmod(0o600)` is the whole story on POSIX and does nothing on NTFS, where
  the group/other bits have no equivalent — so the API key and the bearer
  token guarding the HTTP server stayed readable by every other account on the
  machine, with no error to say so. The credentials file that holds both now
  goes through one `icacls` call, run from System32 by its full path: its
  inherited entries are stripped, `Users`, `Everyone` and `Authenticated
  Users` are removed by SID, and the owner is granted. The removal matters
  because an explicit `BUILTIN\Users` ACE survives the obvious
  `/inheritance:r` + `/grant:r` pairing and would have left the file readable
  by everyone anyway. It is not a reset: an explicit grant to any other
  principal is left in place. The DACL is read back afterwards, and such a
  grant counts as NOT restricted. `SYSTEM` and `Administrators` entries can
  remain, as root does for a 0600 file on POSIX. The single credentials
  writer reports whether the restriction actually landed, so `init`, `serve`
  and `login` say so explicitly when it did not, rather than implying a
  protection that is not there. POSIX behaviour is unchanged.
- **A config write no longer fails because someone was reading the file.**
  `os.replace` is atomic on POSIX and a concurrent reader keeps its own inode;
  on NTFS `MoveFileEx` refuses to replace a file that ANY other handle has
  open, including one opened purely for reading, and for the width of that
  rename the reader takes an `Access is denied` of its own. Both directions
  were measured under a read/write storm. The reader half was the more
  expensive: `cli/launch.py` treats an unreadable config as "launch untraced"
  by design, so a config write racing a launch silently cost tracing with
  nothing raised anywhere to say so. Both sides now retry through one bounded
  helper (~0.9s, then the original error unchanged). The two paths report
  contention DIFFERENTLY — `os.replace` sets `winerror` 5/32, `Path.open`
  goes through the C runtime and sets `errno` 13 with `winerror` **None** —
  and matching only the obvious one covered just the writer.
- **A read racing a credentials write can no longer corrupt the API key.**
  `store` and `drop` rewrote `~/.aisquare/credentials` in place, which
  truncates it first, and took no lock. A `load_all` in that window read an
  empty file or half a JSON document, and the half document came back as a
  pre-JSON bare key. The next `store` wrote it into `api_key`, and the serve
  token and the IAM session went with it. Two writers at once also lost
  whichever key the first one added. The file is now replaced by rename
  (`core.atomic.write_replacing`, with the Windows contention retry) under a
  lock on `credentials.lock`, and only one non-blank line that could not start
  a JSON value is migrated as a bare key. A rename publishes a NEW file, so
  two things the in-place write got for free are now done on purpose: the
  temp is created 0600 and restricted to this account (the DACL, on Windows)
  while it is still empty, so the secrets never sit under the permissions the
  home hands down; and a file that exists but cannot be read (a root-owned
  one left by `sudo`) is refused rather than replaced by the one key being
  stored; a sign-out that cannot read it fails and says so, rather than
  reporting a session gone that is still on disk. A sign-out whose rewrite of
  the remaining keys could not be restricted says so, as a sign-in does: on
  stderr from `aisquare logout`, and in the fleet UI's Accounts page.
- **A limit message's named zone is read on Windows too.** Windows has no IANA
  time zone database, so `ZoneInfo("America/Toronto")` raised there and a
  reset named in a zone fell back to the offset in force now: a weekly reset
  across a DST change came out an hour off. `tzdata` is now a dependency on
  Windows only, where `zoneinfo` looks for it; Linux and macOS keep reading
  the system database.
- **The explainability workspace key is restricted on Windows too.** The third
  secret file to have this bug and the first that landed after the fix for the
  other two: `store_api_key` used `chmod(0o600)`, which is the whole story on
  POSIX and nothing on NTFS, leaving the key readable by every other account
  on the machine. Now through `paths.restrict_to_owner` like the credentials
  file and the serve token, and it says so when the restriction cannot be
  applied. Like the credentials file, it is written by rename into a temp
  restricted while still empty, so a first key never sits under the
  permissions the home hands down.
- **The spawn-seam registry is no longer separator-dependent.** `core.spawn.SEAMS`
  is keyed by `<path>::<function>` with forward slashes; the guard built its
  keys with `str(Path)`, so on Windows every call site read as undecided AND
  every ruling read as stale, against a registry that was entirely correct.

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
