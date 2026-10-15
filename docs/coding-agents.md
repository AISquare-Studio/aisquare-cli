# Terminal coding agents

AISquare supports Claude Code and Codex through a coding-agent registry. Memory,
the task board, claims, manager continuation, CI delivery, tmux, worktrees and the
terminal UI are shared. An adapter supplies native hooks, config locations,
model options, permission options and MCP configuration.

Codex compatibility is tested against **CLI 0.162.1**. Use this version or a
newer compatible release; older releases may lack the required hooks and flags
(`--approve-for-me`). What each Claude Code feature does on Codex, as observed
live, is in [Parity with Claude Code](#parity-with-claude-code). Cursor
remains a detection-only legacy entry. No Gemini or Antigravity adapter ships
with this change.

## Setup and selection

```sh
# Installer: Codex alone, or both agents (--agent both, or --agent twice).
# Windows: $env:AISQUARE_INSTALL_AGENT = 'codex' before `irm .../install.ps1 | iex`,
# which becomes --agent inside WSL.
sh install.sh --agent codex
sh install.sh --agent both

# Or init directly:
aisquare init --local --yes --agent codex
aisquare init --local --yes --agent claude-code --agent codex

# Existing installation:
asq agents connect codex
asq agents use codex
asq agents use codex --project
asq agents status codex

asq launch coder --agent codex
asq team spawn coder --agent codex --exec
asq fleet spawn coder --agent codex --prompt 'Implement the task on the board'

# Mixed teams:
asq team bind reviewer --agent claude-code
```

`install.sh --agent codex` installs a missing Codex with its own installer
(`curl -fsSL https://chatgpt.com/codex/install.sh | sh`, as its
[docs](https://learn.chatgpt.com/docs/codex/cli) give), run so that it asks
nothing; an installed Codex is kept, and `--upgrade-all` runs `codex update`.
It then runs `init --agent codex` and, with Codex alone, `agents use codex`, so
launches use Codex rather than the Claude Code default. Claude Code is neither
installed nor connected. The run ends on a doctor that is green but for one
row: Codex's hooks are `unverified` until you trust them, which the summary
names as the step left to you (run `codex`, sign in, open `/hooks`, trust them).

Connect merges AISquare's handlers into `CODEX_HOME/hooks.json`, preserving
other hooks and instruction files. Unreadable context files are skipped with a
note; hooks still install. Codex requires native trust: open `/hooks`
in Codex, review the definitions and trust them. Reconnect is idempotent and
does not rewrite an identical file. Changing definitions requires native
review again. AISquare never writes Codex trust records or automatically adds
a trust-bypass flag.
Connect validates and installs hooks before importing global memories. Malformed,
unreadable or non-regular native settings produce a file repair diagnostic;
existing settings and memories are preserved on that failure. Updates retain
the file's permissions and symlink target. Replacing a hard-linked file updates
that path while leaving the other hard links' contents untouched.

Status distinguishes configured hooks from observed execution. `unverified`
means the file is installed but this definition has not been observed running.
`observed` means a callback ran; a session's own trust and permission policy
still applies. Evidence identifies the definition that actually ran, so an older
open session cannot verify newly installed hooks. `aisquare upgrade` rewrites
them for the new install (see Upgrade and uninstall); review the updated hooks in
Codex. Doctor reports missing hooks, stale AISquare executables,
short context timeouts and the review step, including account homes discovered
on disk. End and interrupt handlers are local and use Codex's three-second
maximum. Reconnect preserves extra headroom for the two context hooks and
restores the bounded timeouts for the other lifecycle hooks. Events AISquare does
not install (`SubagentStart`, `PreCompact` and later additions) are ignored
without output.

Codex hooks are on by default. `hooks = false` under `[features]` (or the
deprecated alias `codex_hooks`, which `hooks` overrides wherever both are set) in
`CODEX_HOME/config.toml` or a `-p` profile file, or `--disable hooks` /
`-c features.hooks=false` in a launch, switches every hook off, the way
`"disableAllHooks": true` does for Claude Code. Doctor says so, naming the file
or the roles whose arguments do it, and `agents status` names the file; Connect
still installs the hooks and cannot change the switch.

Selection order is explicit `--agent`, role binding, an exact known binary
override (legacy shorthand), project preference, inherited launch family,
operator `AISQUARE_CODING_AGENT`, user default, then Claude Code. An arbitrary wrapper declares its family with
`team bind ROLE --agent NAME --bin PATH` or `--agent` at launch (`NAME` is
`claude-code` or `codex`). With the implicit default or a saved Claude user default, legacy
bin-only wrappers retain Claude compatibility, including inside an AISquare
pane: the parent's exported family does not reclassify another wrapper.
Non-Claude user defaults, project defaults, and operator `AISQUARE_CODING_AGENT` do not
identify an arbitrary wrapper; with those defaults, declare its family explicitly.
Conflicting known binaries and families are rejected.
Changing a default affects future launches. AISquare writes the inherited family
to `AISQUARE_LAUNCH_AGENT` (source `inherited`); it leaves the operator preference
unchanged (source `operator`). This
applies equally to plain shells, fleet windows with an empty launch ID, and
pasted commands. Printed commands for both agents use `aisquare launch`, which
creates a fresh identity each time without exporting anything into the parent shell.
Printed commands carry the resolved native arguments with `--no-bound-args` so
the launcher does not prepend the saved arguments again.
They also authorize custom role names with `--custom-role`; a plain `launch`
continues to catch role typos unless that option or a saved binding declares the role.

Coding agents are optional for a CLI-only installation (`install.sh --no-agent`).
Doctor warns about a missing executable when an agent or launch profile has
been configured explicitly. Managed `--account` slots work for both agents
(see [Codex accounts](#codex-accounts)); a role's `CODEX_HOME` binding still
works for a home laid out some other way.
A merely present, unused Codex home is advisory. `team harness` reports a bad
role with its repair hint alongside the roles that resolve successfully.
`--json agents use codex --project` returns `scope: "project"` and `project_id`.

### In asq: Welcome, Doctor, Settings and the spawn dialog

`asq` takes a Codex user from an empty home to a running manager and two coders
the way it takes a Claude Code user ([the first five
minutes](fleet.md#the-first-five-minutes)):

- **Welcome, step 2** is the agent the fleet launches for the manager: the
  selection order above, and on a machine where Codex is the only agent installed
  and nothing chose one, Codex. It shows Codex's version (read from its install,
  else `codex --version`) and binary, whether the account the manager will launch
  on is signed in (`CODEX_HOME/auth.json`, read locally), and whether AISquare's
  hooks are in that `CODEX_HOME`. *Connect* runs `agents connect codex` (with
  `--config-dir` when the manager's home is not your shell's); *Sign in* opens the
  Accounts page with `codex login` already running on that account; *Check again*
  looks again (the page also does so every few seconds). Hooks switched off
  (`features.hooks = false`) or a `hooks.json` that cannot be read are named
  instead of offering a Connect that cannot help. The trust step is Codex's own:
  the card says the hooks run once you trust them in `/hooks`, and step 3 says
  where (the manager's pane, the first time it starts). With neither agent
  installed, step 2 names both agents' install commands (Codex: its installer
  `curl -fsSL https://chatgpt.com/codex/install.sh | sh`, Homebrew on macOS, npm);
  with both installed and nothing more specific than the user default choosing, it
  offers *Use Claude Code* / *Use Codex* and saves the pick as your default
  (`agents use`).
- **Welcome, step 3** starts the manager and the coders with `fleet spawn --agent`
  on that agent (a role bound to an agent of its own keeps it), each on the account
  the account ladder picks for it. A Codex the page picked because it was the only
  one installed is saved as your default first, so the coders the manager spawns
  later run on Codex too. Nothing is typed into a new agent: Codex, like Claude
  Code, first asks whether to trust the folder.
- **Doctor** rows for each Codex home: connected (`configured`/`observed`),
  `partial` (some of AISquare's hooks, naming the missing events), not connected,
  hooks naming a stale or missing aisquare, hooks switched off, `unverified` (the
  trust step, whose fix is `/hooks` in Codex — no button can trust hooks), the
  plugin route (connected through the aisquare Codex plugin, its hooks not trusted
  yet, both routes installed — keep one — or the plugin running in place of
  `hooks.json` hooks whose aisquare is gone) and the account rows
  (`codex-accounts`, defaults, bindings, limits). *Connect* fixes are
  one click, and so is a slot's sign-in: *Sign in account N* opens the Accounts
  page with that slot's `codex login` in its pane. `aisquare --json doctor` carries
  every row.
- **Settings** edits, per role, the agent (`team.profiles.<role>.agent`), Claude
  Code's permission mode, Codex's sandbox, approval policy (`on-request` or
  `never`) and `approve_for_me`, the worktree switch and the account binding (whose
  list names any slot of another agent, which a launch would refuse). Under each
  role is the line its next `fleet spawn` passes for permission — the adapter's own
  flags (`--approve-for-me` with the store's profile, `--ask-for-approval on-request
  -c approvals_reviewer="auto_review"` read-only, `--permission-mode auto`) — or the
  refusal the spawn would meet. The role's
  one permission for every agent (`fleet.roles.<role>.permission`,
  `WRITE:APPROVALS`) is a select of its own, and the line shows what each agent
  would be passed under it ([the table](fleet.md#one-permission-for-every-agent)).
- **The spawn dialog** (`＋ spawn agent`) is `fleet spawn` with the role, agent,
  account (the launching agent's slots only) and permission (Claude Code's
  `--permission-mode`, or Codex's `--sandbox` and `--approval`), and shows the
  flags the launch will pass. It calls the same `services.fleet.spawn`.

Model/account profiles continue to use `team bind`.
Settings retains custom values written by another version, so they can be
reviewed or corrected without losing them when saving unrelated changes.
A damaged AISquare config renders defaults and an error; saving preserves the
damaged file until it has been repaired and reloaded.

### The Codex plugin

Without the installer, Codex can take aisquare's hooks from a plugin:
`codex plugin marketplace add AISquare-Studio/aisquare-cli`, then
`codex plugin add aisquare@aisquare-cli`, then trust its hooks in `/hooks`.
They run `aisquare hook codex` and stand down while `$CODEX_HOME/hooks.json`
runs aisquare's; `aisquare agents status codex` names the route (`hooks`,
`plugin` or `both`). See [The Codex plugin](codex-plugin.md).

### Upgrade and uninstall

`aisquare upgrade` re-connects Codex's hooks as it does Claude Code's: the new
install runs `agents refresh-hooks codex` for every Codex home this machine
connected and for each Codex account home under `~/.aisquare/codex-accounts`,
so the hooks name the new install. Only homes holding AISquare hooks are
rewritten; other groups, `config.toml` and `AGENTS.md` are left as they are,
and a home whose hooks run another aisquare install is left and named. An
identical definition is not rewritten; a changed one is reported with the
review Codex asks for in `/hooks`. `--version` back to a release without Codex
hooks (0.8.0 or older) leaves them, naming `agents disconnect codex` to run first.

`aisquare uninstall` takes AISquare's hook groups out of every Codex home it
finds (`~/.codex`, `~/.codex*`, `$CODEX_HOME`, connected homes, account
homes), keeping every other group. It reads each home's `config.toml` and
never rewrites it: a `[mcp_servers.<name>]` that runs aisquare is listed with
the `codex mcp remove <name>` that removes it there (with `CODEX_HOME=` set for
a home other than the one your shell's `codex` reads), and an enabled aisquare
Codex plugin with its `codex plugin remove`. `--purge` refuses while either
would start aisquare again, as it does for Claude Code's. MCP servers added only
for a launch (`agents.mcp = true`, `-c mcp_servers.aisquare…`) are never written
to a file, so there is nothing to remove.

## Parity with Claude Code

Each Claude Code feature AISquare offers, and what it does on Codex, as observed.
Two sources. The first is a live smoke test by the team's tester: Codex 0.162.1,
signed in, beside Claude Code 2.1.299, with temporary homes kept out of `/tmp`
(where every sandbox may write), at `273652fc`. It passed 16 of its 18 items and
filed eight defects. The second is the fix for each of those defects, with the
live test on the real Codex 0.162.1 that now covers it. Those tests are in
`tests/test_codex_native.py`, run against a loopback provider: *live hooks* is
`test_real_codex_hooks_resume_and_usage`, *live panes* is
`test_real_codex_panes_take_a_told_line_before_any_hook_and_the_managers_nudge`, and
*live sandbox* is `test_real_codex_reaches_the_store_from_its_sandbox_and_rings_only_for_a_human`.
The live sandbox test passes on a Linux machine. On GitHub's runners it skips,
because their bwrap may not bring up loopback, so CI's Codex job does not
evidence those rows. *Smoke N* is the smoke's item N. The smoke itself has not been run again since the
fixes.

| Feature | Claude Code | Codex 0.162.1, as observed | Evidence |
|---------|-------------|----------------------------|----------|
| Install and connect | `install.sh` installs Claude Code, `init` writes its hooks into `settings.json`, and they run at once. | The same commands with `--agent codex`, and the hooks go into `CODEX_HOME/hooks.json`. **Differs:** Codex runs no hook until you trust it in `/hooks`. `agents status codex` reads `unverified` until then and `observed` after. | Smoke 1 |
| Welcome: a manager and two coders | Step 2 is Claude Code; step 3 starts the fleet. | The same on Codex: step 2 shows its version, the manager's account signed in, and connected. It reads the hooks in the home of the account the manager launches on (`~/.aisquare/codex-accounts/<n>` when a slot is the default), and Connect installs them there. **Differs:** on its first start, Codex asks in the manager's pane to trust the folder and review the eight hooks. | Smoke 2. Account home: `4df3fd44`, unit tests (no live rerun) |
| Context at session start | `SessionStart` hook context | Same: the Codex manager quoted its "You are team session … (role: manager)" line. | Smoke 3; live hooks |
| Prompt capture (`aisquare log`) | `UserPromptSubmit` | Same, with `source=codex`. | Smoke 4; live hooks |
| ▶ working / ⏸ waiting | From its hooks; a fresh agent is waiting from its `SessionStart`, which fires once it sits at its prompt | Same, from Codex's hooks. | Smoke 5. Claude Code's fresh agent: SMOKE2 (it read working until a first prompt), fixed in `c29a2670` |
| 🔔 attention | `Notification`: a permission prompt or a question | 🔔 for an approval the human answers ("Codex needs your approval") and for `request_user_input`. None for one Codex's automatic reviewer decides under `--approve-for-me`. **Differs:** Codex fires `PermissionRequest` with the same payload for both, so the reviewer is read from the session's rollout. Codex also redraws its approval view every second, so pane activity does not clear the bell. It clears when Codex's working line (`esc to interrupt`) is back on the screen, which is the moment the human allows the command, or on Esc, `Stop` or the next prompt. | Smoke 5 failed both ways (seven false bells for one task; a real approval read working). Fixed in `3b11964c`, `182f70e7` and `467d4ef4` (review r2). Live sandbox: a reviewed escalation rang no bell, attention held through the redraws, and the row read working within 5 s of `y`, while the approved 8 s command still ran |
| ⏳ limited and `on_limit = "switch"` | The `StopFailure` hook. Claude Code's own wait-and-continue runs at the reset. | **Differs:** Codex has no `StopFailure`, and 0.162.1 fires no `Stop` for a turn that ends on its limit. The limit is read off the rollout at `Stop` and `Interrupt`, and by the fleet's read of a row its board says is working; nothing continues at the reset. | Live: a team plan at its spend cap (SMOKE3) and a loopback provider answering the same `429` (`tests/test_codex_native.py`) |
| Manager continuation | The `Stop` hook's "keep going" decision | Same: "Blocked by hook: 1 board update(s)…", and the manager went on. | Smoke 6 |
| Waking a waiting manager | The nudge is typed into its prompt. | Same. Every line the fleet types is now a bracketed paste with its Enter 0.2 s later. **Differs:** Codex reads a burst of keys as a paste and keeps an Enter inside it as text. A manager whose hooks went stale is nudged only when its pane shows the empty prompt. A board write made in Codex's sandbox cannot reach the fleet's tmux, which stays out of its reach: it leaves the nudge owed, and the agent's own `PostToolUse` hook, which Codex runs outside the sandbox, sends it as the command ends. | Smoke 6 failed (three nudges left unsent in the composer). Fixed in `6ee17ee3`. Live panes: a waiting manager runs the nudge a coder's task review sends. SMOKE2 6 failed: a sandboxed result, verdict or block never woke the manager, Codex's or Claude Code's. Fixed in `43237797`. Live sandbox: a sandboxed `--kind result` wakes the waiting Codex manager, and `tmux` from the sandbox is still refused |
| `fleet tell` | Typed when it is waiting, else a board note | Typed when its hooks say waiting. **Differs:** a busy agent's message is queued with `codex queue --message=<text>` and runs as its next turn (`✓ queued for its next turn`), even a message that starts with `-`. Claude Code has no queue. Before its first prompt (no hook yet), the message is typed when the pane is quiet and shows `? for shortcuts`, and after it only when the pane shows it too: at the folder-trust or hook-review dialog, which is as quiet, at the rate-limit model dialog Codex opens after its `Stop` (*Switch to gpt-6-luna?*), or with a draft in its composer, it is a board note, so the dialog and the draft stay the human's. A fleet launch hides that model dialog (`-c notice.hide_rate_limit_model_nudge=true`) unless the operator's config sets the key, and `fleet stop` types no `/exit` into a dialog: it kills the window and says why. | Smoke 7 (typed, then queued; a never-prompted agent got only a note). That case was fixed in `47c9f23b`. Live panes: "reply ok" told before any hook ran, and the hook review was left alone. Live hooks: a queued `--no-verify …` ran as the next turn (`fc860457`, review r2) |
| `fleet stop` | Types `/exit`; `SessionEnd` releases the claims. | Same: `/exit`, with its Enter apart, and exit 0. Codex's `SessionEnd` ended the session 0.5 s before the fleet's `agent_exited`. | Smoke 8; live hooks |
| `fleet restart`, Restart | `claude --resume <transcript>` | Same outcome: `codex <options> resume <thread> -- <continue line>`. It quoted its question and answer from before the restart, and its claim was kept. | Smoke 9; live hooks |
| `/clear` | Row and claims move to the new session. | Same: the row and the claim moved, and the new thread knew its claim. **Differs:** Codex fires no `SessionEnd` for the thread it leaves; the new thread's `SessionStart` (clear) comes with its first prompt. | Smoke 10; live hooks |
| A session a tool starts | `CLAUDE_PID` tells the agent's own process from a child it starts. | `CODEX_THREAD_ID`, which Codex sets only in its tools' shells, does the same. A session of another agent than the row's never takes the row, such as a `codex exec` from a Claude Code agent's Bash tool. | Measured on 0.162.1; unit tests (`5898ce7e`, review r2) |
| Accounts and usage | Numbered slots; usage from the endpoint Claude Code's `/usage` reads | The same slots, on one ladder (`~/.aisquare/codex-accounts/<n>`), with doctor rows. **Differs:** usage is read offline from the account's own rollouts (49 % of 5 h and 8 % of the week in the smoke). It is only as fresh as the last turn, and has no *to the limit* trend. | Smoke 11 |
| `fleet switch` to another account | Resumes the session on the new account. | **Differs:** a Codex thread lives under its `CODEX_HOME`, so the agent starts fresh with the hand-off prompt (which got through the new home's folder-trust dialog) and its claim moves. A switch onto the same home resumes. | Smoke 12 |
| Claim, review, done | Board tasks | Same: a Codex coder claimed a task and sent it to review, and a Codex tester marked it done. | Smoke 13 |
| Board calls from inside the agent | No sandbox: `aisquare` runs as the user. | Same since `e8436fa3`: a sandboxed fleet Codex runs under the fleet's `aisquare` permission profile, and its board calls succeed on the first, sandboxed attempt. **Differs:** Codex sandboxes its tools. Its account homes, config, credentials and keys, the fleet's tmux conf and the agent registry are read-only to it. A restart, switch, hand-over or `tell` queue runs from the row's launch record only, written by its spawner and its own hook where the sandbox cannot write: binary, arguments, permission, role, agent, directory, account, transcript and thread; a row without one, or a project whose directory in the store is not the record's, is refused. What is typed into a pane or kills a window goes to the pane the record places (socket and pane), never the row's, and a failed turn's limit is read from the record's transcript. A pick in its own `/permissions`, or a `codex` started by hand, uses its own sandbox ([the sandbox and the store](#the-sandbox-and-the-aisquare-store)). | Filed by the smoke: every call needed an escalation. Live sandbox: a note and a task claim succeeded sandboxed. Writes to the read-only paths were refused, and the launch record held (`ca6d43ea`, `8337e88c`, review r2; review r3; one launch record, review r4: a sandboxed reviewer's rewrites of its thread, home, role, binary and account replayed as recorded, the project's directory refused; review r5: a row pointed at another pane or transcript is refused or ignored) |
| Who answers approvals (`ask` / `auto` / `never`) | `--permission-mode manual` / `auto` / `dontAsk` | Same intent. `ask` puts Codex's "Would you like to run the following command?" in the pane, and `auto` is `--approve-for-me`. **Differs:** the reviewer is pinned on every spawn, restart and resume (`approvals_reviewer`), because a resumed thread otherwise keeps its own. `full:auto` is refused. | Filed by the smoke: a restart onto `ask` kept the automatic reviewer. Fixed in `e8436fa3`. Live sandbox: `fleet restart --permission-mode workspace:ask` brought the prompt |
| Read-only reviewer | `--disallowedTools=Edit Write NotebookEdit`; its shell can still write | **Differs, and is stricter:** the store profile over `:read-only` with Codex's reviewer (`read_only:auto`). The worktree is read-only to the sandbox, and its verdict still reaches the board. | Live sandbox: the reviewer posted its verdict |
| MCP (`agents.mcp = true`) | AISquare's stdio server per launch | Same: Codex called `aisquare.team_board` and `aisquare.note_add`, and the note landed on the hook-bound row. | Smoke 14; live hooks |
| Explainability | Model traffic through the explainability proxy | **Differs:** Codex exports its own OTLP logs to a per-launch loopback receiver, which the SDK replays. The smoke spooled 58 native events: token counts and tool names, no tool arguments. The receiver's token reaches Codex in its environment, never its command line, and is kept out of the shells its tools run in. There is no gateway for OpenAI Responses traffic. | Smoke 15. Token: `6efabab1`, where a scan of every `/proc/*/cmdline` found it 0 times, and `63d61be4` (review r2). Live hooks: the native events spool, and a tool's shell read the variable empty |
| A team on both agents | Claude Code manager | Codex coder: each quoted the other's note from context, and a Codex `--kind result` woke the Claude Code manager. | Smoke 16 |
| Upgrade and uninstall | Re-connects, or removes, the hooks. | The same for every Codex home and account home. `uninstall` names the `codex mcp remove` / `codex plugin remove` commands. **Differs:** a changed hook definition needs a new `/hooks` review. | Smoke 17 |
| Doctor | Claude Code rows | Same on a Codex-only home with no `claude` on PATH: exit 0, Codex `observed`. | Smoke 18 |
| Model and effort per role | Its ladder, with an availability probe | **Differs:** pins in `[agents.models.codex]` and no AISquare probe. Levels pass unchanged, and `ultracode` becomes `max`. | Live hooks: each level reached the provider |
| The ui-tester's browser | `--chrome` | **Differs:** no `--chrome`. The doctor names Codex's own `browser_use` feature and browser plugin. | Unit tests; not exercised live |
| shift+Enter in asq's pane | A newline | **Differs:** sent as ctrl+J, Codex's newline (Codex submits on shift+Enter). | Measured on 0.162.1; unit tests |
| Native sub-agent teams off | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0` | `-c agents.enabled=false` | The launch line on a real Codex manager |
| Plugin route | `claude plugin` | Same: `codex plugin add aisquare@aisquare-cli`. It stands down while `hooks.json` runs aisquare's hooks ([the Codex plugin](codex-plugin.md)). | Real Codex 0.162.1 with a local marketplace; not in the smoke |

## Codex accounts

`aisquare accounts` and the Accounts page manage Codex logins as they manage
Claude Code's ([Accounts](fleet.md#accounts)): numbered slots, an alias, a
priority order, a default, *Disable*, usage, a headroom pick and the hand-over
on a usage limit.

- **Slots.** One ladder numbers both agents' slots, so a number names one
  login: `aisquare accounts add --agent codex` takes the lowest number no
  Claude or Codex slot uses and makes `~/.aisquare/codex-accounts/<n>/` (its
  marker names `"agent": "codex"`). Existing Claude slot numbers never change.
  Slot **0** is the plain `codex` of this shell (`CODEX_HOME`, else
  `~/.codex`), listed once that home exists; slot **1** stays the plain
  `claude`. A launch on a Codex slot sets `CODEX_HOME` to it and keeps the
  shell's own as `AISQUARE_PLAIN_CODEX_HOME`, so a process under the slot still
  knows which home the plain codex is. Codex has no scratch variable of its own.
- **Sign-in.** `add --agent codex` runs `codex login` with the slot's
  `CODEX_HOME` (in the foreground; the Accounts page runs it in a tmux window
  as `aisquare accounts run <n> login`). It writes `auth.json`; once that holds
  a login, the slot is recorded and `agents connect codex --config-dir <slot>`
  installs AISquare's hooks there (Codex asks you to trust them on its first
  session). A slot nothing signed into is discarded. `accounts run <n>` starts
  `codex` on the slot.
- **Identity.** Read from the claims of the id token in `auth.json` — `email`,
  and `chatgpt_plan_type` / `chatgpt_account_id` under
  `https://api.openai.com/auth` — decoded locally and never verified, sent or
  printed. No token value is ever read beyond whether one is there. An API-key
  login shows `API key`.
- **Usage, offline.** There is no public usage endpoint and AISquare makes no
  request for Codex: each session writes a rollout
  (`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`) whose `token_count`
  events carry `rate_limits`, and the newest one under a home that carries a
  window is that account's usage: at its limit 0.162.1 records one more, `limit_id`
  `premium` with both windows `null`, which only says the limit was reached. The
  shorter window is shown as the session window and the longer one
  as the week; some plans have only a weekly window. A window whose reset has
  passed since that reading reads 0 %, and the reading is labelled with its
  age (`as of 3h ago`) because it is only as fresh as the account's last turn.
  A Codex reading is not kept as a usage sample, so its bar shows no
  *to the limit* trend.
- **Choosing.** `--account`, a role's `account` binding, `accounts default
  --project` and the machine default all answer with an account of the role's
  agent: a Codex role takes a Codex slot. A Claude slot named for a Codex role
  (or the reverse) is refused naming both agents. Defaults are per agent;
  `accounts default <ref>` sets that account's agent's default and
  `accounts default --clear --agent codex` clears Codex's. An email signed in
  to both agents resolves to the launching agent's account; without one it is
  refused as ambiguous.
- **Usage limits.** Codex has no `StopFailure` hook. On its `Stop` and
  `Interrupt` events AISquare reads the end of the session's rollout: a
  `rate_limit_reached_type` on the newest `token_count`, or this turn's error
  saying so — an `error` event with `codex_error_info: usage_limit_reached`, the
  turn's `task_complete` `error` with `usage_limit_exceeded` (0.162.1), or the text
  `You've hit your usage limit` or `spend cap` — parks the row **limited** with the
  reset time of the window that is full, puts `limited` on the board and wakes the
  manager — exactly what Claude Code's limit does. Codex 0.162.1 fires no `Stop`
  for a turn that fails, on its limit or any other error (only its
  `UserPromptSubmit`, and no `notify`), so the fleet's read of a row the board
  says is working (`fleet ls`, the app's tick, `fleet tell`) reads the end of its
  rollout as well: a limit is recorded as above, once per turn, and any other
  error returns the row to waiting with a `turn_failed` line, as Claude Code's
  `StopFailure` does. With `[accounts] on_limit =
  "switch"` the hand-over moves the agent to the Codex account with the most
  headroom. A limit found when the session ends is recorded without a move.
  AISquare relies on no wait-and-continue from Codex at the reset (Claude
  Code's): a parked Codex agent that is not switched waits for its next prompt.
- **Switching to another account is fresh.** A Codex thread lives under its
  `CODEX_HOME` and cannot be resumed from another, so `fleet switch` of a Codex
  agent onto another account (by hand or automatic) starts it fresh with the
  board-built hand-off prompt and moves its claims onto the new session, as
  `--fresh` does. A target on the home the thread ran under resumes it.
- **Doctor** lists Codex slots and their headroom beside Claude's; reading
  Codex headroom needs no network, so it does not wait for `--live`.

## Native settings

Codex resolves `--config-dir`, profile `CODEX_HOME`, ambient `CODEX_HOME`, then
`~/.codex`. Connect imports the effective global `AGENTS.override.md` or
`AGENTS.md` into the user memory pool. Project and nested instruction files
remain under Codex's native project discovery; they are never promoted to
global memory or overwritten by AISquare.

Codex keeps its native model default. Configure optional agent and role
preferences in AISquare's `config.toml`:

```toml
[agents]
default = "codex"
mcp = false

[agents.models.codex]
model = "your-model-id"
effort = "high"

[agents.models.codex.roles.reviewer]
effort = "xhigh"

[team.profiles.coder]
agent = "codex"
bin = "codex"
env = { CODEX_HOME = "/path/to/codex-account" }

[fleet.roles.coder]
approve_for_me = false        # the default is Codex's --approve-for-me
sandbox = "workspace-write"
approval_policy = "on-request"
agent_args = { codex = ["--no-alt-screen"] }
```

`AISQUARE_MODEL_<ROLE>` and `AISQUARE_EFFORT_<ROLE>` remain explicit role pins.
Codex names `minimal`, `low`, `medium`, `high`, `xhigh`, `max` and `ultra`; these
pass unchanged, and which of them a model takes is Codex's to settle (a level the
model lacks is sent as its default). The shared name `ultracode` maps to Codex's
`max`, with a note identifying the mapping: a native reasoning level, without
Claude workflow orchestration. Another level name is passed on with a note for
Codex to validate; a value that is not a level name is refused. AISquare-configured values are trimmed and effort
names ignore case; blank environment/config pins fall through to native defaults.
An explicitly empty `--effort` is a usage error. Native model availability is left to
Codex, without paid discovery probes. `--probe`, `--no-probe` and `--refresh`
explain that there is no AISquare Codex availability cache. Claude retains its ladder, but probes
now use the selected executable and effective account. Cache keys include the
resolved executable's upgrade fingerprint, login identity and provider inputs.
File-backed Claude credentials also contribute their subscription and rate-limit
tier. On macOS those entitlements live in Keychain and are not read for cache
hashing: use `team spawn ROLE --refresh` after a plan change, or wait for expiry.
Parent-session variables, OAuth refresh timestamps and hook edits do not
invalidate the 24-hour cache. `--refresh` clears the selected account's scope.
Both refresh and ordinary cache writes prune expired scopes while retaining
other accounts' valid cached verdicts.

```sh
asq fleet spawn reviewer --agent codex --sandbox read-only --approval on-request
asq launch coder --agent codex -- resume NATIVE_SESSION_ID
asq launch coder --agent codex -- exec --json 'Run the local checks'
```

Use `--` to explicitly separate native options from AISquare options. Codex
`-c KEY=VALUE` and `-cKEY=VALUE` also work before the separator, as do attached
model options such as `-mMODEL`. Legacy `-c BINARY` selects an executable;
`--command BINARY` is unambiguous, including for a path containing `=`.
Attached AISquare options retain their values, including spaces in executable
paths or environment assignments. Once a native subcommand or positional prompt
begins, all following tokens belong to the agent. For example,
`asq fleet spawn coder --agent codex exec -listfiles` forwards both native tokens;
`asq launch coder --agent codex exec -- -listfiles` preserves the native separator.
Known native option values such as `--model MODEL` and `--add-dir DIR` do not
start that boundary. For an unknown native flag whose arity is ambiguous, put
AISquare options first and use `--`; the parser refuses ambiguous mixed ownership.
An explicit separator before any native command belongs to AISquare. To pass a
literal dash-prefixed prompt directly, include the native separator too:

```sh
asq launch coder --agent codex -- -- '-migrate the schema'
```

Explicit native model/effort options override AISquare defaults without injecting
a second value. Codex validates its own `-c` values, including future reasoning
levels; the shared alias `ultracode` is rewritten to `max` in the actual native
arguments. AISquare validates its explicit `--effort` and the
effective Claude effort saved with `--arg`. One-off native passthrough is validated
by the agent, including future levels; a later native effort supersedes a saved one. `team harness` and `team spawn` report these choices with source `native`;
these are argv overrides, not availability claims. Codex parses `-migrate` as
`-m igrate`, including a space-containing suffix in a quoted token. To preserve
a configured model and send that text as a prompt, use the two separators above.
When native effort overrides AISquare's `--effort`, spawn reports the precedence.
`team harness` also reports fleet-specific arguments and model choices separately
from the defaults used by `team spawn`.
Native sandbox and approval policies stay separate.
Codex fleet roles run with `--approve-for-me` (Codex's automatic reviewer, in the
workspace-write sandbox), the counterpart of the `auto` permission mode Claude
Code fleet roles get; reviewers, including numbered seats such as `reviewer2`,
run read-only with `--ask-for-approval on-request` and that same automatic reviewer
(`read_only:auto`) instead. Every sandbox the fleet gives Codex is the store's
profile below, never `--sandbox`, except `danger-full-access`. Codex refuses
`--approve-for-me` beside `--sandbox` or `--ask-for-approval`, so a role (or a
spawn) that chooses a sandbox or an approval policy, in its settings or its
native arguments, runs without it, and its approvals are the human's
(`-c approvals_reviewer="user"`); `approve_for_me = false` turns it off, and
`approve_for_me = true` beside a conflicting choice is refused at spawn. The
approval policies are `on-request` and `never`: Codex no longer supports
`untrusted`, `on-failure` is deprecated, and the granular table is native
config. A sandbox or policy given in the native arguments is not given twice.
Numbered seats inherit the base role's sandbox, approval policy and
`approve_for_me` per field unless configured separately.
Their worktree, Claude permission mode and extra arguments keep their own
defaults. Saved Codex sandbox/approval
settings apply only to Codex; Claude Code keeps its own permission mode.
Legacy fleet `extra_args` belong to Claude Code; `agent_args` maps each agent
name to its own extra arguments. A customized Claude role therefore cannot
forward `--restricted` or `--verbose` into a Codex fleet launch, and a Codex
spawn of a role that carries them says where Codex's go. The reviewer's
built-in `--disallowedTools=Edit Write NotebookEdit`, which `init` writes, is
not mentioned: nobody chose it, and Codex's reviewer has its read-only seat.
No approval bypass is added: `--approve-for-me` reviews each request.
Native subagents are disabled in fleet sessions when
`fleet.disable_native_agent_teams` is enabled.

### The sandbox and the aisquare store

Every board call an agent makes opens the store (`context.db` in the aisquare home)
for writing, a read such as `aisquare board` included, and Codex's sandbox lets its
commands write the workspace and the temporary directories only. Measured on 0.162.1
with the home in `~/.aisquare`: each `aisquare` call failed in the sandbox ("unable
to open database file") and was re-run as an escalation, so under `--approve-for-me`
every board read and write was a round trip through Codex's reviewer, and under a
human's approvals every one asked the human. A home under `/tmp` hides this, as every
sandbox may write there.

So the fleet runs a sandboxed Codex agent under a permission profile of its own,
`aisquare`, which its launch defines (`CodexAdapter.store_access`):

```text
-c default_permissions="aisquare"
-c permissions.aisquare.extends=":workspace"        # ":read-only" for a reviewer's seat
-c permissions.aisquare.filesystem={"<aisquare home>" = "write",
     "<aisquare home>/claude-accounts" = "read", "<aisquare home>/codex-accounts" = "read",
     "<aisquare home>/cache/claude-accounts" = "read", "<aisquare home>/cache/codex-accounts" = "read",
     "<aisquare home>/cache/oidc" = "read", "<aisquare home>/launch-seals" = "read",
     "<aisquare home>/credentials" = "read", "<aisquare home>/config.toml" = "read",
     "<aisquare home>/fleet-tmux.conf" = "read", "<aisquare home>/agents.json" = "read",
     "<aisquare home>/explainability-key" = "read"}
```

This is Codex's built-in profile for the write scope, with the aisquare home
writable, so a sandboxed agent can write any row of the board's store
(`context.db`). The rule (`core.launch_seal`): nothing it can write there chooses
what runs outside the sandbox, what is typed into, or what is killed. What does is
read-only, or recorded where the sandbox cannot write, or validated against something
it cannot write; for tmux, by construction: one resolver places every pane and session
the fleet acts on from a launch record, and a test fails on any tmux call that does not
go through it (below).

Read-only inside the profile (`types.store_read_only`):

- the account homes and their caches: a Codex home's `config.toml` holds its hook
  trust and its hooks run unsandboxed, and a Claude Code home's settings and hooks
  likewise;
- the sign-in tokens, the credentials and the explainability key;
- aisquare's config, which chooses every later launch's flags;
- `fleet-tmux.conf`, which every fleet tmux server loads;
- `agents.json`, the homes `aisquare upgrade` reconnects hooks in;
- `launch-seals/`, the records of the fleet's launches.

Recorded: a fleet row's launch, as one record that only its spawner writes, in
`launch-seals/` (a file named by the SHA-256 of the row's id, never by the id the
store holds). `fleet spawn` writes it before the window starts, from what it
resolved itself: the project and its directory, the role, the agent, the binary,
the working directory and whether it is a worktree, the account (its slot and its
home) and the launch spec (permission mode, arguments, the window's command line).
Once tmux has made the window, the spawner adds where it is: the tmux socket's name
and path, the pane, the server's boot and the row's time, in a file of its own beside
the record, before the row is written; a window whose place cannot be recorded is
killed. The agent's own hook adds its session (the id, the native id, the transcript
and the home), from the payload the agent handed it, in a process outside any sandbox:
Claude Code has none, and Codex runs its hooks outside the one its tools run in. A
tool in the sandbox that runs a hook itself cannot write there.

`fleet restart`, `fleet switch` and the usage-limit hand-over start a replacement
outside any sandbox from that record and nothing else from the store: the binary,
arguments and permission, the role, the agent, the directory (a worktree's as it
stands), the account and, when it resumes, the transcript and the native thread.
`fleet tell` queues for a busy Codex agent with the record's binary, thread and
home. A row with no record (spawned by an aisquare that keeps none, or whose record
could not be written), a record that cannot be read or was written by a newer
aisquare, and a project whose directory in the store is not the record's are refused
before anything is stopped, with the reason and the way out (stop the agent, spawn a
new one). The reason is also a board note, once per row and reason, whichever path met
it first (restart, switch, the hand-over, a message told to a busy agent), so an
unattended hand-over leaves it where the manager and `fleet ls` readers look. Nothing
reads the store to write a record, and a spawn that cannot write one starts no agent,
asked before the project is captured or a worktree made. A record is read back as it
was written, so a release that adds a field reads an older record with that field's
default.

Where the fleet acts on a row's window is decided in one place, the placement
resolver (`services.fleet.resolve_place`): it answers with the pane, the session, the
server and the agent the row's launch record holds, or with a refusal saying why there
is none. Every tmux question or act about an agent goes to what it answers, never to
the row's `pane_id`, `tmux_socket` or `socket_path`, its `agent`, or the project's
codename: `fleet tell`'s typing and a spawn's prompt, the manager's nudge (which a
sandboxed agent's own hooks deliver from outside its sandbox), `/exit` and the kill of
`fleet stop` and with it restart, switch, the hand-over and `shutdown`, whether an agent
runs (`restart`, the listings, `reap`, a spawn's reconcile), its lingering and late
windows, the sessions `shutdown` takes down, `fleet rename` and `fleet attach`. The
record's agent's rules decide whether to type (its readiness check keeps an Enter out of
Codex's dialogs) and whether it redraws while it waits on you (its bell). A test,
`tests/test_fleet_placement.py`, enumerates every tmux call in the fleet's and the
board's services and fails on one that names a pane or a session the resolver did not
place, or on a place made from anything but a launch record or what tmux itself listed.

A row rewritten in the store to name another pane, socket or agent is not typed into
(`tell` files the board note, the nudge waits), and is stopped where its record places
it, so one `UPDATE` of its own row does not make an agent unstoppable; the board says
what was rewritten. Sessions are tmux's own ids (`$3`), which the record holds: a spawn
joins only a session a record of its project names, else makes one of its own, so a
swap of two projects' codenames never puts an agent into the other's session;
`shutdown` takes down the sessions its live agents' records name, and the dead windows
of agents that ended within a day, never `asq-<codename>`; `fleet rename` renames those
sessions and the records' names for them, whether or not tmux could be told; `--all`
also sweeps every `asq-` session tmux lists. A row with no record (spawned before this
version) is not typed into, asked about or listed as anywhere: `fleet ls` shows it
lost, saying why. `stop` and `shutdown` kill its window, with no exit command typed,
only where tmux lists it in a session a record of its project names and no record
places another agent there, so its claims are released by the stop, not by its own
`SessionEnd`; elsewhere they refuse, nothing typed or killed. One spawned before this
boot is ended as lost; otherwise end its agent in its own window, and `fleet shutdown
--forget` ends its row on your word.
What the fleet reads of a session to act on outside the sandbox, a Codex turn that
failed on its limit with no hook (which parks the row and may hand it over), is read
from the transcript the record holds, never the board session's, and only by a reader
whose tmux answered, so a Codex manager's sandboxed `fleet ls` leaves it to the app's
tick or a `fleet` command of yours.

Validated, besides: a native id goes on Codex's command line only as a UUID, the
shape Codex makes (`codex resume` and `codex queue` read a value that starts with
`-` as an option of their own), even from the record.

The rest of the board stays the agent's to write, as on Claude Code: tasks, notes,
its session's state (which decides whether `fleet tell` types, queues or files a
note), and the project's default coding agent (`aisquare agents use --project`),
which a later `fleet spawn` without `--agent` reads; that one is checked only to
name an installed agent.

A path denied reading (`"none"`) is not used: with one, Codex reads its
own instruction files through its sandbox helper, and where bwrap cannot bring up
loopback the session does not start.

Codex's sandbox leaves an empty file on the host where a path its profile names does
not exist (measured), so the launcher makes those paths first. Codex's own guards in
the workspace (`.git`, `.codex`, `.agents`) still hold. Measured: `aisquare note`, `task claim` and `board` succeed on their first, sandboxed
attempt in the TUI, `exec` and `resume`; the profile holds beside `--approve-for-me`
and over a `sandbox_mode` in `config.toml`. A plain `--add-dir <aisquare home>` (or
`sandbox_workspace_write.writable_roots`) reaches the store too, but a sandboxed command
could then rewrite its own `CODEX_HOME`, so the fleet does not use it.

The reviewer's seat is the same profile over `:read-only`: its board writes (its
verdict) reach the store, and the worktree stays read-only to the sandbox, which is a
filesystem guard where Claude Code's reviewer has only `--disallowedTools`. What the
profile does not cover:

- `--sandbox` (and `-c sandbox_mode=…`, `default_permissions`, `permission_profile`,
  `sandbox_workspace_write.*`) given in the native arguments chooses the sandbox the
  older way, which replaces any profile: the fleet adds none, and the store is then the
  operator's to reach.
- A pick in Codex's own `/permissions` swaps the profile for a built-in one until the
  agent is launched again; `aisquare fleet restart <label> --permission-mode
  workspace:ask` (or any `WRITE:APPROVALS`) restores it, with the reviewer that says.
- A Codex started by hand (`codex`, or `aisquare launch` without the fleet) runs in
  the sandbox its own config chooses. Inside one, a board call that cannot open the
  store says so and names the directory, and never tells the agent to move the store
  aside: nothing in it is damaged.
- `danger-full-access` (`full:*`) has no sandbox, and so nothing to add.

Who answers an approval is pinned with it: `ask` is `-c approvals_reviewer="user"`
(Codex's "Would you like to run the following command?" in the pane), `auto` is
`--approve-for-me` or `approvals_reviewer="auto_review"` (Codex's reviewer, no prompt),
on a spawn, a restart and a resume alike. A resumed thread otherwise keeps the reviewer
it ran under, and `config.toml` may name either.

## Identity, MCP and telemetry

Codex board IDs are scoped by agent, config home and native thread ID. A launch
token and fleet token bind hooks without guessing a newest transcript. Binding
metadata can arrive before the fleet row; resume keeps the same board identity.
Callback deduplication prevents concurrent prompt capture and repeated manager
decisions. Generated continuations have their own prompt source.
Failed callbacks release their owned pending claim immediately so retries can
recover. Pending claims expire after 180 seconds using a monotonic clock.
An unavailable observation cache costs readiness evidence, not lifecycle processing.
Launch bindings and retry records expire after 24 hours without session activity.
Active sessions retain their bindings and replay history. Metadata without a
historical timestamp gets a full retention period on upgrade; restored rows with
an unknown (zero) timestamp are retained. Queued insights are retained too.
Cleanup runs on all native session starts and receiver maintenance, at most once
per minute across processes sharing the store. Ordinary starts between cleanup
runs use an indexed timestamp read.

Initial fleet tasks use Codex's positional prompt. After that, `fleet tell`
types into a Codex agent whose hooks freshly say it is waiting — or, before any
hook has fired (no first prompt yet), whose quiet pane shows Codex's empty prompt
— queues the message for the next turn of one they say is working (`codex queue
--thread <id> --message=<text>` under its own `CODEX_HOME`, the text joined to its
option so one starting with `-` is not read as an option), and otherwise files a
board note ([`fleet tell`](fleet.md#fleet-tell)); the manager's wake-up nudge
goes in by the same rule, its Enter apart. A permission request the human answers,
and a `request_user_input`, mark attention; one Codex's automatic reviewer answers
does not, though Codex fires its `PermissionRequest` hook for both with the same
payload: the reviewer comes from the session's rollout (its newest `turn_context`, or
a `/permissions` change since). Codex redraws its approval view every second while
it waits, so pane output does not clear the bell. The bell clears when Codex's working
line (`• Working (12s • esc to interrupt)`) is back on the pane's screen, where Codex
draws it: right above its composer (or above the follow-ups queued for the turn). The
approval view replaces that line, and it returns the moment the human allows the
command, so a long approved command reads working while it runs. The same words
anywhere else on the screen, in the command awaiting approval or in output, do not
count. The bell also clears on
`PostToolUse`, `Interrupt` (Esc), `Stop` or the next prompt. A screen tmux cannot
read keeps the bell. Tools
restore working state, and end hooks release claims. The
existing fleet pause, resume, attach, stop, reap and worktree operations remain
shared; `fleet stop` types `/exit` and its Enter apart, because Codex reads one
burst of keys as a paste. `fleet restart` (and Restart in asq) resumes the
thread the agent's first hook bound, as `codex <options> resume <id> -- <continue
line>`, on the same account; with no bound thread it starts fresh and says so.
`/clear` keeps the row and moves its claims to the new thread.

Set `agents.mcp = true` to add AISquare's stdio server to launched sessions.
Install the `serve` extra first. This is ephemeral launch configuration;
unrelated servers are retained. The launch identity and AISquare configuration
environment are forwarded explicitly to Codex's MCP child. MCP tools reuse a
hook-bound session for the requested project when available. Until hooks join
(including while awaiting native trust), local clients use a provisional
identity unique to the project and launch token (or fleet token when there is
no launch token). The native join adopts its claims, history and focus, renews
its leases, and retires the provisional presence, including after an idle sweep
ended that provisional row. Late tool calls resolve to
the native row. External clients retain their project-scoped virtual identity.

With both `explainability.enabled` and `explainability.ship` enabled, Codex
exports native OTLP JSON logs to a per-launch, authenticated loopback receiver on
POSIX/WSL. The receiver's bearer token reaches Codex in its launch environment
(`OTEL_EXPORTER_OTLP_LOGS_HEADERS`), never on its command line, which every local
user can read. `-c shell_environment_policy.exclude=[…]` keeps the variable out of
the shells Codex's tools run in, where an app's own OpenTelemetry SDK would prefer it
to the app's `OTEL_EXPORTER_OTLP_HEADERS`. Codex's own exporter still sends it
(measured on 0.162.1). That `-c` replaces the operator's own `exclude`, so the list
also carries the patterns of every config layer the launch may read: system, user,
profile, and the project's `.codex/config.toml`. If the operator sets that variable
themselves, or sets `shell_environment_policy` with their own `-c`, their value is
kept and AISquare's native tracing is off for the launch, which says so. It spools
allowlisted, redacted event metadata. Existing
`asq explainability ship` delivery replays model, tool and decision spans
through the SDK under the same run binding. User-configured native OTEL
exporters in the effective account home, selected profile, system config or
explicit `-c`/`--config` overrides are preserved; AISquare reports that it
has stood down. Unreadable or invalid native config also leaves telemetry
unchanged, with a message naming the affected layer and file. Missing files
are treated as absent. Attached native options such as `-cotel.exporter="none"`,
`-pwork` and `-p=work` are inspected too. Prompts and unselected profiles do not
disable tracing. Use a second `--` to keep a prompt beginning with an option
literal, for example `asq launch coder --agent codex -- exec -- '-pwork'`:
the first terminator belongs to AISquare, the second to Codex.
Codex [ignores `otel` in project-local config](https://learn.chatgpt.com/docs/config-file/config-advanced),
so AISquare does not search ancestor projects or other account homes for
exporters. Usage is counted from native logs once, with the observed provider
name; duplicate exports and the parallel native span stream do not add usage again.
Each queued event's dedup marker commits before the next event is processed,
so a failed batch retains its completed prefix on retry. Spool file writes do
not hold the board's database writer lock. A temporary spool failure returns
a retryable receiver response instead of acknowledging the incomplete batch.
Per-launch deduplication/provider metadata is removed when the receiver exits.
Malformed OTLP batches return 400. Temporary spool failures such as a full disk
return 503 for retry; permanent failures such as a read-only spool fail open with
200. A partial batch checkpoints its queued prefix in one metadata transaction,
without holding the board's writer lock across spool writes. Delivery remains
at least once across receiver crashes or loss of the checkpoint store.

Receivers also prune metadata idle for 24 hours and caches from older releases;
project purge removes that project's native metadata, including unjoined launches.
Queued insight records remain available for delivery. Malformed token counts
are treated as unknown (zero), so they cannot indefinitely block later records.

This transport does not change model routing, API keys or ChatGPT login. It
does not capture raw model bodies or tool arguments. Native timestamps remain
metadata; SDK replay creates spans at delivery time. Do not interpret replay
span duration as native model latency. Exporter or gateway failure leaves the
agent usable; disabled telemetry starts no receiver. Native Windows launches
remain usable without this POSIX receiver; the fleet runs in WSL.

## Validation and extending support

`make check` runs the common suite. The opt-in native fixture uses the installed
Codex binary, temporary config homes and a loopback Responses server:

```sh
AISQUARE_TEST_CODEX=1 .venv/bin/python -m pytest tests/test_codex_native.py
```

The CI Codex job installs the pinned compatible binary and runs
`python -m tests.run_codex_native` on each PR, alongside the existing Claude
and shared regression suite. This runner checks the exact binary version and
requires the native fixture to execute and pass; an all-skipped run fails. The
sandbox test probes Codex's sandbox first (`codex sandbox -- true`). Where bwrap
may not bring up loopback, as on GitHub's runners, it skips and names the
refusal. Any other probe failure fails it.
The ambient CI jobs also export parent-agent identity and a populated
`CODEX_HOME`, which the shared test fixtures must isolate.

It covers native hook context, prompt capture, exec/resume identity, tool and
MCP calls, usage export, interactive fleet startup with `--approve-for-me`, tell
(typed, queued with `codex queue`, and typed before the first hook), the manager's
wake-up nudge, resize, restart with the thread's history, `/clear`, a graceful
stop (Codex's own `SessionEnd` releases the claim), board calls from inside the
sandbox, the bell only for a human's approval, a restart onto `workspace:ask`, and
the effort levels that reach the provider.
Owned test hooks use Codex's automation trust flag only inside this fixture.
No production credentials or paid model calls are used. Common tests also
cover config preservation, mixed agents, a synthetic third adapter, migration,
concurrent callbacks, manager continuation, CI, MCP, redaction and SDK replay.
The live smoke behind [Parity with Claude Code](#parity-with-claude-code) ran
with a signed-in ChatGPT login. API-key logins and delivery to a deployed
gateway have not been run, and the loopback tests do not claim them.

To add another terminal agent, follow [Adding a terminal agent](#adding-a-terminal-agent).

Native contracts (checked against 0.162.1's `codex --help` and `codex features list`):
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

Native OTLP retries use deterministic spool names and durable receipts independent
of the board. A killed receiver or locked board cannot republish an already queued,
claimed, or delivered event within the seven-day receipt retention period. Shipping
still retries network delivery in the existing outbox; this does not promise exactly-once
network delivery if the gateway accepts a request but its acknowledgement is lost.
Malformed members are skipped without discarding valid neighboring records. Permanent
spool failures cost at most one write attempt per request, and `aisquare doctor` checks
spool access and reports the most recent receiver failure.

## Adding a terminal agent

`src/aisquare/core/agent_adapters/` is the one place a terminal agent (Gemini CLI,
Grok, ...) is implemented. The shared services (hooks, board, launch, fleet, doctor)
ask its adapter and never compare its name: `tests/test_no_agent_literals.py` fails on
`== "codex"`, `in (..., "claude-code")` or a `match` case on either name anywhere
outside that package. Keep unsupported capabilities explicit instead of copying
another agent's flags or claiming hookless detection is integration.

### The files

| # | File | What |
|---|------|------|
| 1 | `src/aisquare/core/agent_adapters/<agent>.py` | A class implementing `AgentAdapter` (`types.py`), with its `AgentCapabilities`. |
| 2 | `src/aisquare/core/agent_adapters/__init__.py` | One line: `register(<Agent>Adapter())`. |
| 3 | `tests/fixtures/agent_events/<id>.json` | `events`: a recorded payload for every hook event it installs, with the `AgentEvent` fields it must decode to; `version_output`/`version`; `hooks_off` (the file and text that switch its hooks off). |
| 4 | `tests/test_adapter_contract.py` | Its row in `EXPECTED_PERMISSIONS` (a coder's and a reviewer's flags). |
| 5 | `tests/test_<agent>_native.py`, `tests/run_<agent>_native.py` | The opt-in test against the real binary (`AISQUARE_TEST_<AGENT>=1`), as `test_codex_native.py` and its CI job do for Codex. |
| 6 | `docs/coding-agents.md` | Its setup and native settings. |

Nothing else, and no new `aisquare hook` subcommand: a `HookSpec` whose command is
`types.GENERIC_HOOK` installs `aisquare hook event --agent <id> --event <native event>`
(plus `--config-dir <home>` with `hook_names_home`), which decodes with the adapter.
Optional slots: numbered login slots (`accounts`, an `AgentAccounts`), the plugin route
(`plugin`, an `AgentPlugin`), and native telemetry (`capabilities.native_telemetry` with
`telemetry_args`; the receiver in `services/native_telemetry.py` reads Codex's OTel
attributes and config layers, which a second telemetry agent extends there).

### The protocol

| Slot | What it answers |
|------|-----------------|
| `id`, `label`, `binary`, `install_hint` | The board's and config's name; what the UI says; the executable (`adapter_for_binary` knows a wrapper by this name). |
| `home_env`, `home_name`, `config_dir(home, env, explicit)` | The config dir the binary reads: explicit, else `$home_env`, else `~/home_name`. |
| `version_args` | The arguments that print its version; `types.version_in` reads the output. |
| `settings_name`, `capabilities.hooks` | The hooks file in that dir, and each event installed with its subcommand, timeout and matcher. `core.agents.install_hooks`/`remove_hooks` merge it, keeping every foreign entry. |
| `hooks_off_setting`, `hooks_switch(home, settings, args)` | The setting that silences every hook, and whether the dir or the launch arguments set it. |
| `plugin` | The aisquare plugin route, or `None`. |
| `decode_event(payload)` | The payload as an `AgentEvent` of one kind: `session_start`, `prompt`, `stop`, `session_end`, `interrupt`, `attention`, `tool_done`, `turn_failed`, `notification`; `None` for one AISquare ignores. |
| `encode_reply(kind, reply)` | What the hook prints: context for `session_start`/`prompt`, a "keep going" decision for `stop`, `""` for nothing. |
| `model_args`, `resolve_model`, `native_args`, `native_choice`, `validate`, `effort_alias` | Model and reasoning effort: the flags, the configured choice, the native spelling, what a native argv already chose. |
| `permission_args(intent, role, native)` | The flags for a `PermissionIntent` (below), less what the `native` arguments already settle, with `store_access` for its write scope; `ValueError` for one it cannot express. `fleet.roles.<role>.permission` and `--permission-mode WRITE:APPROVALS` reach every agent through it (`services.fleet.permission_flags`). |
| `store_access(write, store)` | How a session sandboxed at `write` reaches the aisquare store in `store` (`types.store_dir()`), with `types.store_read_only(store)` read-only within it, made first by `types.prepare_store` ([the sandbox and the store](#the-sandbox-and-the-aisquare-store)). `[]` without `sandboxes_tools` and for a scope run unsandboxed. |
| `fleet_args(role, label, mode, ...)` | A fleet spawn's flags from the role's native permission settings, then `label_args(label)`. |
| `resume_args(native_id, transcript)` | The arguments that resume a session, put first: `fleet restart`, `fleet switch` and Restart resume the native id the first hook bound (Codex: `codex <options> resume <id> -- <continue line>`). |
| `exit_command` | What `fleet stop` types to end the session so its `SessionEnd` runs. |
| `queue_args(native_id, message)` | The arguments that queue a message for a RUNNING session's next turn (Codex: `queue --thread <id> --message <text>`), run with `home_env` set to the session's dir: what `fleet tell` does for a busy agent. `None` for none (a board note). |
| `key_spellings` | tmux key names the agent reads under another one, which the fleet UI's pane sends instead (Codex: `S-Enter` → `C-j`, its newline). |
| `role_defaults(role, binary, args)` | The flags a role needs on this agent wherever it starts (Claude Code's ui-tester `--chrome`), less what `args` says. |
| `browser_tools(home)` | The browser the agent brings itself, as the doctor's browser-tools line names it (Codex's `browser_use` feature and bundled browser plugin); `None` where the doctor finds it another way. |
| `mcp_args`, `context_files`, `disable_native_teams` | AISquare's MCP server for this session; the instruction files it reads; the arguments and environment that keep its own sub-agent teams off. |
| `telemetry_args(endpoint, keep_out=)` | Its OpenTelemetry exporter, with `native_telemetry`. The receiver's `Authorization` is never an argument: it reaches the agent in `OTEL_EXPORTER_OTLP_LOGS_HEADERS`, and `keep_out` (the variable, with the operator's own patterns) is what the agent keeps out of its tools' shells (Codex: `shell_environment_policy.exclude`). |

`AgentCapabilities` holds the rest as flags, each documented in `types.py` with the
service that reads it: `pid_env` (the variable that hands a hook the agent's own pid,
Claude Code's `CLAUDE_PID`) or `tool_shell_env` (the variable the agent sets in its
tools' shells and never in its own hooks, Codex's `CODEX_THREAD_ID`) — how a fleet row
tells its agent's own process from a nested child, across a `/clear` or a `/resume` —
`assigns_session_id` (AISquare names the session at launch),
`requires_hook_trust`, `hook_names_home`, `strict_hooks_file`, `stop_reason_as_prompt`,
`limits_in_transcript` (no turn-failed event: the accounts slot's `limit_from_transcript` and `failed_turn` read it), `ambient_first_run`,
`reads_pane_readiness` (`fleet tell` may type into a pane that looks idle),
`idle_prompt_mark` (what the last line of the screen starts with at an empty prompt,
Codex's `? for shortcuts`: where a pane its hooks say nothing fresh about is typed into,
and what a pane whose hooks say waiting, or one `fleet stop` would `/exit`, must show too),
`idle_on_screen` (the agent's own reading of that empty prompt, for a layout without the
mark: Codex under `--no-alt-screen`, its empty composer with only its footer below),
`fleet_settings` (`-c` settings a fleet window's agent launches with unless the
operator's config sets the key: Codex's `notice.hide_rate_limit_model_nudge=true`),
`starts_at_its_prompt` (its `SessionStart` fires once it is at rest at its prompt,
Claude Code's), `start_dialog_mark` (what its screen shows at a dialog before its start,
where an Enter answers it: Claude Code's `Enter to confirm`, which keeps a prompt out
and stops the wait for its start), `resumes_across_homes`, `exports_home_env`, `own_doctor_check`, `model_ladders`,
`model_proxy`, `positional_prompt`, `first_context_file_only`, `sandbox_permissions`,
`sandboxes_tools` (its tools run in an OS sandbox, which `store_access` must open to
the store), `redraws_while_asking` (its UI redraws while it waits on the human, so an
attention row holds until a hook moves it or `working_on_screen` reads a turn running),
`working_on_screen` (whether its screen shows a turn running unblocked: Codex's status
line, right above its composer), `legacy_fleet_args`, `value_options`,
`switch_options`.

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

The store profile is [the sandbox and the store](#the-sandbox-and-the-aisquare-store)'s.

### What the conformance suite fails on

`tests/test_adapter_contract.py` runs every case for every registered adapter and for
`tests/fake_agent.py`, a synthetic third agent registered only there. It fails when:

- the fixture file or the `EXPECTED_PERMISSIONS` row is missing;
- an installed event has no recorded payload, or decodes to another kind or other fields;
- the adapters together no longer decode every event kind;
- installing twice doubles a hook, removing leaves the file other than it was before,
  a foreign entry is lost, or the installed command is not `hook_args`'s;
- the fixture's hooks-off switch does not make a complete install read as not connected;
- the installed command, run as `aisquare hook ...` with the recorded payloads, does not
  put the session on the board under this agent and then mark it `waiting`;
- a reply prints something for nothing, drops the context, or makes a Stop reply the
  bare reason rather than a decision;
- `version_in` misreads its version line, or `config_dir` ignores `home_env` or an
  explicit dir;
- an agent that sandboxes its tools (`sandboxes_tools`) has a write scope whose
  `store_access` does not name the store (a Windows home included, as the agent
  spells it), or leaves out a path `types.store_read_only` keeps read-only; a permission the fleet maps for it,
  a role's built-in one included, does not carry its `store_access`; or an agent
  without a sandbox declares any;
- an intent maps to neither flags nor a refusal with a reason; resume arguments are
  empty; the exit command is not a slash command; the MCP arguments do not name the
  server; nothing keeps native teams off; telemetry arguments come without the
  capability.

The fake adapter was written from this section alone. Besides the cases above it is
spawned through `services.fleet.spawn` with a stub binary, its window's `aisquare
launch` builds its argv, and `fleet stop` types its `/quit`, with no change to any
shared service.
