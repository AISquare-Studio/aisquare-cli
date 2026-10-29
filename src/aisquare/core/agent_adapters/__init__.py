r"""One registry for supported terminal agents; adding an adapter is registration.

The contract of an adapter is below: the checklist of what a new agent touches, the protocol it
implements (``types.AgentAdapter`` and the classes beside it), and what the conformance suite fails
on. ``tests/test_adapter_contract.py`` holds this text to ``types.py``: every public member of the
protocol's classes is named in an inline code span here.

## Adding a terminal agent

`src/aisquare/core/agent_adapters/` is the one place a terminal agent (Gemini CLI,
Grok, ...) is implemented. The shared services (hooks, board, launch, fleet, doctor,
Welcome, upgrade and uninstall) ask its adapter and never compare its name:
`tests/test_no_agent_literals.py` fails on an agent's id (`== "codex"`, `in (...,
"claude-code")`, a `match` case) anywhere outside that package, and holds a ratchet on the
rest of the agent-specific shapes there: a comparison with a constant naming an agent
(`AGENT`, `CODEX`, `DEFAULT_AGENT`, `DEFAULT_AGENT_BINARY` and any module constant set to
an agent's id or binary), a call of an `is_default_agent` (the binary-name predicate, gone:
a binary is the agent its name runs, `agent_adapters.adapter_for_binary`, and that agent's
adapter answers), and a string constant that is an agent's binary, one of its variables,
files or folders, or one of its flags. Those words are read off every registered adapter,
and the synthetic ones the suites register, never listed in the test: its `id`, `binary`
and Windows shims, its variables (`home_env`, whose first word is a prefix of its own, its
accounts' and capabilities', those its launch slots set), its files (`home_name`,
`settings_name`, `launch_inputs`, `instruction_files`, its accounts' `dir_name`), and what it
declares in `capabilities.vocabulary`, a `Vocabulary` of the rest: `env_prefixes` (its model
provider's, Claude Code's `ANTHROPIC`), `files` (Claude Code's `.claude.json`, Codex's
`auth.json`) and `flags`. So a new agent's words in a shared module fail with no edit to the
test. The first three are none outside the package. Its
`BASELINE` counts the rest per file; a new one fails with its
`file:line`, and a change that removes one lowers the count (aisquare's own options, prose
naming a variable and a provider's API key are not counted). Keep unsupported capabilities
explicit instead of copying another agent's flags or claiming hookless detection is
integration.

### The files

A new terminal agent's adapter touches these eight files, and no other: no shared module names
an agent, compares with an agent's id or constant, or asks a binary's name
(`tests/test_no_agent_literals.py`), and `tests/test_third_agent_seams.py` holds it with the
synthetic third agent shaped after Gemini CLI (its adapter is a module of `tests/`,
registered in tests alone),
whose launch, hooks, transcript tail and pages, usage and info line, uninstall and Welcome
run through the shared code: every file that names it is one of the eight, or of an optional
route below (the same test scans `src`, `tests`, `.github`, `plugins`, the
marketplaces and every file at the top of the repository).

| # | File | What |
|---|------|------|
| 1 | `src/aisquare/core/agent_adapters/<agent>.py` | A class implementing `AgentAdapter` (`types.py`), with its `AgentCapabilities`. |
| 2 | `src/aisquare/core/agent_adapters/__init__.py` | One line: `register(<Agent>Adapter())`. |
| 3 | `tests/fixtures/agent_events/<id>.json` | `events`: a recorded payload for every hook event it installs, with the `AgentEvent` fields it must decode to; `version_output`/`version`; `hooks_off` (the file and text that switch its hooks off); `lasting`: the `files` of a home that keeps aisquare's MCP server run through uvx (beside one that is not aisquare's) and the `[kind, name, fetches]` its `lasting_installs` must read; with account slots, `accounts`: the files a `login` leaves in a slot's home (in the config dir its accounts' `config_dir` names there), and a `relogin` (the same login after a new sign-in). |
| 4 | `tests/test_adapter_contract.py` | Its row in `EXPECTED_PERMISSIONS` (a coder's and a reviewer's flags), in `EXPECTED_HEADLESS` and `RECORDED_HEADLESS` (its headless call's session and hook switches, and an answer it gave), and in `EXPECTED_TOOLS_OFF` (what leaves its tools-only session no built-in tool). |
| 4a | `tests/fixtures/agent_transcripts/<id>.json` | `moments`: its transcript as it stood at a permission prompt, a question, an interruption and a turn ended on a question (`records`, or the first `lines` of a recorded session file beside it), with what `transcript_reader` must read of each; and `usage`, moments of its token records (`records` or a `rollout`), each with the `models` (requests, tokens by kind, whether priced, the cost), `tools`, `first_edit` and optionally `lookup_calls`, `injected_chars` and `thread_total` its `usage` must read; and `turn_end`, transcripts at a turn end (`records` or a `rollout`'s first `lines`, `turn_end` of them written at the turn end) with the `background` ids its `background_work` must read and whether its `activity_since` reads a turn `begun` since. |
| 4b | `tests/fixtures/agent_screens/<id>.json` | With a `screen` whose `dialog_on_screen` or `screen_prompt` is set only: `dialogs`, its screens at each dialog of its own (`rows`, as `capture-pane -p` gives them) with the `words` `dialog_on_screen` must read of each and, optionally, the `prompt` (`[shape, yes_key, no_key]`) `screen_prompt` must, and `not_dialogs`, screens with none (its empty prompt, a draft, a turn), each with `"ready": true` where `ready_on_screen` (or its default) reads it ready for a typed line; a `modal` on either, where given, is what `modal_on_screen` (or its default) must name on it (`null`: nothing). |
| 5 | `tests/test_<agent>_native.py`, `tests/run_<agent>_native.py` | The opt-in test against the real binary (`AISQUARE_TEST_<AGENT>=1`), as `test_codex_native.py` does for Codex; its CI job is an optional route, below. |

### Optional routes

An agent that takes one of these routes touches that route's files besides the eight, and
nothing else. The synthetic third agent takes none.

| Route | Files |
|-------|-------|
| Its live suite in CI | `.github/workflows/ci.yml`: one job that runs `python -m tests.run_<agent>_native` on a pinned version (Codex's `codex:` job). |
| A plugin (`plugin`, an `AgentPlugin`) | `plugins/<agent>/` (its manifest, its hooks and its copy of the hook launcher); its marketplace, `.agents/plugins/marketplace.json` or `.claude-plugin/marketplace.json`, the one its agent reads; its hook script's line in `.gitattributes`; its install check in `.github/workflows/install.yml`; its tests, `tests/test_<agent>_plugin.py` and `tests/test_<agent>_plugin_route.py`; and its README, in that folder (how it installs, what it carries, how it registers). |
| Installed with aisquare (`install.sh --agent <id>`) | `install.sh`, `install.ps1`, and their cell in `.github/workflows/install.yml`; for `--with chrome-devtools`, also its `mcp_registration` slot, whose commands and survey the shell copy in install.sh is held equal to by the installer's function tests. |
| Named in the prose | `README.md` (the agents it names) and `CHANGELOG.md` (its entry). |

Nothing else, and no new `aisquare hook` subcommand. Its launch is its slots' answers, each a
`LaunchPart` of arguments, environment and settings for its own settings layer
(`settings_layer`), so an agent whose seams are variables and settings files edits no
launch; its transcript is read through its `records`, a record a later line completes
included, on whichever page that line is; its usage is the windows its plan has (`UsageWindow`), whichever limits shown
beside its account; its permissions step `auto` down to `ask` without a reviewer of its own
(`approves_for_me`); its telemetry is its `telemetry_reader`'s; its screen its `screen`'s,
or none. Welcome offers its `install_routes`,
`agents connect <id>` is the doctor's one-click fix for it (`onboarding.KNOWN_FIXES`' one
agent row), `core.agents.install_hooks` writes its `hook_entry`, and upgrade and uninstall
find its homes (the ones it was connected in, its default, its `~/<home_name>*` siblings and
its account slots, each as its accounts' `config_dir`), take its hooks out and name what
its `lasting_installs` keep. The project paths its own start
reads and runs code from (its project settings, hooks, MCP servers: Claude Code's
`.claude` and `.mcp.json`, Codex's `.codex` and `.agents`) go in its
`capabilities.launch_inputs`, and every sandboxed agent's profile holds every registered
agent's read-only at the tops of its trees (`services.store_broker.launch_inputs`), so a
sandboxed Codex in the project cannot plant what the new agent's next start there runs.
The doctor's sandbox rows (`sandbox-install`, `sandbox-path`, `sandbox-hooks`) ask every
agent with `sandboxes_tools`, by its adapter. A `HookSpec` whose command is
`types.GENERIC_HOOK` installs `aisquare hook event --agent <id> --event <native event>`
(plus `--config-dir <home>` with `hook_names_home`), which decodes with the adapter.
Optional slots: numbered login slots (`accounts`, an `AgentAccounts`, below), the plugin route
(`plugin`, an `AgentPlugin`), Remote's reading of its session transcript
(`transcript_reader`, an `AgentTranscript`, below: without one its rows have no card a
transcript decides and an empty Transcript tab), Remote's reading of a dialog of its own that
fires no hook off its screen (`screen`, an `AgentScreen`, its `dialog_on_screen` with 4b), and native telemetry (`capabilities.native_telemetry` with
`telemetry_args` and `telemetry_reader`, a `TelemetryReader`: the receiver in
`services/native_telemetry.py` parses OTLP and asks the reader of the launch's agent what to
keep, so a second telemetry agent edits nothing there).

### The protocol

| Slot | What it answers |
|------|-----------------|
| `id`, `label`, `binary`, `install_hint` | The board's and config's name; what the UI says; the executable (`adapter_for_binary` knows a wrapper by this name). |
| `install_routes` | Every way to install it, best first (`types.InstallRoute`: how, the command, and the platforms it is offered on, every one when empty); at least one. Welcome's step 2 offers those for the platform, and on Windows the first, run inside WSL2 (`first_run.install_routes`). Claude Code: its installer, Homebrew on macOS, npm. Codex: the same three of its own. |
| `home_env`, `home_name`, `config_dir(home, env, explicit)` | The config dir the binary reads: explicit, else `$home_env` (through its accounts' `config_dir`, where the variable names the folder the config dir is in: Gemini CLI's `GEMINI_CLI_HOME`, its `.gemini` inside), else `~/home_name`. |
| `lasting_installs(home)` | What config dir `home` keeps of aisquare's in the agent's own files, which uninstall lists and never edits, each a `types.LastingInstall`: an MCP server that runs aisquare (`mcp`) or the aisquare plugin enabled (`plugin`), the file that records it, the agent's own command that removes it (aimed at that home, and for a project's entry run inside the project), and whether it starts aisquare once the package is gone (`fetches`: a uvx runner, a plugin), which refuses a purge until it is removed. `core.agent_adapters.lasting` reads whether an `{"command", "args"}` entry runs aisquare and fetches it. Claude Code: the `.claude.json` the dir uses (user and local scope) and the `.mcp.json` of each project it lists, `claude mcp remove --scope …`, and its plugin, `claude plugin uninstall`. Codex: `config.toml`'s `[mcp_servers.*]` and `[plugins."aisquare@…"]`, `codex mcp remove` and `codex plugin remove`. |
| `version_args`, `layout_version(binary)` | The arguments that print its version; `types.version_in` reads the output. And the version its install layout names, read off the filesystem with no process started (what `accounts` and the Accounts page show beside the binary; `None` for a layout that names none): Claude Code's native installer's `versions/<version>` target or its npm `package.json`, Codex's `releases/<version>-<target>` or its npm package; `types.package_version` reads an npm install's. |
| `settings_name`, `capabilities.hooks` | The hooks file in that dir, and each event installed with its subcommand, timeout (seconds) and matcher. `core.agents.install_hooks`/`remove_hooks` merge it, keeping every foreign entry. |
| `hook_entry(spec, command)` | The handler `install_hooks` writes for `spec`, running `command`, in the agent's own shape and its timeout in its own unit, with the `command` key the shared reader finds; an operator's longer `timeout` is kept. Claude Code and Codex: `types.command_entry`, `{"type": "command", "command": …, "timeout": <seconds>}`; Gemini CLI's would be milliseconds. |
| `hooks_off_setting`, `hooks_switch(home, settings, args)` | The setting that silences every hook, and whether the dir or the launch arguments set it. |
| `plugin` | The aisquare plugin route, an `AgentPlugin`, or `None`: `supported()` (its hooks can run on this platform), `installed(config_dir)` (the user-scope install there), `repo_installs(config_dir)` and `repo_install_here(config_dir, cwd)` (project- and local-scope installs), `command(verb, config_dir)` (the agent's own `plugin <verb>` line to paste) and `stands_down_per_event` (its launcher stands down only for the events the hooks file runs). |
| `mcp_registration` | Optional (`None`: its install registers no server): how the agent's own CLI registers an MCP server at user scope and where its config says one is, an `AgentMcpRegistration` (`core/agent_adapters/mcp_servers.py`): `add_args(name, command)` and `remove_args(name)` (the arguments after its binary: Claude Code's `mcp add NAME --scope user COMMAND…`, Codex's `mcp add NAME -- COMMAND…`), and `registered(home, name, package)`, read-only, true for a server of that name or one that runs the package under another. `install.sh --with chrome-devtools` registers it with every agent the run sets up, and spells these commands and this survey in shell, because it runs before aisquare exists; `tests/test_install_script_functions.py` holds the two to each other. A third agent takes the extra by declaring it. |
| `decode_event(payload)` | The payload as an `AgentEvent` of one kind: `session_start`, `prompt`, `stop`, `session_end`, `interrupt`, `attention`, `tool_done`, `turn_failed`, `notification`; `None` for one AISquare ignores. Its `model` and `effort` are what the session runs with, wherever the agent records them: Claude Code's payloads carry both; Codex's carry no effort and its `Stop` no model, so its `prompt` and `stop` take them from the rollout's newest `turn_context` (`codex.turn_settings`, measured on 0.162.1). |
| `encode_reply(kind, reply)` | What the hook prints: context for `session_start`/`prompt`, a "keep going" decision for `stop`, `""` for nothing. |
| `model_args`, `resolve_model`, `native_args`, `native_choice`, `validate`, `effort_alias` | Model and reasoning effort: the flags, the configured choice, the native spelling, what a native argv already chose. |
| `model_flags`, `claims_model(model)`, `effort_levels` | Every spelling of its model option (Codex's `-m` beside `--model`), by which `harness.model_arg` reads the model a spawn names whichever agent it is for; where none names one, each agent's own reader (`native_choice`: Codex's `-c model=…`) is asked, and the one model they find is the spawn's. Whether a model name is the agent's own, by its families as much as its price table (Claude Code's ladders' families, `harness.model_family`; Codex's `gpt-*`, `o<digit>*`, `codex-*` and the catalog 0.164.0 bundles, `codex.BUNDLED_MODELS`), which `agent_adapters.model_owner` asks every agent: the one agent that claims a model a launch names keeps the launch on it (`services.agent_launch`'s `model` rung), and a model one agent claims is never another's remembered pick (`services.model_memory`). And the efforts it takes, lowest first, by its own names: what the Spawn dialog's Effort offers while it follows the agent, and what its pick keeps (Claude Code's `harness.EFFORT_SCALE` and `ultracode`; Codex's `minimal` to `ultra`). |
| `merged_args(under, over)` | A role's (or its binding's) arguments `under` less each option the agent takes once that the spawn's own `over` name, by its own reading (`types.without_options_named`): the spawn's win, and none is passed twice (Codex 0.164.0 does not start on `--model` given twice; each `-c` key is merged too). Claude Code's are left as they are, its model choice replacing the role's `--model` and `--effort` (`services.fleet`). |
| `permission_args(intent, role, native)` | A `types.PermissionArgs(args, notes)`: the flags for a `PermissionIntent` (below), less what the `native` arguments already settle, with `store_access` for its write scope, and what the spawn's receipt says of them; `ValueError` for one it cannot express. An agent with no reviewer of its own (`capabilities.approves_for_me` False) runs `auto` approvals as `ask` (`types.stepped_down`), so the built-in `workspace:auto` is not refused for every role, and the receipt and the Settings page's launch line say "<agent> has no reviewer of its own: workspace:auto runs as workspace:ask"; Claude Code's `auto` mode and Codex's `--approve-for-me` are reviewers. `fleet.roles.<role>.permission` and `--permission-mode WRITE:APPROVALS` reach every agent through it (`services.fleet.permission_flags`). |
| `store_access(write, store)` | The launch arguments that hold the aisquare store in `store` (`types.store_dir()`) read-only in a session sandboxed at `write`, all of it (`types.store_read_only(store)`), made first by `types.prepare_store` (the sandbox and the store (`core.launch_seal`)). `[]` without `sandboxes_tools` and for a scope run unsandboxed. |
| `sandbox_writes(args, cwd, home)` | What the sandbox of a session launched with `args` in `cwd` may write (a `types.SandboxWrites`: its writable roots, the read-only paths within them, its workspace and temporary roots), read as the agent reads its settings: its config in `home`, the profile `args` select, then `args`, whatever chose the sandbox (`store_access`'s profile, a native flag or setting, the directories added). `None` only where the session runs unsandboxed. `services.store_broker.refuse_inside` refuses a launch whose sandbox may write an aisquare its hooks or broker run, or a directory inside one, from this answer alone; the conformance suite fails an agent with `sandboxes_tools` that answers `None` for a sandboxed scope. |
| `broker_access(args, store, inbox, *, protect=(), writable=())` | `args` with the launch's broker inbox the one writable path under `store` (and its `reports/`, handed in as one more `writable` path: where a sandboxed `asq exec` saves), `protect` (the launch inputs of every tree the launch touches, the other worktrees, the tmux sockets: `services.store_broker.launch_paths`) read-only and `writable` (the agent's own worktree, for a seat that writes its workspace) writable, the rest of `store` read-only whatever `args` recorded, applied by every launch (`services.store_broker.start_for_launch`); `None` where `args` run no sandbox of `store_access`'s. An adapter with `sandboxes_tools` declares one, and the conformance suite fails one that does not. |
| `fleet_args(role, label, mode, ...)` | A fleet spawn's flags from the role's native permission settings, then `label_args(label)`'s arguments. |
| `resume_args(native_id, transcript)` | The arguments that resume a session, put first: `fleet restart`, `fleet switch` and Restart resume the native id the first hook bound (Codex: `codex <options> resume <id> -- <continue line>`). |
| `fork_args(native_id, transcript)` | A `types.ForkArgs`: the arguments, put first as `resume_args` are, that start a NEW session carrying this one's conversation (a hand-off's Fork), and `mints_id`, whether the launch names the new session (Claude Code: `--resume <transcript> --fork-session`, beside the `--session-id` the fleet mints) or its first hook does (Codex: `fork <id>`, measured on 0.162.1: a new rollout and thread id, the source's turns carried). In the session's own config dir unless `resumes_across_homes`. `None` for an agent that cannot fork: the fork starts fresh with a hand-off prompt, and the receipt says so. |
| `adoptable(args)` | The session a process runs, as its command line (`ps`'s words) names it, when the process is the agent's: its native id, or the transcript's path where the agent takes one; `None` for another program's process and for one of the agent's that names none (a fresh start, a resume picked at run time). `fleet adopt` (#290) refuses a conversation such a process still holds (two processes would write one conversation). Claude Code: a word running `claude` and a session flag of its `session_flags` with its value; Codex: `codex [options] resume [options] <id>`, `exec resume` too, past `value_options`. |
| `exit_command` | What `fleet stop` types to end the session so its `SessionEnd` runs. |
| `dismiss_key` | The one tmux key that closes a dialog of the agent's own with its least answer and leaves it at its prompt (both agents: `Escape`; never `Enter`). The automatic usage-limit hand-over presses it once where the screen shows anything but the empty prompt, and types `exit_command` once that prompt is back, as Remote's Switch with `dismiss_dialog` does; a screen the key does not bring back is killed as before. |
| `queue_args(native_id, message)` | The arguments that queue a message for a RUNNING session's next turn (Codex: `queue --thread <id> --message <text>`), run with `home_env` set to the session's dir: what `fleet tell` does for a busy agent. `None` for none (a board note). |
| `key_spellings` | tmux key names the agent reads under another one, which the fleet UI's pane and Remote's key pad send instead (Codex: `S-Enter` → `C-j`, its newline). |
| `screen` | How its screen is read, an `AgentScreen` (below, after the capabilities): its empty prompt, a turn running, its dialogs and prompts, and the readings derived from them; `None` for an agent whose screen is not read (`types.NO_SCREEN`). |
| `role_defaults(role, binary, args)` | The flags a role needs on this agent wherever it starts (Claude Code's ui-tester `--chrome`), less what `args` says. |
| `first_prompts_since(home, cwd, since, pane)` | The prompts the agent's sessions under `home` that began in `cwd` at or after `since` ran, as typed, from its own record of them, whether or not its hooks ran, as a `PromptRecord(prompts, own)`; what settles a first message no board session has spoken for (`services.fleet`, #295's W1a for an agent whose `SessionStart` comes with its first prompt, Codex's: `agent_adapters/codex_prompts.py`). `None` for an agent whose hooks always say it (Claude Code's `SessionStart` fires at rest, before its first prompt) or that keeps no such record. It runs for every listing while the message is `pending` or `unsent`, so it reads a bounded part of what it reads and keeps what it found against the file's size and modification time. `pane` (`core.process_table.PaneProcesses`) is the row's pane, asked only when there is a prompt to attribute: a record that is named for no process (Codex's rollouts) is the row's when the processes below the pane's pid hold it (`pane.held_below(folder)`), and `own` is `True` for what it kept so; `own=False` is a record that could not tell (`held_below` gave `None`), whose prompts the fleet takes a row for only when they are the message's own words (#24), the row's detail saying so. An agent that cannot tell its sessions apart declares `own=False`. |
| `tool_policy(role, binary, args, fleet, effort)` | How a launch carries the role's deny set and skill listing (`harness.denied_for`): Claude Code's one `--disallowedTools` token and skill settings, every deny in `args` folded in; or none, with a note (`harness.withheld_policy`), for an agent whose tools its permission decides (Codex). |
| `browser_tools(home)` | The browser the agent brings itself, as the doctor's browser-tools line names it (Codex's `browser_use` feature, bundled browser plugin and the Chrome DevTools MCP server its `mcp_registration` finds in `config.toml`); `None` where the doctor finds it another way. |
| `mcp_args`, `label_args`, `disable_native_teams`, `fleet_settings(home, args)`, `settings_layer(settings, file, home, env, cwd)` | The launch slots, each a `types.LaunchPart(args, env, settings, notes)`: what the agent takes on its command line, in its environment, and in its settings layer of this launch, and what the launch prints of it. AISquare's MCP server for this session; the fleet label in its UI (Claude Code's `--name`); what keeps its own sub-agent teams off; what a fleet window's agent launches with to keep a dialog of its own out of a pane the fleet types into, less what the operator's config layers set, an administrator's managed one included, of which a note says so (Codex's `notice.hide_rate_limit_model_nudge=true` and `check_for_update_on_startup=false`, its layers read once, in `codex_config.layers`), asked only in a fleet window (`agent_launch.fleet_settings`). A launch composes them (`agent_launch.compose`): their arguments in order, their environment into the agent's, and their settings, with those a fleet spawn's own slots handed its window (`agent_launch.carry_settings`, the label's and the teams'), merged table by table (`types.merged_settings`) and given by `settings_layer` as the session's one settings layer ABOVE the operator's, where a value their own user or project settings set loses to it: Claude Code's `--settings <file>`, Codex's `-c` pairs, an agent whose seams are settings files a file of the launch's own under the cache that its variable names, of a layer over every settings file of the operator's (Gemini CLI's system settings, `GEMINI_CLI_SYSTEM_SETTINGS_PATH`, not its system defaults, which are under them; the agent writes `file`). The slot runs with the launch's home, environment and directory: such a file stands in for the machine's own of its kind (Gemini CLI's `/etc/gemini-cli/settings.json`, or the one the variable names), which the adapter reads by them and carries in it, an administrator's value kept over AISquare's with a note that says so (`LaunchPart.notes`, which the launch prints). `None` for an agent with no layer, whose slots answer no settings. |
| `instruction_files(home, cwd)` | The instruction files a session of config dir `home` started in `cwd` loads, in the order it loads them: the config dir's, then the project's along `cwd`'s chain, each chosen as the agent chooses among a folder's files on disk, absolute; what a session's start leaves out of the context it injects, a preference such a file holds already. With `cwd` `None`, the config dir's alone, every one it may read there in its precedence (`capabilities.first_context_file_only`: only the first not blank is read), which `agents connect` imports and the doctor shows. Claude Code: its config dir's `CLAUDE.md`, then each folder's `CLAUDE.md`, `CLAUDE.local.md` and `.claude/CLAUDE.md` from `cwd` up to the root (main's rule). Codex, measured on 0.164.0: the first of `$CODEX_HOME`'s `AGENTS.override.md` and `AGENTS.md` that is not blank, then from the git root down to `cwd` (or `cwd` alone outside a repository) each folder's `AGENTS.override.md` where one is there, an empty one too, else its `AGENTS.md`; nothing above the root, never a `CLAUDE.md`. |
| `unattended_env()` | Environment for a session nobody may be watching (the captain's, answered from a phone) that keeps out a prompt of the agent's own which would hold its pane and is no work's. Claude Code: `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1` (its session-rating survey). Codex: none; its dialogs in a running pane are kept out by `fleet_settings`. |
| `tools_only_args(server, command, args, env, *, inherit, home, cwd, launch_args)` | The arguments that leave the session no tool but those of the one MCP server `server` (run as `command` with `args` and `env`, keeping the session's own values of the variables `inherit` names), each allowed without asking, and no other MCP server loaded, the operator's own included: the captain's session, launched from config dir `home` in `cwd` with `launch_args`. `None` for an agent that cannot be confined so, which cannot be the captain; `ValueError` for a server its arguments cannot name. Claude Code: `--strict-mcp-config --mcp-config <the server> --tools "" --allowedTools mcp__<server>__*`, its servers inheriting the whole environment. Codex: `--disable` for each tool feature (`codex.TOOL_FEATURES`), `-c web_search="disabled"`, the server as `-c mcp_servers.<server>.*` with `env_vars` for `inherit` and `default_tools_approval_mode="approve"`, and `-c mcp_servers.<name>.enabled=false` for every other server the launch's config layers name (only those: on a name Codex does not have, it refuses its config), then `codex.TUI_TOOLS_SERVER` named with the server's command and switched off, which keeps the TUI's worktree tools out (0.162.1 and 0.164.0); measured on 0.162.1, the request then offers the server's tools, `request_user_input` (Plan mode only) and the MCP-resource readers. |
| `transcript_reader` | An `AgentTranscript` (`types.py`, the shapes and helpers in `agent_adapters/transcripts.py`), which Remote's needs-you scan and Transcript tab ask of the row's agent (`services.transcript.reader_of`), or `None`. Its `speaker` is how a page names the agent's turns (`* <speaker>`); `records(lines, ahead)` turns the `(offset, line)` pairs a backwards walk reads (`transcripts.lines_backwards`) into its records at those offsets, newest first, `None` for a line that is no record of its own, folding a record a later line completes into it (a tool call's result in a later `{"$patch": …}` line, Gemini CLI's), which the Transcript tab's pager reads (`services.transcript.read_page`); `ahead` is the lines after the walk's first, oldest first and read only as far as the reader asks (`transcripts.lines_forward`: the pager's are the newer pages'), where such a reader reads on for a later line of a record on this page, and nothing of it is yielded; `transcripts.one_per_line` is one whole record a line, Claude Code's and Codex's, which read nothing ahead; `tail_facts(file, size, budget, written)` reads the end of the transcript, backwards and within `budget` bytes (`transcripts.lines_backwards`), into a `TranscriptTail`: the calls still waiting for their result, the newest record's kind, the agent's last words, and `cut_at` for a walk that ran out inside the newest model response of a turn still running (Codex's: a rollout's `function_call` without its `function_call_output`, `turn_aborted`, `task_complete`, the response's `token_count` as its end); `page_lines(record, width)` renders one record as the page's lines (`transcripts.YOU`, `speaker_line`, `wrap`, `dim_note`, `question_lines`); `call_asks(tool)` says what a pending call waits on the human for (`question`, its questions in `input["questions"]` as `AskUserQuestion` holds them, `plan`, `subagent` or `tool`); `call_mark(tool)` what its prompt shows of it, to tell several pending calls apart on screen; `answer_keys(kind, options)` the quick answers of a `permission`, `question` or `plan` card on its dialog (every key one of `remote_needs.NEEDS_ANSWER_KEYS`); `reply_since(file, since)` the agent's words since the newest prompt of the human's, which the captain reads back (`""` an answer without words; `None` with no prompt, nothing of the agent's after it yet, or a prompt older than `since`). `usage(file, state)` is the session's token usage, a `types.UsageScan(delta, state, first_edit)`: what the transcript added since `state` (the last scan's own text, stored as it is; `None` from the start), its `delta` a `core.usage.Usage` (each model request's tokens once, by model as the agent's `price_table` names it and priced off it, the tool calls, the characters aisquare's hooks put in front of the model, every sign aisquare reached the session), its `first_edit` a `types.FirstEdit(edited, tokens, weighted, tool_calls)` (`as_dict()` as a usage row stores it); it reads only the complete lines appended since, a rewritten file from its start counting what is newer, and a second call with the state the first returned adds nothing; `None` for an agent whose transcript holds no token record, of which nothing is recorded. `ledger(file, since, until)` is the token ledger's counts of the transcript (`core.ledger.SessionLedger`: requests and their input split, the baseline, peak, mean and latest context, wakes and the no-action ones, the deltas and what they carried, compactions, tool calls), what `metrics ledger` prints for a session of that agent, counted by `core.ledger.Window` under one set of rules (#296's WS-0): Claude Code's is main's reader, moved (`claude_ledger.py`); Codex's reads its rollout's `token_usage_record` lines (fresh input is `input_tokens` less the cached and written), its calls by name, the deltas in its `developer` messages, a wake as the `user` message its Stop's continuation runs, and a `compacted` record as a compaction (`codex_ledger.py`). `context_tokens(file)` is the context now (the last request's input), which the quiet-point compaction weighs, and `compacted_since(file, since)` whether the transcript records a compaction since then (a `compact_boundary`, Codex's `compacted`), its acknowledgement where the compaction's `SessionStart` does not come at the compaction (Codex's comes with the next turn); both read the transcript's tail (`transcripts.latest_in_tail`, `ledger.recorded_since`), widened only while it does not reach what they ask for. `agent_adapters/usage_tally.py` keeps that state for a reader of one record a line (`Tally`: `read`, `request`, `tool`, `scan`). Claude Code's is main's `core.usage.scan`, moved (`claude_usage.py`); Codex's reads its rollout's `token_usage_record` lines, a response's model off its turn's `turn_context` (`codex_usage.py`). `conversations(home)` is each conversation the agent keeps under a config dir, its native id and transcript, by name (Claude Code's `projects/<folder>/<id>.jsonl`, Codex's `sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`), and `opening(file)` the folder it ran in and the first thing the human typed, from the first `transcripts.OPENING_LINES` lines (`transcripts.head_records`): what `fleet adopt` lists, looks a conversation up among and resumes it in (`services.adopt`, #290), on the account whose home holds it; `held_open(args)` says how the process run with `args` (any process's) holds the transcripts the agent writes open (`types.HoldsOpen`), a process being the agent's by what it runs (`types.process_executable`: its first word, or an npm shim's script under `node`) and never by a word in its arguments, each holding what it has open (measured with `lsof`), so a process of the agent's that names no session (`adoptable`) still holds the conversation it has open (`core.process_table`): `session`, for as long as its session lives, itself or as a `clients` process's client (Codex's TUI, with no subcommand or a prompt, `resume` and `fork`, `--remote <ADDR>` whatever else it is given, and its `agents` and `app-server proxy`, each measured connected to the daemon's socket; read up to the prompt and never into it, as `ps` loses quoting, a subcommand's name followed by what could be a prompt's words, past one of its own subcommands, reading as a prompt's); `alone`, the same but never a `clients` process's client, named only when it holds the conversation itself (Codex's that runs embedded, a `-c`, `--search` or `--profile` before the prompt and no `--remote`, an `exec` or a `review`, and one that runs no thread of the daemon's: its own `app-server`, `exec-server`, `login` and every other subcommand, `execpolicy`, `tcp-tunnel` and `cloud-tasks` too); `clients`, for the clients it serves and on after they quit (each process of Codex's managed app-server daemon, `codex app-server … --managed-daemon` and `codex app-server daemon pid-update-loop`, run from `<CODEX_HOME>/packages/app-server-daemon/`, where a `codex` started with no arguments runs its thread), which holds what it has open too (Codex resumes no thread its daemon has open), a client of it that runs named before it (a process of the agent's connected to its socket, `process_table.connected_to`, or where the socket cannot say a `session` one; never a fleet agent's) and it named only when it holds the conversation itself (its `pid-update-loop`, which holds nothing, never), and `server_note(run)` what that refusal says of it, `run` the argv that runs the agent on the conversation's account (`aisquare accounts run <slot>`), any command it names written with `shell_line.command_line` (Codex's: its background app-server, how long it keeps a thread after the last client quit, `codex app-server daemon stop` through `run`; `""` for an agent with none); `None`, none, or no process of the agent's (Claude Code's, whose session registry says who runs one). `background_work(file, since)` is the work the session's turns left running in the agent's own process, which an `/exit` would kill (ids, oldest first: Claude Code's background Bash, `Monitor`, background subagent or forked skill, main's `core.transcripts.background_running`; Codex's exec sessions still open, an `exec_command` answered `Process running with session ID <n>` with no `CommandExecution` completed for it since nor a `write_stdin` answered `Process exited`), leaving out what was started before `since` (the process that runs the agent now) and counting a shape it does not know until it ends; `activity_since(file, offset)` whether a turn began after byte `offset`, the transcript's length at a turn end, or is about to (Claude Code's: a prompt, a notice, a queued message taken or not yet taken, main's `core.transcripts.activity_since`; Codex's: a `task_started` or any `response_item`, never its `thread_settings_applied` or a background command's end at the idle prompt), a rewritten file counting as begun; both raise `OSError` for a file that cannot be read, and are what `[accounts] before_limit` reads before it moves an agent (`services.hooks`), which never moves one of an agent with no reader. `turns(file, state, day_of)` is `usage` cut into the transcript's turns, a `types.TurnsScan(turns, scan)`: each `types.TurnChunk(ended_at, usage)` the work between two of the human's prompts (not the agent's own context, a tool's result or a summary), cut too where `day_of(timestamp)` changes, a stretch with no request carried into the next, and `scan` the whole as `usage` reads the same bytes, its state the one `usage` takes; what the usage sweep records of a session no hook watched (`services.usage_stats`), which a Stop and a sweep read on from each other (Claude Code's main's `scan_turns`, moved; Codex's its rollout cut at each `user` message that is the human's, `agent_adapters.usage_tally.cut_turns` for a reader of one record a line); `None` where `usage` is. The sweep lists a home's sessions by `conversations` and counts each under the repository its `opening` names. Claude Code's reads its session JSONL (`claude_transcript.py`, its reply main's `core.transcripts.last_reply`), Codex's its rollout (`codex_transcript.py`, its reply the assistant `message` records after the newest `user` one that is not its own context). |
| `price_table` | A `types.PriceTable(as_of, source, prices, canonical)`, or `None`: the list price of each model its sessions run, USD per million tokens (`core.pricing.Price`: input, output, `cache_read` and `cache_write` as multiples of input, and a long prompt's rates over `long_prompt_over`), dated and sourced, keyed by `canonical`'s spelling of a model id (every key its own); `price_for(model)`, `cost_usd(model, tokens, prompt=)` and `weighted_tokens(model, tokens, prompt=)` price one request (`core.pricing.cost_at`, `weighted_at`). A model it lacks is unpriced, never priced like a neighbour, and so is a request that wrote to the cache at a row with no `cache_write` (`None`: the source lists none). The figure is the API's list price whatever the login pays with. Claude Code: Anthropic's table, `core.pricing.PRICES` by reference. Codex: OpenAI's Standard tier, read 2026-10-24 off developers.openai.com/api/docs/pricing (`codex.PRICES`: the gpt-6 and gpt-5.6 rows with their 30-minute cache write, 1.25x input; gpt-5.5, gpt-5.4 and gpt-5.3-codex with none; the long rates over 272K input tokens). `None`: every model of the agent is unpriced and reported apart. |
| `accounts` | Numbered login slots, an `AgentAccounts` (`types.py`), or `None`: the home's variables (`home_env`, `extra_env` each set to the slot's scratch dir, `plain_vars` where a launch keeps the shell's own) and the slots' directory name (`dir_name`), the plain slot's number and name (`plain_slot`, `plain_label`; the number is negative for a new agent, as Claude Code's is 1, Codex's 0 and the managed ladder runs from 2, and `--account -2` names it as `--account 3` names slot 3) and home (`plain_home(home, plain)`), `install_command`, `noun` (what prose calls one of its accounts: a `Claude` account, a `Codex` one), `network_usage` (its `usage` leaves the machine; a local reading is as fresh as the slot's last session, and a listing says how old it is), `legacy_slots` (Claude Code's alone: its slots predate every other agent's, so their arrangement is the store's own `claude_account` table and project key, its plain slot is listed before its home exists, and the listings, `accounts --json` and the doctor's `claude-*` rows name them as they always did; `False` for a new agent, whose slots are keyed by its id), `sign_out` (how a plain home is signed out, as a refusal to remove it says), `shares_settings` (`[accounts] share_settings` links a managed slot's settings to the plain home's: Claude Code's `settings.json`), `share_parts` (what its accounts can share, `[accounts] share` and `accounts share`: a tuple of `SharePart(name, entry, is_dir, how, experimental)`, `name` what the operator writes, `entry` its place in the agent's config dir (`config_dir(slot_dir)`) and `is_dir` whether it is a folder (made empty in the plain home when it has none) or a file (refused when the plain home has none), `how` `"link"` (each managed slot's entry becomes a link to the plain home's, its own kept beside it) or `"merge"` (the slots' own copies merged into one place the first time it is turned on, and `memory` hands it to every launch); `()` shares nothing; Claude Code's are memory, skills, agents, commands, `claude_md` and plugins, Codex's `agents_md` and `skills`, a new agent's whatever its config dir holds, linked by the code every agent's use) and `memory` (a `SharedMemory` for an agent with a `merge` part, else `None`: `root_name` the folder under the aisquare home that keeps the shared copies, `override` the operator's own way of setting it that wins, in the stand-down line's words, `key(cwd)` what the agent files a folder's memory under, `projects(account)` the memory directories a slot holds by key, `apply(args, operator_args, directory)` the launch's arguments with the agent's memory pointed at `directory` (merged into what they already set) or `None` when the operator's line sets it, and `warnings(account)` what is wrong with a slot's memory, for the doctor), `launched_account(env, accounts)` (the slot a launch with `env` runs on: its home, `core.claude_accounts.account_in_home`; Claude Code's by the `.claude.json` it reads), `sign_in_args` (the agent's own sign-in command run in a slot, Codex's `login`; `()` for an agent that signs in from its own TUI, Claude Code's and Gemini CLI's: a sign-in window starts the binary in the slot with nothing after it, the human signs in there and leaves it with its `exit_command`, and the window waits for `login_fingerprint` to change), `identity`, `signed_in(account, fresh=)`, `subscription`, `token_state` (`ok`, `expired`, `missing`, or `unreadable` when the store its login is kept in would not say), `usage` (a `ClaudeUsage` of the windows its plan has, shortest first, each a `models.UsageWindow(name, minutes, used_percent, resets_at)`: Claude Code's five-hour `session` and seven-day `week`, Codex's primary and secondary by length, an agent of daily per-model quotas one a model; `session_percent` and `week_percent` are views of the two so named; the Accounts page and `accounts usage` show those two in their columns, and a reading with a window of another name every window by its name under the row (`ClaudeUsage.own_windows`); the headroom pick, the sidebar's `#slot·%`, the info line, the account picker, those listings (`limits`) and the JSON (`limiting`) go by the window that limits, `ClaudeUsage.limiting`, the pick and the sidebar skipping a reading whose shortest window went unread), `limit_from_transcript`, `failed_turn`; `login_fingerprint(account)`, a digest of the login stored in a slot (never a token), which a *Log in* over a login still on disk waits to see change, and which keys an agent of `network_usage`'s readings in the five-minute usage cache (each sample keeps every window, and `doctor --live` has a headroom row for it), `None` with none stored, raising `LoginUnreadable` when the store would not say; and `trust`, a `FolderTrust`, or `None` for an agent that asks nothing per folder: `file_name` (the file in a home that holds it), `scope(base)` (the folder and the key a yes there records, from `core.claude_sharing.trust_scope`, in the agent's own rules: its `covering` entries in the order it reads them), `read(account)` (`{key: trusted}`; `False` is a no that no share overrides; `SharingError`, or `NotStartedYet` with nothing to add to yet) and `grant(account, *keys)` (a yes for each, nothing else in the file changed). `types.trusted_by(records, scope)` is the entry that decides. `share_for_launch`, `accounts trust` and `--sync` copy trust only between accounts of one agent. And `config_dir(slot_dir)`: the config dir the agent reads in a home its `home_env` names, where a slot's sign-in connects aisquare (`core.claude_accounts.agent_config_dir`) and the uninstall plan finds its hooks: the slot itself for Claude Code and Codex, `<slot>/.gemini` for Gemini CLI. And `transcript_roots(config_dir)`: the folders under a config dir that hold its sessions' transcripts, every one its reader's `conversations` lists there among them (Claude Code's `projects`, Codex's `sessions`; `()` for none): how a transcript's path names the slot it ran on, which the usage sweep and a Stop's first record ask (`services.usage_stats`). |
| `telemetry_args(endpoint, keep_out=)`, `telemetry_reader` | Its OpenTelemetry exporter, with `native_telemetry`, and how the per-launch receiver (started with `--agent <id>`) reads it, a `types.TelemetryReader`: `operator_configured(home, args, cwd)` (an exporter of the operator's own in a config layer of the launch, which is kept and AISquare's tracing is off), `tool_shell_excludes(home, args, cwd)` (the patterns its config keeps out of its tools' shells, which the receiver's header joins; `None` where an override of the operator's own would hand it to every tool), `tool_shell_policy` (how a note names that setting), and `decode(attributes, event)` (what of one log record is kept: model, provider, tokens, timings, `event.name`; never a prompt or a header). Codex's is `codex_config.CodexTelemetry`, its config layers read there. The receiver's `Authorization` is never an argument: it reaches the agent in `OTEL_EXPORTER_OTLP_LOGS_HEADERS`, and `keep_out` (the variable, with the operator's own patterns) is what the agent keeps out of its tools' shells (Codex: `shell_environment_policy.exclude`). |
| `system_prompt_args(block, text, args, home, *, cwd)` | The launch arguments that add a persona's briefing (`text`, which the file `block` holds) to the agent's system prompt beside its own, for a launch with `args` from config dir `home` in `cwd`; `None` where `args` already set that seam or the agent has none, and the session-start hook alone briefs the persona. Claude Code: `--append-system-prompt-file <block>`. Codex: `-c developer_instructions=…`, the operator's own value from its config layers first, then a blank line and `text`, as the `-c` replaces theirs (measured on 0.162.1). |
| `skill_dirs(home, repo)` | Where a session of config dir `home` reads skills (`<name>/SKILL.md` folders), as a `types.SkillDirs`: `user` dirs, and `project` dirs in `repo` (`()` outside one), absolute and in the agent's order. The first of each is where `persona export --skill` writes, and `persona import --list` reads them all. Claude Code: `<home>/skills` and `<repo>/.claude/skills`. Codex: `~/.agents/skills` (read under every Codex home, so one export reaches every account) and `$CODEX_HOME/skills`, then `<repo>/.agents/skills` and `<repo>/.codex/skills` (measured on 0.162.1). |
| `headless_call(schema, out, model, *, home, cwd)`, `headless_answer(stdout, out)` | One headless turn whose answer is JSON the schema in the file `schema` describes (`persona import --engine manager`), the instructions and source on stdin, run in the manager account's environment from config dir `home` in `cwd`: it keeps no session, runs no tool and no MCP server (the source is untrusted; `ValueError` where they cannot be switched off), its hooks are off where an argument can say so, and `model` is pinned when given. `headless_answer` reads the answer from its stdout or from `out`, `None` when there is none. `None` from `headless_call`: no such call, and the engine ladder goes on. Claude Code: `-p --tools "" --max-turns 1 --output-format json --json-schema <text> --no-session-persistence --settings {} --strict-mcp-config`, its envelope's `structured_output`. Codex: `exec --ephemeral --skip-git-repo-check --sandbox read-only -c features.hooks=false` with `tools_only_args`' tool switches and its layers' MCP servers off, then `--output-schema <schema> -o <out> [-m model] -`, the JSON in `out` (measured on 0.162.1: a manager slot's trusted hooks fire under `exec` without `features.hooks=false`). |

`AgentCapabilities` holds the rest as flags, each documented in `types.py` with the
service that reads it: `pid_env` (the variable that hands a hook the agent's own pid,
Claude Code's `CLAUDE_PID`) or `tool_shell_env` (the variable the agent sets in its
tools' shells and never in its own hooks, Codex's `CODEX_THREAD_ID`, with `tool_shell_vars` the rest it sets there, stripped from every tmux client too) — how a fleet row
tells its agent's own process from a nested child, across a `/clear` or a `/resume` —
`assigns_session_id` (AISquare names the session at launch) with its `session_flags`, a
`SessionFlags` in the agent's own spelling (Claude Code's: `names` is `--session-id`, which
takes the id a fresh start or a fork is handed, `minted(id)` those two arguments; `resumes`
`--resume`/`-r`, `continues` `--continue`/`-c` and `forks` `--fork-session`, which the
session planner reads as a choice the caller made and a fleet row's recorded arguments go
without), `launch_inputs` (above),
`requires_hook_trust`, `hook_names_home`, `strict_hooks_file`, `stop_reason_as_prompt`,
`limits_in_transcript` (no turn-failed event: the accounts slot's `limit_from_transcript` and `failed_turn` read it), `ambient_first_run`,
`reads_pane_readiness` (`fleet tell` may type into a pane that looks idle),
`one_press_exits` (the keys, as the agent is sent them past `key_spellings`, that end it with
one press at its empty prompt (its screen's `at_empty_prompt`): Codex's `C-c` and `C-d`; Remote's `send-keys` refuses one
unconfirmed, 409 `one_press_exit`, while the screen shows that prompt or cannot say, as it
refuses every agent a second within 3 s; empty for an agent that asks for a second press,
Claude Code's; each must be a key the pad reaches the agent as),
`starts_at_its_prompt` (its `SessionStart` fires once it is at rest at its prompt,
Claude Code's), `resumes_across_homes`, `resume_keeps_system_prompt` (a
resumed session keeps the system prompt `system_prompt_args` gave its first launch and
ignores a new one, Codex's: a restart's receipt says a persona changed since reaches it
through the briefing alone), `exports_home_env`, `own_doctor_check` (the doctor has a dedicated check for it, Claude Code's, which Welcome's step 2 probes it with too; the per-adapter row and the generic probe skip it), `model_ladders`,
`model_proxy` (its traffic goes through the explainability proxy, the lane the doctor and
Remote's card judge it by; with `native_telemetry` instead, the card judges its own
telemetry's lane, and with neither it reads untraced), `usage_shared` (its usage is in the
opt-in anonymous usage share, whose consent names Claude Code's alone; an agent without is
counted locally and left out of every shared batch), `remote_control` (it can be started so
that another client continues the session, titled: Claude Code's `--remote-control`, #289; a
launch that asks of an agent without says so and starts it as it was), `positional_prompt` (its first
message on its command line, after `--`), `compact` (a `CompactCommand(line, instructions)`:
the command that compacts the session at its idle prompt, which the quiet-point compaction types, bare
where text after it would run as a prompt; its acknowledgement is the compaction's `SessionStart`
where that comes at rest, else the transcript's record of it),
`first_context_file_only`, `approves_for_me` (a reviewer of its own decides `auto` approvals; without one `auto` runs as `ask`), `sandbox_permissions`,
`sandboxes_tools` (its tools run in an OS sandbox, which `store_access` must open to
the store), `redraws_while_asking` (its UI redraws while it waits on the human, so an
attention row holds until a hook moves it or its screen's `working_on_screen` reads a turn running),
`hookless_through` (the last aisquare release that writes
none of its hooks, Codex's 0.8.2: an upgrade or move back to it leaves them and says so),
`legacy_fleet_args`, `value_options` (its options that take a value; one that is also a short
option of `aisquare launch`'s, Codex's `-a`, is named in launch's refusal of a value that is
none of launch's), `switch_options`.

Its screen is read through one optional reader, `screen`, an `AgentScreen` (`types.py`): nine parts, each optional, and three readings derived from them where the agent declares nothing more of its own, `AgentScreen.ready` (a typed line lands as a prompt: `ready_on_screen`, else its empty prompt with no turn running on it), `AgentScreen.drawn` (its prompt drawn, busy or not: `prompt_on_screen`, else its empty prompt) and `AgentScreen.modal` (what a line must not go into: `modal_on_screen`, else `dialog_on_screen`'s words, else `screen_prompt`'s question). An adapter with no screen (`screen = None`) reads as `types.NO_SCREEN`, never at its prompt and never ready (`types.screen_of`): the third agent of the suite runs so. Remote reads its empty prompt by `idle_on_screen` and `idle_prompt_mark` alone (`AgentScreen.reads_empty_prompt`, `at_empty_prompt`), never by `ready_on_screen`. The parts: `idle_prompt_mark` (what the last line of the screen starts with at an empty prompt,
Codex's `? for shortcuts`: where a pane its hooks say nothing fresh about is typed into,
and what a pane whose hooks say waiting, or one `fleet stop` would `/exit`, must show too),
`idle_on_screen` (the agent's own reading of that empty prompt, for a layout without the
mark: Codex under `--no-alt-screen`, its empty composer with only its footer below; Remote's
agent actions read the agent's screen by it, a quiet pane at that prompt at rest and anything
drawn over it a dialog they refuse),
`working_on_screen` (whether its screen shows a turn running unblocked: Codex's status
line, right above its composer),
`dialog_on_screen` (what a dialog of its own on its screen asks, the words above its
options, or `None` for a screen with none, a draft and a turn included: Remote's needs-you
scan reads it for a row with no call pending and no bell, and a dialog it reads is a
`permission` card with no quick answers, pushed 30 s after the scan first saw it; Codex's
folder trust and hook review before its first prompt, and its *Switch to gpt-6-luna?* and
*Implement this plan?* after a turn's `Stop`, fire no hook. An agent whose every dialog reaches the board through a hook
leaves it `None`, and its screen is not read for one),
`start_dialog_mark` (what its screen shows at a dialog before its start,
where an Enter answers it: Claude Code's `Enter to confirm`, which keeps a prompt out
and stops the wait for its start),
`screen_prompt` (the prompt at the bottom of its screen and the keys that answer it, a
`types.ScreenPrompt`: `chooser`, its yes the digit of its first option that says Yes and its
no; `yn`; `trust`, the folder-trust question, and `dialog`, any other of its own, both the
owner's with no keys; `None` for none. The captain asks it of the pane's own agent before
`press` sends a key or a paste types a line: Claude Code's input box, chooser and trust
dialog, `claude_screen.prompt_showing`; Codex's dialogs as `dialog_on_screen` reads them,
its approval view a chooser on `1` and `Escape`),
`ready_on_screen` (whether its screen shows its prompt drawn with no turn running at it and
nothing over it, so a typed line lands as a prompt, a draft allowed: the captain's word over
a `working` read for its `say`, `press` and `paste`, `AgentScreen.ready`. Claude Code's
input box drawn and idle, `claude_screen.box_idle`; `None` takes its empty prompt,
`idle_on_screen` else `idle_prompt_mark`, with no turn on it by `working_on_screen`, which is
Codex's. Remote never reads it),
`prompt_on_screen` (whether its screen shows its prompt drawn, a turn running at it or not:
all a line to a captain the fleet reads `waiting` needs, `AgentScreen.drawn`, since the rows
above that prompt are the captain's own last reply, and one quoting a busy footer must not
hold its next line out. Claude Code's input box drawn, `claude_screen.box_drawn`; `None` takes
its empty prompt, `idle_on_screen` else `idle_prompt_mark`, which is Codex's),
`modal_on_screen` (what its screen shows that a typed line must not go into, in the owner's
words, or `None`: what the captain's `say` refuses by name, `AgentScreen.modal`. Claude
Code's `claude_screen.modal_showing`, its chooser and trust dialog, the rating survey over its
box and any other dialog with an Enter or Esc footer; `None` takes `dialog_on_screen`'s
words, which is Codex's, else `screen_prompt`'s question).

A `PermissionIntent` is a write scope (`read_only`, `workspace`, `full`) and an
approvals policy (`ask`, `auto`, `never`), written `workspace:auto`. A reviewer's seats
are `read_only:auto`, every other role `workspace:auto`; an explicit native flag still
wins.

| Intent | Claude Code | Codex |
|--------|-------------|-------|
| `read_only:ask` | `--permission-mode manual --disallowedTools=Edit Write NotebookEdit` | `--ask-for-approval on-request -c approvals_reviewer="user"`, store profile over `:read-only` |
| `read_only:auto` (reviewer) | `--permission-mode auto --disallowedTools=Edit Write NotebookEdit` | `--ask-for-approval on-request -c approvals_reviewer="auto_review"`, store profile over `:read-only` |
| `read_only:never` | `--permission-mode dontAsk --disallowedTools=Edit Write NotebookEdit` | `--ask-for-approval never`, store profile over `:read-only` |
| `workspace:ask` | `--permission-mode manual` | `--ask-for-approval on-request -c approvals_reviewer="user"`, store profile over `:workspace` |
| `workspace:auto` (default) | `--permission-mode auto` | `--approve-for-me`, store profile over `:workspace` |
| `workspace:never` | `--permission-mode dontAsk` | `--ask-for-approval never`, store profile over `:workspace` |
| `full:ask` | `--permission-mode manual` | `--ask-for-approval on-request -c approvals_reviewer="user" --sandbox danger-full-access` |
| `full:auto` | `--permission-mode auto` | refused: `--approve-for-me` runs only in workspace-write |
| `full:never` | `--permission-mode bypassPermissions` | `--ask-for-approval never --sandbox danger-full-access` |

The store profile is the one of the sandbox and the store (`core.launch_seal`).

### What the conformance suite fails on

`tests/test_adapter_contract.py` runs every case for every registered adapter and for
two synthetic ones registered only there: `tests/fake_agent.py` and the third agent
shaped after Gemini CLI, a module of `tests/` beside it. It fails when:

- the fixture file or the `EXPECTED_PERMISSIONS` row is missing;
- it names no `install_routes`, or Welcome would offer none on Linux, macOS or Windows;
- its `lasting_installs` find something in a home that keeps nothing, misread the
  fixture's `lasting` home, name an entry in another home or of another agent, or a
  removal that its own binary does not run;
- an installed event has no recorded payload, or decodes to another kind or other fields;
- the adapters together no longer decode every event kind;
- installing twice doubles a hook, removing leaves the file other than it was before,
  a foreign entry is lost, the installed command is not `hook_args`'s, or a handler is not
  its `hook_entry`;
- the fixture's hooks-off switch does not make a complete install read as not connected;
- the installed command, run as `aisquare hook ...` with the recorded payloads, does not
  put the session on the board under this agent and then mark it `waiting`;
- a reply prints something for nothing, drops the context, or makes a Stop reply the
  bare reason rather than a decision;
- `version_in` misreads its version line, or `config_dir` ignores `home_env` or an
  explicit dir;
- an agent that sandboxes its tools (`sandboxes_tools`) has a write scope whose
  `store_access` does not name the store (a Windows home included, as the agent
  spells it); a permission the fleet maps for it, a role's built-in one included,
  does not carry its `store_access`; or an agent without a sandbox declares any;
- an agent that sandboxes its tools declares no broker (`broker_access` answers
  `None` over a sandboxed permission, or its answer does not name the inbox and the
  store), or one without a sandbox declares one;
- an agent that sandboxes its tools answers `sandbox_writes` with `None` for a sandboxed
  scope, or its answer lets the store be written, or writes the workspace under a
  read-only scope or not under a workspace one; or one without a sandbox answers it;
- an agent declares no `launch_inputs`, a name there is not one path component, or the
  profile's protected set (`store_broker.launch_inputs`) leaves one out;
- an agent with account slots does not connect aisquare, at a slot's sign-in, in its
  accounts' `config_dir` of the slot, or `managed_slot` does not map that dir back to the
  slot;
- an agent with account slots has no `accounts` fixture; its `login_fingerprint` is
  `None` for a login, differs between two reads of one login, holds what the login file
  holds, or stays the same after a `relogin` (`claude_accounts.sign_in_landed` must
  answer the new login and not the old); or its `trust` does not carry a yes from one
  account to another at a launch (`share_for_launch`, with the receipt's note), in its
  own `file_name`, the login there kept;
- an intent maps to neither flags nor a refusal with a reason; resume arguments are
  empty; the exit command is not a slash command; the MCP arguments do not name the
  server; nothing keeps native teams off; telemetry arguments come without the
  capability;
- its `system_prompt_args` names neither the block nor its text, or still answers when the
  launch arguments already carry its own answer (the operator's seam wins);
- it keeps a system prompt across a resume (`resume_keeps_system_prompt`) with no seam to
  set one;
- a dir of its `skill_dirs` is relative or named twice, a project dir lies outside the
  repository or a user dir inside it, or outside a repository it names project dirs;
- its `headless_call` holds no schema, lacks the words that keep its session and hooks off
  (`EXPECTED_HEADLESS`), or does not pin a model given it; or its `headless_answer` misreads
  the answer it recorded (`RECORDED_HEADLESS`), or reads one where there is none;
- its `fork_args` are its `resume_args`, name neither the session nor its transcript, or
  mint an id for an agent that does not name its sessions at launch;
- its `tools_only_args` does not name the server, its command, arguments and environment,
  leaves out a switch that turns its built-in tools off (`EXPECTED_TOOLS_OFF`), turns one
  on, or is no more than its `mcp_args`; loads a server the operator keeps where the agent
  reads servers (`EXPECTED_OPERATOR_OFF`), switches its own off, or keeps from its server
  a variable `inherit` names;
- its `unattended_env` names a variable that is not one, or one that says which home,
  process or launch a session is, or one that keeping its native teams off already sets;
- the agent has no `transcript_reader`, or it misreads one of its four recorded moments
  (`tests/fixtures/agent_transcripts/<id>.json`: the pending calls and what each waits
  for, the newest record, the last words, a walk cut short on a small file); the shared
  needs scan, given its facts, makes no permission card on its `answer_keys` or no
  question card on its keys for each option, or a key is one a phone may not send; or a
  page of its transcript does not name the human (`> you`) and the agent
  (`* <speaker>`);
- its `background_work` or `activity_since` misreads one of its recorded turn ends (the
  fixture's `turn_end`: what was left running, whether a turn began since), counts work
  started before `since`, reads a rewritten file as no turn, or answers for a file that
  cannot be read;
- a hand-off brief made from its recorded transcript (`services.handoff`, task #15) is not
  that transcript as its own reader pages it, names neither agent, holds a terminal escape
  or runs past `PROMPT_MAX`; or the synthetic agent's `fleet switch --agent` onto Claude
  Code, or Claude Code's and Codex's onto it, does not start the target fresh with the
  brief as the target's first message (on its command line through the prompt file where it has `positional_prompt`, else typed), its new
  launch record naming the target;
- its `reply_since` misses the last words of its recorded turn that ended on a question,
  answers when that turn's prompt is older than `since`, finds words in a turn interrupted
  before any, or answers for a transcript that is not there;
- a key in its `one_press_exits` is not one a pad key reaches the agent as (past its
  `key_spellings`), or Remote's `send-keys`, with nothing to say the agent is not at its
  empty prompt, sends one of them unasked, refuses a `C-c` or `C-d` it does not declare,
  or refuses without naming the agent (409 `one_press_exit`, its `reason`);
- an agent with `dialog_on_screen` brings no `tests/fixtures/agent_screens/<id>.json`, or
  one without it brings one; it misreads the words of one of its dialogs, or reads a
  dialog on a screen that shows none; or the shared needs scan, given a fresh row (no
  session, no call pending) and one of its dialogs on screen, makes no `permission` card,
  one whose reason names an agent, one with quick answers or one pushed sooner than 30 s,
  or a card for a screen with none. Of an agent without it, the scan reads no screen.
- an agent with `screen_prompt` brings no screens, or reads no prompt on one of its
  dialogs, keys on a `trust` or `dialog` one, a `chooser` or `yn` one without its No, a key
  a quick answer may not send (`remote_needs.NEEDS_ANSWER_KEYS`), another shape or keys than
  a dialog's `prompt` names, or a prompt on a screen with none.
- its screens read ready (`types.screen_ready`) at a dialog of its own, or otherwise than
  their `ready` says; or the default, for an agent with no `ready_on_screen`, reads ready
  where its empty prompt does not show or a turn runs on it.
- its screens name nothing a line must not go into (`types.screen_modal`) at a dialog of
  its own, or otherwise than their `modal` says; or the default, for an agent with no
  `modal_on_screen`, names a dialog by other words than `dialog_on_screen`'s, else
  `screen_prompt`'s question.

The fake adapter was written from this section alone. Besides the cases above it is
spawned through `services.fleet.spawn` with a stub binary, its window's `aisquare
launch` builds its argv, and `fleet stop` types its `/quit`, with no change to any
shared service. Its transcript is a log of its own (`heard`, `said`, `run`, `pick`,
`ran`, `halt`, `over` records), and its dialogs take `y`/`n` and arrows, never digits. A
dialog of its own asks on a `?? ` row and ends its screen on `(y/n)`, which its
`dialog_on_screen` reads, and its `screen_prompt` reads as a yes/no on `y` and `n`.

The third agent's adapter has Gemini CLI's shapes as its docs give them (nothing of it is
installed or measured): `GEMINI_CLI_HOME` names the folder its config dir `.gemini` is in,
its hook timeouts are milliseconds, every hook reply is JSON, and a call's result reaches
its log in a later `$patch` line. Both, through `tests/test_third_agent_seams.py`, are
connected and disconnected in their own handler shape, named by the uninstall plan with
their own removals (a purge their uvx server would undo refused, their account homes
counted), offered by Welcome with their own installers, and found, connected and read by
first run, with no edit to a shared file.
"""  # noqa: E501

from aisquare.core.agent_adapters.claude_code import ClaudeCodeAdapter
from aisquare.core.agent_adapters.codex import CodexAdapter
from aisquare.core.agent_adapters.types import AgentAdapter, executable_name

_ADAPTERS: dict[str, AgentAdapter] = {}

#: The agent a launch runs when nothing chooses one, and the one a row or a board
#: session from before adapters (no ``agent`` recorded) ran.
DEFAULT_AGENT = "claude-code"


def register(adapter: AgentAdapter) -> None:
    if adapter.id in _ADAPTERS:
        raise ValueError(f"agent adapter already registered: {adapter.id}")
    _ADAPTERS[adapter.id] = adapter


def unregister(name: str) -> None:
    """Take an adapter out again (a test's synthetic one)."""
    _ADAPTERS.pop(name, None)


def adapters() -> tuple[AgentAdapter, ...]:
    return tuple(_ADAPTERS.values())


def get_adapter(name: str) -> AgentAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"unknown terminal agent {name!r}; choose {', '.join(_ADAPTERS)}"
        ) from None


def default_adapter() -> AgentAdapter:
    """:data:`DEFAULT_AGENT`'s adapter: what a launch runs when nothing chooses one, and what
    an unnamed executable (a ``claude2`` binding) and a record from before adapters ran."""
    return get_adapter(DEFAULT_AGENT)


def default_binary() -> str:
    """The executable a launch runs when nothing names one: :data:`DEFAULT_AGENT`'s own."""
    return default_adapter().binary


def recorded_adapter(name: str | None) -> AgentAdapter | None:
    """The adapter a recorded ``agent`` names: :data:`DEFAULT_AGENT`'s for ``None`` (a
    record from before adapters), ``None`` for one this install does not know."""
    return _ADAPTERS.get(name or DEFAULT_AGENT)


def adapter_for_binary(binary: str) -> AgentAdapter | None:
    """The adapter whose ``binary`` is ``binary``'s name (``types.executable_name``: the
    basename, Windows shims included and a trailing separator tolerated), or ``None``: what
    a launch's own flags for an agent go by (a role's ``default_args``, ``--session-id``).

    **A wrapper script literally named ``claude`` is Claude Code**, deliberately:
    ``~/bin/claude`` and ``/opt/wrap/claude`` are the shape operators actually use to add an
    account or a flag to it, a wrapper is indistinguishable from the real thing from here,
    and the cost of being wrong is one unknown flag on a program the operator named after
    it. Anything else (``claude2``, ``claude-next``, ``aider``) is no agent's by name."""
    name = executable_name(binary)
    return next((adapter for adapter in adapters() if name == adapter.binary), None)


def model_owner(model: str | None) -> str | None:
    """The agent whose own model ``model`` is: the one registered agent that claims it
    (``AgentAdapter.claims_model``: Claude Code's ``opus``, ``claude-opus-5-5``; Codex's
    ``gpt-x``, ``o3``, ``codex-mini-latest``). ``None`` for no model, one no agent claims,
    or one two agents claim."""
    if not model or not model.strip():
        return None
    owners = [adapter.id for adapter in adapters() if adapter.claims_model(model)]
    return owners[0] if len(owners) == 1 else None


register(ClaudeCodeAdapter())
register(CodexAdapter())
