# Terminal coding agents

AISquare supports Claude Code and Codex through a coding-agent registry. Memory,
the task board, claims, manager continuation, CI delivery, tmux, worktrees and the
terminal UI are shared. An adapter supplies native hooks, config locations,
model options, permission options and MCP configuration.

Codex compatibility is tested against **CLI 0.164.0**, which CI installs, and
**0.162.1**, whose live suite passes too. Use one of these or a newer compatible
release; older releases may lack the required hooks and flags
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
review again. AISquare never writes Codex's hook trust records or automatically
adds a trust-bypass flag. (A folder's trust is another record: one a human gave on
one Codex account is copied to the others, [Codex accounts](#codex-accounts).)
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
- **The spawn dialog** (`＋ spawn agent`) is `fleet spawn` with the role, agent
  (blank: the role's, which the field names), account (the launching agent's slots
  only; an account preset or picked brings its agent, since slots are one ladder)
  and permission (the one `WRITE:APPROVALS` for every agent, or Claude Code's
  `--permission-mode`, or Codex's `--sandbox` and `--approval`, as the agent's
  `sandbox_permissions` says), beside the persona, presets and hand-off, and shows
  the flags the launch will pass. It calls the same `services.fleet.spawn`. Its
  target picker lists every agent's slots, reads a bind's account from its agent's
  home variable, and *+ New bind* defaults the binary to the chosen agent's and
  sets that agent's home (`claude_accounts.launch_env`).

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
evidence those rows. *Smoke N* is the first smoke's item N. The smoke was run again after
the fixes, each time on the same real binaries with loopback providers: SMOKE2 at
`81da2b7a` (7 of 8), SMOKE3 at `1398cd07` (25 of 28), SMOKE4 at `769e35a8` (18 of 19: the
limit, the model dialog and tampered launch records), SMOKE5 at `30af973f` (17 of 20: the
store and the broker), SMOKE6 at `8b33c942` (36 of 37), whose one failure, a root the
sandbox may write inside an install, was fixed in `8dd8a14f`, and SMOKE7 at `7e7e152e` (28 of
28: Remote on a sandboxed Codex, its accounts, and the fleet and board with Codex, after the
0.8.1 lanes). *SMOKEn (item)* is that run's item.

Codex 0.164.0 was then measured the same way against 0.162.1: the full live
suite (`tests/test_codex_native.py`, 16 cases) and the opt-in live captain, persona and
broker cases pass on both; its hook payloads, the dialogs and prompts the adapter reads
off its screen, the tools its requests offer and `codex features list` for every feature
the adapter switches hold as on 0.162.1, and `check_for_update_on_startup=false` still
keeps its update dialog shut. Its rollout adds fields the reader does not read. So
"Codex 0.162.1, as observed" below holds for 0.164.0 too.

The rows from *Persona in the system prompt* on are main's 0.8.2 features: each states the
agent's adapter answer (the slots in [the protocol](#the-protocol)), measured on 0.162.1
through the adapter's own arguments with a loopback provider, which the launch, persona,
fleet, captain and asq code asks as #113's 0.8.2 lanes wire it in.

| Feature | Claude Code | Codex 0.162.1, as observed | Evidence |
|---------|-------------|----------------------------|----------|
| Install and connect | `install.sh` installs Claude Code, `init` writes its hooks into `settings.json`, and they run at once. | The same commands with `--agent codex`, and the hooks go into `CODEX_HOME/hooks.json`. **Differs:** Codex runs no hook until you trust it in `/hooks`. `agents status codex` reads `unverified` until then and `observed` after. | Smoke 1; SMOKE6 (1): the hooks as `<python> -E -P -m aisquare hook codex` |
| Welcome: a manager and two coders | Step 2 is Claude Code; step 3 starts the fleet. | The same on Codex: step 2 shows its version, the manager's account signed in, and connected. It reads the hooks in the home of the account the manager launches on (`~/.aisquare/codex-accounts/<n>` when a slot is the default), and Connect installs them there. **Differs:** on its first start, Codex asks in the manager's pane to trust the folder and review the eight hooks. | Smoke 2. Account home: `4df3fd44`; SMOKE6 (2): step 2 named the slot's home, and Connect wrote its hooks |
| Context at session start | `SessionStart` hook context | Same: the Codex manager quoted its "You are team session … (role: manager)" line. | Smoke 3; live hooks; SMOKE6 (3) |
| Prompt capture (`aisquare log`) | `UserPromptSubmit` | Same, with `source=codex`. | Smoke 4; live hooks; SMOKE6 (4) |
| ▶ working / ⏸ waiting | From its hooks; a fresh agent is waiting from its `SessionStart`, which fires once it sits at its prompt | Same, from Codex's hooks. | Smoke 5. Claude Code's fresh agent: SMOKE2 (it read working until a first prompt), fixed in `c29a2670`; SMOKE6 (5) |
| 🔔 attention | `Notification`: a permission prompt or a question | 🔔 for an approval the human answers ("Codex needs your approval") and for `request_user_input`. None for one Codex's automatic reviewer decides under `--approve-for-me`. **Differs:** Codex fires `PermissionRequest` with the same payload for both, so the reviewer is read from the session's rollout. Codex also redraws its approval view every second, so pane activity does not clear the bell. It clears when Codex's working line (`esc to interrupt`) is back on the screen, which is the moment the human allows the command, or on Esc, `Stop` or the next prompt. | Smoke 5 failed both ways (seven false bells for one task; a real approval read working). Fixed in `3b11964c`, `182f70e7` and `467d4ef4` (review r2). Live sandbox: a reviewed escalation rang no bell, attention held through the redraws, and the row read working within 5 s of `y`, while the approved 8 s command still ran. SMOKE2 (5) and SMOKE6 (5): no bell under `--approve-for-me`; under `workspace:ask` the bell held 11 s and 15 s unanswered, then read working within 0.5 s of `y` |
| Remote: needs-you cards | From the session JSONL: a pending tool with the bell is a permission card, `AskUserQuestion` a question, `ExitPlanMode` a plan, an Esc *interrupted*, a turn that ends on a question *asked*. | Same, from its rollout: an approval the human answers (the call without its result, and the bell) is a permission card naming `exec_command` and its command; `request_user_input` (Plan mode) a question card; `turn_aborted` an *interrupted* card; a turn's last words ending on a question an *asked* card. A limited agent's push waits 90 s for a hand-over only when another Codex account can take it, and a rollout past the tail's 512 KiB reads as what its last turn did, never as a tool pending for good. **Differs:** a dialog of Codex's own that no hook reports is a card read off its screen (`dialog_on_screen`): its folder-trust question in a git checkout no trust covers and its hook review before its first prompt, *Switch to gpt-6-luna for lower credit usage?* after a turn's `Stop`, and *Implement this plan?* after a Plan-mode turn's. The card says "<name> shows a dialog that needs you" and quotes the dialog's words, with no quick answers (Enter trusts the folder and Esc quits Codex there; a digit on the hook review only moves the highlight), so the key pad answers it; it is pushed 30 s after the scan first saw it, as Claude Code's folder-trust card is after its start. While it is up it takes the place of an *asked*, *interrupted* or *failed* card, whose words and Enter would answer it. So a plan proposed in Plan mode is no *plan* card, as its *Implement this plan?* dialog leaves no record: it is that dialog's card, the plan in the transcript above it. | Live: `test_real_codex_approval_and_question_are_answered_from_the_remote_page`; `test_real_codex_dialogs_no_hook_reports_are_cards_on_the_phone` (folder trust, hook review and *Switch to gpt-6-luna?* in a fleet window, each a card on Remote served on loopback); *Implement this plan?* measured in a fleet window on 0.162.1. Unit: `tests/test_remote_needs_codex.py`, over a rollout measured on 0.162.1 (`tests/fixtures/agent_transcripts/`); `tests/test_remote_dialogs_codex.py`, over screens measured on 0.162.1 (`tests/fixtures/agent_screens/`). SMOKE7 (1, 2, 3, 4, 12) |
| Remote: quick answers | The dialog's digits and Esc | The same keys, measured on 0.162.1: `1` approves (*Yes, proceed*), `2` is *Yes, and don't ask again*, Esc is *No* and stops the turn; a question's options take their digit (picked and sent), and Esc stops the turn. **Differs:** Codex records a call before its hooks run and draws its view only after them, so a tap that comes first is refused ("shows no prompt now") until the view is on screen (`working_on_screen`/`idle_on_screen` read it). | Live: the question's first tap was held, and the retry picked Blue. Unit: the hold through the answer route. SMOKE7 (1, 2, 3) |
| Remote: Transcript tab | The session JSONL, under `* claude` | The rollout, under `* codex`: the human's words, the agent's, each call as one line, a question in full, an interruption and a proposed plan; Codex's own context (the tags it writes, measured: `<environment_context>`, `<turn_aborted>`, `<user_action>`, `<recommended_plugins>`; AGENTS.md) is left out, and a prompt that opens with another tag (`<task>…</task>`) is the human's. Load older reads at most 2 MB a page, as for Claude Code. | Live; unit over the measured rollout, padded with 3 MB of output. SMOKE7 (8) |
| Remote: Tell, Stop, Restart and Switch from the phone | Typed, or refused while a dialog may be up, by the board, the transcript and the pane's quiet. | **Differs:** decided by Codex's own screen. A quiet pane at its empty prompt (`? for shortcuts`, or `esc again to edit previous message` after one Esc) is at rest. Anything else drawn over that prompt is a dialog: the *Switch to gpt-6-luna?* dialog Codex opens after a turn's `Stop`, which no hook reports, its folder-trust, hook-review and approval views, its transcript after a second Esc (where Enter rewinds), a draft. Prompt and Interrupt & tell refuse it before any key; Stop, Restart and Switch refuse it, or press Esc first with `dismiss_dialog`. A fresh Codex, which has fired no hook, takes a Prompt tell at its empty prompt. Interrupt & tell presses no Esc while the screen shows the empty prompt with no turn on it: after a pad Esc there, its Esc was the second, which opens the transcript. | Live remote: `test_real_codex_takes_a_phones_prompt_tell_at_its_empty_prompt_and_none_at_its_dialog`, through Remote served on loopback: the fresh agent's line reached the provider as its first prompt, and at the dialog Prompt and Interrupt & tell were 409 `dialog_open`, the dialog left unanswered. `test_real_codex_after_a_pad_esc_takes_interrupt_and_tell_and_opens_no_transcript`: a pad Esc after a turn, then Interrupt & tell, whose line reached the provider with no transcript open. Unit: `tests/test_remote_codex.py`. SMOKE7 (5, 6, 11) |
| Remote: key pad | ⇧⏎ (More) goes as `S-Enter`, its newline (`C-j` on a tmux below 3.5). | **Differs:** ⇧⏎ goes as `C-j`, Codex's newline; `S-Enter` submits. One Ctrl-C or Ctrl-D at its empty prompt exits Codex (Ctrl-D with a turn running too; a Ctrl-C there opens its *Cancel task* / *Exit* choice), where Claude Code says *Press Ctrl-C again to exit*, and the same of Ctrl-D: `send-keys` asks first (409 `one_press_exit`) while Codex's screen shows that prompt or cannot be read, as it asks before any agent's second press, and the page asks again in the machine's words. Over a draft neither exits it, and the key goes. Two Esc open its transcript, where Enter rewinds. | Measured on 0.162.1 under tmux 3.7c, Claude Code on 2.1.304. Unit: `tests/test_remote_codex.py`; live: a single `C-c` from the API to an idle fleet Codex is refused and Codex runs on (`tests/test_codex_native.py`). SMOKE7 (7) |
| Remote: Card tab (explainability) | Judged by the doctor's checks, the proxy's among them. | **Differs:** judged by its own telemetry's lane, which needs insight shipping on and POSIX, never by the proxy, which does not carry its traffic. The card names it (`policy.source`). | Unit: `tests/test_remote_codex.py`. SMOKE7 (9) |
| ⏳ limited and `on_limit = "switch"` | The `StopFailure` hook. Claude Code's own wait-and-continue runs at the reset. | **Differs:** Codex has no `StopFailure`, and 0.162.1 fires no `Stop` for a turn that ends on its limit. The limit is read off the rollout at `Stop` and `Interrupt`, and by the fleet's read of a row its board says is working; nothing continues at the reset. With the operator's `[notice] hide_rate_limit_model_nudge = false`, Codex draws *Switch to gpt-6-luna?* under the limit's error. The automatic hand-over presses Esc there (its `dismiss_key`, which keeps the model), then `/exit`, so Codex's `SessionEnd` ends the session it leaves and `doctor` lists no agent parked. Not fixed: a stop, switch or restart by hand kills the window at that dialog, the session it leaves limited until the reset (Remote's Switch with `dismiss_dialog` ends it); a hand-over whose stop had to kill (a draft on screen) leaves the old session unended. | Live: a team plan at its spend cap (SMOKE3) and a loopback provider answering the same `429` (`tests/test_codex_native.py`). SMOKE4 (limit-ls, on_limit=switch), SMOKE5 (limit) and SMOKE6 (limit): one limited event at the provider's reset, then the hand-over to the next slot with the recorded flags. SMOKE7 saw the hand-over kill the window at that dialog, the old session never ended and `doctor` warning until the reset. Fixed: `test_real_codex_at_its_switch_dialog_is_handed_over_and_the_session_it_leaves_ends` (live, 0.162.1); unit `tests/test_codex_handover_dialog.py` |
| Manager continuation | The `Stop` hook's "keep going" decision | Same: "Blocked by hook: 1 board update(s)…", and the manager went on. | Smoke 6 |
| Waking a waiting manager | The nudge is typed into its prompt. | Same. Every line the fleet types is now a bracketed paste with its Enter 0.2 s later. **Differs:** Codex reads a burst of keys as a paste and keeps an Enter inside it as text. A manager whose hooks went stale is nudged only when its pane shows the empty prompt. A board write made from Codex's sandbox is run by the agent's broker outside it, which sends the nudge; the fleet's tmux stays out of the sandbox's reach. Where tmux refuses the nudge all the same, it is left owed, and the agent's own `PostToolUse` hook, which Codex runs outside the sandbox, sends it as the command ends. | Smoke 6 failed (three nudges left unsent in the composer). Fixed in `6ee17ee3`. Live panes: a waiting manager runs the nudge a coder's task review sends. SMOKE2 6 failed: a sandboxed result, verdict or block never woke the manager, Codex's or Claude Code's. Fixed in `43237797`. Live sandbox: a sandboxed `--kind result` wakes the waiting Codex manager, and `tmux` from the sandbox is still refused. SMOKE5 (wake) and SMOKE6 (6): a sandboxed result woke the Codex manager in 0.65 s and 0.75 s, a verdict in 0.64 s |
| `fleet tell` | Typed when it is waiting, else a board note | Typed when its hooks say waiting. **Differs:** a busy agent's message is queued with `codex queue --message=<text>` and runs as its next turn (`✓ queued for its next turn`), even a message that starts with `-`. Claude Code has no queue. Before its first prompt (no hook yet), the message is typed when the pane is quiet and shows `? for shortcuts`, and after it only when the pane shows it too: at the folder-trust or hook-review dialog, which is as quiet, at the rate-limit model dialog Codex opens after its `Stop` (*Switch to gpt-6-luna?*), or with a draft in its composer, it is a board note, so the dialog and the draft stay the human's. A fleet launch hides that model dialog (`-c notice.hide_rate_limit_model_nudge=true`) unless the operator's config sets the key, and `fleet stop` types no `/exit` into a dialog: it kills the window and says why. | Smoke 7 (typed, then queued; a never-prompted agent got only a note). That case was fixed in `47c9f23b`. Live panes: "reply ok" told before any hook ran, and the hook review was left alone. Live hooks: a queued `--no-verify …` ran as the next turn (`fc860457`, review r2). SMOKE4, SMOKE5 and SMOKE6 (modal): with the flag, no *Switch to* dialog; with the operator's setting it opens, `tell` files a note, `stop` kills the window with no `/exit`. SMOKE6 (7) |
| `fleet stop` | Types `/exit`; `SessionEnd` releases the claims. | Same: `/exit`, with its Enter apart, and exit 0. Codex's `SessionEnd` ended the session 0.5 s before the fleet's `agent_exited`. | Smoke 8; live hooks; SMOKE6 (8): its broker's directory removed |
| `fleet restart`, Restart | `claude --resume <transcript>` | Same outcome: `codex <options> resume <thread> -- <continue line>`. It quoted its question and answer from before the restart, and its claim was kept. | Smoke 9; live hooks; SMOKE5 (restart), SMOKE6 (9): the same thread, a new broker, the claim kept |
| `/clear` | Row and claims move to the new session. | Same: the row and the claim moved, and the new thread knew its claim. **Differs:** Codex fires no `SessionEnd` for the thread it leaves; the new thread's `SessionStart` (clear) comes with its first prompt. | Smoke 10; live hooks; SMOKE5 (/clear), SMOKE6 (10): the broker takes the new id and refuses the old |
| A session a tool starts | `CLAUDE_PID` tells the agent's own process from a child it starts. | `CODEX_THREAD_ID`, which Codex sets only in its tools' shells, does the same. A session of another agent than the row's never takes the row, such as a `codex exec` from a Claude Code agent's Bash tool. | Measured on 0.162.1; unit tests (`5898ce7e`, review r2) |
| Accounts and usage | Numbered slots; usage from the endpoint Claude Code's `/usage` reads | The same slots, on one ladder (`~/.aisquare/codex-accounts/<n>`), with doctor rows. **Differs:** usage is read offline from the account's own rollouts (49 % of 5 h and 8 % of the week in the smoke). It is only as fresh as the last turn, and has no *to the limit* trend. | Smoke 11; SMOKE6 (11) |
| Folder trust across accounts | `[accounts] share_trust`: a launch copies the folder's entry in `.claude.json` from the account that has it; `accounts trust [--sync]` | Same, among the Codex slots, in each slot's `config.toml`: the `[projects."<root>"]` table of `trust_level = "trusted"` is appended, every other byte kept, and the spawn's receipt says so. **Differs:** Codex asks only in a git checkout, and its yes records the repository root (a worktree's is the main checkout's). The folder's own entry decides first, then the root's, and nothing above them. A table that says `untrusted` is that account's no: a share never overrides it, and `accounts trust` reports it. A Claude account's yes is not a Codex account's, nor the reverse. | Measured on 0.162.1 in scratch homes (`core.codex_sharing`). Live: `test_real_codex_trust_one_slot_gave_reaches_another_and_its_spawn_asks_nothing` (slot A's yes in Codex's own dialog, a spawn on slot B with no question and a told line run; slot C with sharing off meets the question). SMOKE7 (13) |
| *Log in* on an account row | Waits for a different stored login than the expired or rejected one on disk | Same: `codex login` in the slot, and the pane waits for `codex login` to write `auth.json` anew (a digest of the file, never a value read out of it), not the login already there. | Unit and UI tests (`tests/test_codex_sharing.py`, `tests/test_ui_codex_accounts.py`); not run live, which needs a real login. SMOKE7 (15): on a dummy API-key home, the pane closed 2 s after a new `auth.json` appeared |
| The ⚙ Settings status line | `● 2× claude`: the room left across the Claude accounts | `● 1× codex` on a line of its own under it, from Codex's offline readings, with its own ⚠ for a slot that needs a login. | UI tests (`tests/test_ui_settings_hub.py`). SMOKE7 (16) |
| `fleet switch` to another account | Resumes the session on the new account. | **Differs:** a Codex thread lives under its `CODEX_HOME`, so the agent starts fresh with the hand-off prompt (which got through the new home's folder-trust dialog) and its claim moves. A switch onto the same home resumes. | Smoke 12; SMOKE6 (12): the hand-off waited behind the new home's hook-review dialog and was sent once |
| Claim, review, done | Board tasks | Same: a Codex coder claimed a task and sent it to review, and a Codex tester marked it done. | Smoke 13; SMOKE5 (round trip), SMOKE6 (13): all through brokers |
| Board calls from inside the agent | No sandbox: `aisquare` runs as the user. | Same since `e8436fa3`: a sandboxed fleet Codex runs under the fleet's `aisquare` permission profile, and its board calls succeed on the first, sandboxed attempt, through its launch's broker. **Differs:** Codex sandboxes its tools. The whole aisquare store is read-only to it, and its board calls run outside the sandbox from the broker's whitelist, as its own sessions ([the broker's whitelist](#the-brokers-whitelist)). A restart, switch, hand-over or `tell` queue runs from the row's launch record only, written by its spawner and its own hook where the sandbox cannot write: binary, arguments, permission, role, agent, directory, account, transcript and thread; a row without one, or a project whose directory in the store is not the record's, is refused. What is typed into a pane or kills a window goes to the pane the record places (socket and pane), never the row's, and a failed turn's limit is read from the record's transcript. A pick in its own `/permissions`, or a `codex` started by hand, uses its own sandbox ([the sandbox and the store](#the-sandbox-and-the-aisquare-store)). | Filed by the smoke: every call needed an escalation. Live sandbox: a note and a task claim succeeded sandboxed, through the broker; a direct write to `context.db` and an `--as` of another session were refused (review r9 of #113). Writes to the read-only paths were refused, and the launch record held (`ca6d43ea`, `8337e88c`, review r2; review r3; one launch record, review r4: a sandboxed reviewer's rewrites of its thread, home, role, binary and account replayed as recorded, the project's directory refused; review r5: a row pointed at another pane or transcript is refused or ignored). SMOKE4 (seal restart, seal switch, seal hand-over, tell queue, pane): tampered rows replayed as recorded, the marker never ran. SMOKE5 and SMOKE6 (store, broker writes, identity, malformed, after exit, fleet ops, refusals recorded, seals, launch inputs): the store read-only, 30 and 31 protocol calls on their first try, every refusal logged |
| Who answers approvals (`ask` / `auto` / `never`) | `--permission-mode manual` / `auto` / `dontAsk` | Same intent. `ask` puts Codex's "Would you like to run the following command?" in the pane, and `auto` is `--approve-for-me`. **Differs:** the reviewer is pinned on every spawn, restart and resume (`approvals_reviewer`), because a resumed thread otherwise keeps its own. `full:auto` is refused. | Filed by the smoke: a restart onto `ask` kept the automatic reviewer. Fixed in `e8436fa3`. Live sandbox: `fleet restart --permission-mode workspace:ask` brought the prompt |
| Read-only reviewer | `--disallowedTools=Edit Write NotebookEdit`; its shell can still write | **Differs, and is stricter:** the store profile over `:read-only` with Codex's reviewer (`read_only:auto`). The worktree is read-only to the sandbox, and its verdict still reaches the board through its broker. | Live sandbox: the reviewer posted its verdict. SMOKE2 and SMOKE6 (reviewer seat): its worktree write got EROFS |
| MCP (`agents.mcp = true`) | AISquare's stdio server per launch | Same: Codex called `aisquare.team_board` and `aisquare.note_add`, and the note landed on the hook-bound row. | Smoke 14; live hooks; SMOKE6 (14) |
| Explainability | Model traffic through the explainability proxy | **Differs:** Codex exports its own OTLP logs to a per-launch loopback receiver, which the SDK replays. The smoke spooled 58 native events: token counts and tool names, no tool arguments. The receiver's token reaches Codex in its environment, never its command line, and is kept out of the shells its tools run in. There is no gateway for OpenAI Responses traffic. | Smoke 15. Token: `6efabab1`, where a scan of every `/proc/*/cmdline` found it 0 times, and `63d61be4` (review r2). Live hooks: the native events spool, and a tool's shell read the variable empty. SMOKE2 (token), SMOKE6 (15): the bearer in 0 of ~600 command lines, the receiver started with `-E -P` |
| A team on both agents | Claude Code manager | Codex coder: each quoted the other's note from context, and a Codex `--kind result` woke the Claude Code manager. | Smoke 16; SMOKE5 (mixed), SMOKE6 (16) |
| Upgrade and uninstall | Re-connects, or removes, the hooks. | The same for every Codex home and account home. `uninstall` names the `codex mcp remove` / `codex plugin remove` commands. **Differs:** a changed hook definition needs a new `/hooks` review. | Smoke 17; SMOKE6 (17), and (legacy): a real upgrade from `main` placed its running agents by the census |
| Doctor | Claude Code rows | Same on a Codex-only home with no `claude` on PATH: exit 0, Codex `observed`. | Smoke 18; SMOKE6 (18) |
| Model and effort per role | Its ladder, with an availability probe | **Differs:** pins in `[agents.models.codex]` and no AISquare probe. Levels pass unchanged, and `ultracode` becomes `max`. | Live hooks: each level reached the provider |
| The ui-tester's browser | `--chrome` | **Differs:** no `--chrome`. The doctor names Codex's own `browser_use` feature and browser plugin. | Unit tests; not exercised live |
| shift+Enter in asq's pane | A newline | **Differs:** sent as ctrl+J, Codex's newline (Codex submits on shift+Enter), over the tmux control-mode client as much as polled. | Measured on 0.162.1; unit tests; headless on a real tmux control client (`tests/test_terminal_control.py`: `C-j` for Codex, `S-Enter` for Claude Code on tmux 3.5+) |
| The Spawn dialog and the target picker | Role, account, permission mode | The Agent field filters the accounts to that agent's and shows its own permission controls; the picker lists Codex slots, and a bind on one sets `CODEX_HOME`. | UI tests (`tests/test_ui_spawn.py`, `tests/test_ui_target_picker_agents.py`) |
| `account · %` on the info line and `#slot·%` on the sidebar | The Claude account's five-hour window | Same, from the Codex account's own reading (the rollout's primary window), beside the Claude rows' in one map: slots are one ladder. The effort beside the model is the one its prompt and stop read off the rollout, recorded on the session as each turn reports it. | UI tests over a recorded `rate_limits` reading (`tests/test_agent_info_line.py`); hook test (`tests/test_codex_fleet_parity.py`); live on 0.162.1 in asq: `account 2 · 42%  local-fixture high`, `#2·42%` |
| Native sub-agent teams off | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0` | `-c agents.enabled=false` | The launch line on a real Codex manager |
| Codex's update dialog | None | **Differs:** an npm-installed 0.162.1 opens *Update available · 0.162.1 → 0.163.0* in a starting pane once a newer release is out, its first choice, which an Enter picks, running `npm install -g @openai/codex`. A fleet window's Codex launches with `-c check_for_update_on_startup=false` unless the operator's config, `-c` or `-p` profile, or the administrator's `/etc/codex/managed_config.toml` sets the key; the window's launch says when the managed one does. | CI on `72a88fc0` met it in the Codex native job; fixed in `1fdc8165`, green since. SMOKE6 (update dialog): all 9 fleet command lines carry the flag; a control without it shows the dialog; with the operator's setting it opens, `tell` is not typed and the stub `npm` never ran |
| A project on `PYTHONPATH` (direnv) | Its hooks run in its environment, as its tools do. | The agent keeps `PYTHONPATH` for its own tools; every Python aisquare starts for it ignores it (`-E`): its hooks, its broker, the window's launch, the hand-over. | Live: `test_real_codex_in_a_direnv_project_keeps_its_pythonpath_and_aisquare_imports_none_of_it` (a control without `-E` on the window's launch fails it). SMOKE6 (PYTHONPATH): the agent's tool imported the project's package; no aisquare process, nor a restart, imported the tree's `sitecustomize` |
| Plugin route | `claude plugin` | Same: `codex plugin add aisquare@aisquare-cli`. It stands down while `hooks.json` runs aisquare's hooks ([the Codex plugin](codex-plugin.md)). | Real Codex 0.162.1 with a local marketplace; not in the smoke |
| Persona in the system prompt | `--append-system-prompt-file <block>`, passed again at every launch | **Differs:** `-c developer_instructions=…`, the operator's own value from its config layers first, as the `-c` replaces it. A resumed thread keeps its first launch's value and ignores a new one, so a persona attached since reaches it through the briefing alone (`resume_keeps_system_prompt`). | Live through `system_prompt_args` on 0.162.1 (M1: after the 16,979-character base prompt, before the skills list; M3; M4). Wired into `launch --persona` (`tests/test_launch.py`); live, a fleet Codex coder spawned with `--persona` has the operator's value, then the block, in its developer message (`tests/test_persona_briefing.py`, opt-in) |
| Persona skills: export and `import --list` | `<config dir>/skills`, `<repo>/.claude/skills` | `~/.agents/skills`, which every Codex home reads, and `$CODEX_HOME/skills`; `<repo>/.agents/skills` and `<repo>/.codex/skills`. | Measured on 0.162.1 (M5): the request's skills list named one from each. Wired into `persona export --skill --agent`, `import --list` and the Export dialog (`tests/test_persona_cli.py`, `tests/test_ui_personas.py`) |
| Import through an LLM (the manager engine) | `claude -p --json-schema … --no-session-persistence`, the envelope's `structured_output` | `codex exec --ephemeral --skip-git-repo-check --sandbox read-only -c features.hooks=false <no tools> --output-schema <schema> -o <file> -`, the draft read from the file, `<no tools>` being the captain's `--disable <feature>`… and `-c web_search="disabled"` with `-c mcp_servers.<name>.enabled=false` for each server the home's config layers name. No rollout is written, a slot's trusted hooks fire none, and no tool runs on the untrusted source. | Live through `headless_call` on 0.162.1 (M6, M7: the hooks fired without `features.hooks=false`). On 0.162.1 and 0.164.0 with an MCP server in the home's config, the request offered the shell, the multi-agent and goal tools, `web_search` and that server's tool before the switches, and `request_user_input` alone (M11) after. Wired into `persona import --engine manager` with the manager bound to Codex (`tests/test_persona_import.py`, a stub `codex`); live, the request's `text.format` was the strict schema and the draft came from `-o` |
| A hand-off's Fork | `--resume <transcript> --fork-session`, with a new `--session-id` | `codex fork <id>`: a new thread carrying the source's turns, named by its first hook, within its own home. Onto another account's home it starts fresh with a hand-off prompt, and the receipt says which. | Live through `fork_args` on 0.162.1 (M10); `tests/test_a_hand_off_of_a_codex_row_forks_its_thread.py` |
| A hand-off's Take over | `--resume <transcript>`, the same session | `codex resume <id>` on its own home; onto another account's home it starts fresh with a hand-off prompt, as `fleet switch` does. | `tests/test_a_hand_off_of_a_codex_row_forks_its_thread.py` |
| The captain's tools | `--strict-mcp-config --mcp-config … --tools "" --allowedTools mcp__captain__*`: no other MCP server loads, and its server inherits the session's environment | **Differs:** every tool feature off, `web_search` disabled, the TUI's own worktree tools (`mcp__codex_worktrees`) off through a server of that name switched off, and the server's tools approved (`default_tools_approval_mode`); `request_user_input` (Plan mode only) and the MCP-resource readers remain. Codex has no strict flag, so every MCP server the launch's config layers name (system, home, `-p` profile, managed, a trusted project's `.codex/config.toml` from its git root down to its folder, and `-c`) is switched off (`mcp_servers.<name>.enabled=false`), and the server gets the window's variables it needs by name (`env_vars`): a Codex MCP server starts with `HOME`, `LC_CTYPE`, `PATH` and its `env` alone. | Live through `tools_only_args` on 0.162.1 (M11: without the approval, `exec` declined the call; with a server in the home's `config.toml` and one in a trusted project's, the request carried `mcp__captain` alone, and `TMUX_TMPDIR` reached its server) |
| The captain's press and paste on a pane | Claude Code's chooser, trust dialog and `[y/N]` line (`claude_screen`) | Codex's own reading, asked of the pane's own row: its folder trust is the owner's, so a paste or a press there is refused (*trust this folder first*); its approval view is a chooser on `1` and Esc; its other dialogs are the owner's, so every key and paste there is refused too; a working read is overridden at its empty composer alone (`ready_on_screen`). | Unit tests over the screens measured on 0.162.1 (`tests/test_the_captain_reads_a_codex_pane_by_codexs_own_screens.py`, which fails on the merged head); live: the captain pressing yes on a real Codex approval |
| The captain's reply | Its transcript's text since the last prompt | The rollout's assistant messages since the newest prompt of the human's. | Unit tests over the recorded 0.162.1 rollout |
| Effort on the info line | `effort.level` in its hooks | **Differs:** read off the rollout's newest `turn_context` at each prompt and stop; its hooks carry none. | Live on 0.162.1 (M8: `"effort": "high"` under `model_reasoning_effort="high"`) |
| An unwatched pane (the captain's) | `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1` keeps its rating survey out | Nothing to set: its dialogs in a running pane are kept out by its fleet settings. | Adapter answer (`unattended_env`) |
| The captain itself | One Claude Code session per home | **Experimental:** `[team.profiles.captain] agent = "codex"` runs it on Codex, confined as *The captain's tools* says, from its brain folder (no git checkout, so no folder trust); its own pane read by Codex's screens (`modal_on_screen`, `prompt_on_screen`, `ready_on_screen`), its reply by its rollout ([the captain on Codex](captain.md#the-captain-on-codex)). An agent whose `tools_only_args` answers `None` is refused as the captain in one line. | Unit tests of the launch line (`tests/test_captain_spawn.py`) |
| A persona attached while it works | Its next prompt's hook hands it the briefing | Same: nothing goes through `codex queue`, whose message would run as a turn in the human's name, and its own next `UserPromptSubmit` hands the briefing over as context. | `tests/test_a_persona_attached_to_a_busy_codex_is_owed_never_queued.py` |
| A fleet server started in an agent's tool shell | `CLAUDE_PID` kept out of the server's environment | `CODEX_THREAD_ID` kept out too: every adapter's `tool_shell_env` and `pid_env` (`core.spawn.agent_identity_env_vars`). The doctor's *fleet server env* row names one a server pins, with the command that unsets it. | `tests/test_a_fleet_server_holds_no_agents_process_identity.py`, `tests/test_doctor_fleet.py` |

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
  `codex` on the slot. *Log in* on a row that still holds a login waits for
  `codex login` to write `auth.json` anew (it compares a digest of the file,
  never a value read out of it), not the login already there.
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
- **Folder trust.** Codex asks "Trust this folder?" once per `CODEX_HOME`, in a
  git checkout, and keeps the yes as a table in that home's `config.toml`:
  `[projects."<repository root>"]` with `trust_level = "trusted"`. With
  `[accounts] share_trust` (on by default) a launch on a Codex slot copies the
  table that covers its folder from another Codex slot that has it, appended to
  the slot's `config.toml` with every other byte kept (the file is made if the
  slot has none), and says so on the receipt. `aisquare accounts trust [<dir>]`
  and `--sync` cover Codex slots too, each in its own file. Measured on 0.162.1:
  the folder's own entry decides first, then its repository root's (a worktree's
  root is the main checkout's), and an entry above the repository, or under a
  symlinked spelling, covers nothing. A table that says `untrusted` is that
  account's no: Codex opens *Folder access … Open restricted* there instead, and
  no share overrides it. Codex's config.toml has no lock, so a running Codex that
  rewrites its own file at the same moment can drop a table just appended; the
  next launch copies it again.
- **Status line.** Under *⚙ Settings*, the Codex accounts have a dot of their
  own on a second line, `● 1× codex`, read like Claude's: the room left across
  the enabled, signed-in slots, with a ⚠ for one that needs a login.
- **Doctor** lists Codex slots and their headroom beside Claude's, in the same
  words (*percent USED*); reading Codex headroom needs no network, so it does
  not wait for `--live`.
- **Not shared:** Claude Code's `settings.json` link (`share_settings`) has no
  Codex counterpart; each slot's `config.toml` and `hooks.json` stay its own. A
  login kept in the macOS keyring (`cli_auth_credentials_store = "keyring"`) is
  unmeasured: AISquare reads `auth.json` alone.

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

**The trust boundary.** A sandboxed Codex fleet agent is trusted to the extent of the
tree it may write, exactly as a Claude Code agent (no OS sandbox) and a human contributor
are. What the user's own tools later run from that tree is inside that trust, documented
here rather than sealed: git hooks that live in the tree (`core.hooksPath` into
`.husky/`, `.githooks/`, what `lefthook.yml` or `.pre-commit-config.yaml` generate), which
the user's own `git commit` runs, and git config the repository includes from its own
tree (an `[include]` in `.git/config` naming a file there); a project's own virtualenv; a
subdirectory's `.claude/`, `.mcp.json` or `.codex/`, which a `claude` started there reads;
what the user's own `aisquare project onboard` packs the tree with (repomix, or `npx
repomix`, run in the project's directory, reads its config and `node_modules` there); and
`CLAUDE.md` and `AGENTS.md`, which agents read (a prompt-injection path, as on Claude
Code). So is the launch's `PATH`, the user's shell's: Codex itself runs the `git` it finds
there outside its sandbox (for its git info), as aisquare's hooks and broker run the
`git`, `gbrain` and `tmux` they find there. A launch whose `PATH` names a directory its
sandbox may write (an activated project virtualenv, direnv's `PATH_add`, an empty or a
relative entry, which is the directory a process runs in) is warned of it, and `aisquare
doctor` run in a project whose tree `PATH` names warns (`sandbox-path`): launch from a
shell whose `PATH` names nothing the agent may write. So is an unsandboxed agent's own
environment, which its hooks run in as its tools do: a Claude Code agent's hooks read its
`PYTHONPATH`, as every Python it starts there does. What aisquare itself runs or reads
outside the sandbox on a sandboxed agent's behalf (its hooks, its broker and the commands
it runs) and for the fleet (a window's `aisquare launch`, a hand-over) is kept from that
tree by these means, and only these:

- under the store's profile (below; a native `--sandbox` the operator picks replaces it,
  and these paths with it), the aisquare store, its launch records first, read-only in
  the sandbox, all of it but the launch's broker inbox; and with it the launch inputs at
  the top of the launch's trees, which the fleet's own launches there read, `.git`, the
  other agents' worktrees and the fleet's tmux sockets;
- whatever the sandbox, the aisquare that runs outside it: the launcher's own install,
  which the broker runs (its interpreter and the installation that comes from, its
  environment and the user site, which an `-E` interpreter loads whatever
  `PYTHONNOUSERSITE` says, unless a virtualenv keeps the system's site out, whose `.pth`
  files run at every start, its package, an editable install's source tree), and what the
  agent's aisquare hooks run, each with its environment: the program its `hooks.json`
  names (hooks connected from another install keep running that one; a `python -m
  aisquare` hook's virtualenv by the name it is reached by, not the interpreter it links
  to), and, where the plugin is installed for that home, its hooks trusted or the launch
  bypassing hook trust (`--dangerously-bypass-hook-trust`, under which Codex runs them),
  and the launcher not standing down beside a hook of ours whose program can start, what
  the launcher finds as its own `_find` does: the first `aisquare` that can start on the
  launch's `PATH`, in `~/.local/bin` or in `~/.cargo/bin`, or, with none, its `uvx` (found
  the same way) and what that runs: uv's cache (`$UV_CACHE_DIR`, uv.toml's `cache-dir`,
  else `$XDG_CACHE_HOME/uv`), the uv-managed Python it links to (`$UV_PYTHON_INSTALL_DIR`,
  uv.toml's `python-install-dir`, else `$XDG_DATA_HOME/uv/python`) and the launcher's
  answers.
  An environment (a virtualenv, an installation, a `bin/`'s parent) is counted by what
  holds its code, its `bin`, `lib` (Windows' `Scripts`, `Lib` and `DLLs`, a macOS
  framework's `Resources`), a virtualenv's `pyvenv.cfg` and the site-packages its
  interpreter loads, never by its prefix: a Python built with `--prefix=$HOME`, or an
  `aisquare` in `~/bin`, would otherwise hold every project under the home.
  A launch is refused where its sandbox may write one of them or a directory inside one
  (a root under the install, given with `--add-dir`, `-C`, `writable_roots` or a profile's
  `write` path: a `.pth` in its site-packages runs at every start of its interpreter, `-E`
  or not), naming that directory, by `aisquare launch`, by
  `fleet spawn` before any window is made, and by `fleet restart`, `switch` and the
  usage-limit hand-over before they stop the agent (under the home the replacement
  starts with: a switch's target slot), naming the remedy (`store_broker.refuse_inside`).
  What the sandbox may write is read as Codex layers its settings, the system's
  `/etc/codex/config.toml`, the user's `config.toml`, the `-p` profile beside it, the
  administrator's `/etc/codex/managed_config.toml` over them (its `requirements.toml` only
  narrows what may be chosen; macOS's MDM and cloud-managed layers are not files and are
  not read), then the launch's arguments (`AgentAdapter.sandbox_writes`,
  measured on 0.162.1): the sandbox is `--sandbox`, else `-c sandbox_mode`, else the
  store's profile, which holds over a `sandbox_mode` in the files, else the files'
  `sandbox_mode`; under `workspace-write` (the store's profile over `:workspace`,
  `--sandbox workspace-write`, `--approve-for-me`, or none named) it writes the workspace
  (`-C`/`--cd`, else the launch's directory), `/tmp`, `$TMPDIR`, each `--add-dir` and
  every layer's `sandbox_workspace_write.writable_roots` (a key or an inline table);
  under `read-only` (over `:read-only`, or `--sandbox read-only`) none of those; and in
  either, the profile's own `write` paths. A `read-only` seat in the project is not
  refused; an unsandboxed one (`danger-full-access`, the bypass flags) is trusted as its
  user is. The remedy follows what lets the sandbox write the install: one in the project,
  install the `aisquare-cli` distribution outside it (`uv tool install aisquare-cli`, or
  `pipx install aisquare-cli`) and connect the hooks with that install; one above the
  project (a launch from the home, whose tree holds every install under it), launch from
  the project's directory; one in a directory given the sandbox (`--add-dir`,
  `writable_roots`), drop it or move the install; one in a temporary directory, move it.
  `aisquare doctor` run in a project (a git root or a project marker, never the home or
  `/`) on a machine with Codex warns of an install in that project's tree
  (`sandbox-install`);
- `PYTHONPATH` and `PYTHONHOME`, which a Python reads before the standard library (a
  module of its name, a `sitecustomize`; an empty entry is the directory it runs in): the
  agent keeps them as the operator set them, as its own tools need them (direnv's
  `PYTHONPATH=$PWD/src`), and every Python aisquare starts for it ignores them (`-E`,
  `selfcli.isolated_argv_for`): its hooks as `aisquare agents connect codex` writes them
  (`<python> -E -P -m aisquare hook codex`), the plugin's launcher, which unsets them, its
  broker and the commands it runs, the fleet window's `aisquare launch`, the hand-over and
  the distiller. Hooks connected by an older aisquare read them: a launch warns where one
  names a directory the sandbox may write, `aisquare doctor` warns of such a hooks file
  (`sandbox-hooks`: an agent that sandboxes its tools whose hooks run the console script),
  and `agents connect codex`, or an upgrade's refresh, writes the `-E` form;
- every git aisquare runs (`core.workspace.git_argv`: the fleet's worktrees, the CI turn
  snapshot's `stash create`, which the agent's own hooks run, the project root's lookup):
  no hook and no fsmonitor of the repository's (`core.hooksPath` at the null device,
  `core.fsmonitor=false`);
- gbrain, which a brokered `recall` and a note's distill run: it starts in its brain's
  home, never in the tree, as a runtime may read its configuration from the directory it
  starts in (Bun, gbrain's, reads `bunfig.toml` and `.env` there). Recall passes its query
  as gbrain's positional, as gbrain's source parses one; that is unmeasured live: no host
  this was built on, nor any CI job, has gbrain (`tests/test_brain.py`'s real-gbrain test
  runs where one is installed).

The aisquare store (the aisquare home: `context.db`, the launch records, the config,
the account homes, the caches) is **read-only in a Codex agent's sandbox, all of it**
(under the store's profile, which every sandboxed launch of aisquare's runs under unless
the operator picks a native `--sandbox`), and the board calls its role protocol makes
there go through a broker that its launch runs outside the sandbox. So are the launch
inputs at the tops of the launch's trees, `.git`, the other agents' worktrees and the
tmux sockets (below). Claude Code runs its tools unsandboxed and is
unchanged: its board calls write the store themselves.

The profile, `aisquare`, is defined by the launch (`CodexAdapter.store_access`, then
`broker_access`, which each launch applies over whatever its launch spec recorded):

```text
-c default_permissions="aisquare"
-c permissions.aisquare.extends=":workspace"        # ":read-only" for a reviewer's seat
-c permissions.aisquare.filesystem={"<aisquare home>" = "read",
     "<tree>/.claude" = "read", "<tree>/.mcp.json" = "read", "<tree>/.codex" = "read",
     "<tree>/.agents" = "read", "<tree>/.aisquare" = "read", "<tree>/.git" = "read",
     "<project>/.aisquare-worktrees" = "read", "/tmp/tmux-<uid>" = "read", …,
     "<aisquare home>/broker/<launch id>/in" = "write",
     "<project>/.aisquare-worktrees/<its own>" = "write"}  # a workspace seat's alone
```

This is Codex's built-in profile for the write scope with, read-only within it (the
more specific path wins over a writable one around it: the workspace, `/tmp`):

- the aisquare home, all of it;
- in every tree the launch touches (its own directory and checkout, its project's
  principal one and its project's root, a non-git project's `.aisquare` marker
  included, its launch record's, and each tree Codex is given with `-C`/`--cd`,
  `--add-dir` or `writable_roots`), what a launch outside any sandbox reads there
  (`store_broker.launch_inputs()`: every registered agent's `capabilities.launch_inputs`
  and aisquare's own): Claude Code's project settings, hooks and MCP servers
  (`.claude/`, `.mcp.json`), Codex's project config and agents (`.codex/`, `.agents/`),
  aisquare's root marker (`.aisquare`) and the repository (`.git`, whose hooks and
  config run on the fleet's checkouts). A Codex tester, validator or ui-tester runs in
  the project's own directory, where the manager's `.claude/` lives, and the next
  Claude Code start there would have run what it wrote (review r10 of #113, finding 1).
  `AGENTS.md` and `CLAUDE.md` are content an agent reads, not what runs: they stay
  writable;
- every worktree of the project (the fleet's `worktree_dir`) but the agent's own, which
  a seat that writes its workspace writes again, its own launch inputs read-only within
  it; a `read-only` seat in its own worktree (a fleet reviewer's) writes none of it;
- the directories tmux keeps sockets in: the one its window's server listens in
  (`$TMUX`), `$TMUX_TMPDIR` or `/tmp`'s `tmux-<uid>`; never a shared directory itself
  (`/`, `/tmp`, `/var/tmp`, `$TMPDIR`, the home), where `tmux -S /tmp/x` puts one: then
  the socket file itself (measured on 0.162.1: read-only, its `mv` and `rm` from the
  sandbox fail with EBUSY while `/tmp` stays writable). From `/tmp`, which every sandbox
  may write, the fleet's socket could be moved away, and the fleet cut off its agents
  (finding 4; SMOKE5).

Each path is named whether or not it exists yet: then it cannot be made. Measured on
0.162.1 in a real turn, in the worktree layout (a path inside the workspace, outside
every writable root in the principal checkout, under an absent parent): the sandbox shows
an absent path as an empty read-only mount point, and leaves nothing on the host. One path is writable: this launch's
broker inbox, which no other launch's profile names. Measured on 0.162.1 (store outside
`/tmp`), from a sandboxed command: the store's database reads and does not write
("attempt to write a readonly database"), no file in the store can be made or changed
("Read-only file system"), `.claude/settings.json`, a new `.mcp.json`, another agent's
worktree and `rm` of the fleet's socket are refused, and `aisquare note`, `aisquare
task claim` and `aisquare board` succeed on their first, sandboxed attempt, through the
broker.

**The broker** (`services.store_broker`). `aisquare launch` of an agent that sandboxes
its tools starts one per launch, outside the sandbox, before it execs the agent (as it
starts the telemetry receiver), and it exits with the agent. The CLI in the sandbox,
finding the store read-only and the broker named in its environment
(`AISQUARE_BROKER`, `AISQUARE_BROKER_NONCE`), puts its argv and cwd in the inbox, rings
the broker's bell and prints what the command printed, with its exit status: the agent
types the same `aisquare note "…" --as <id>` as on Claude Code. The broker runs a
request as `aisquare <argv>` in the launch's environment, never the caller's, and only:

- a command on its whitelist (below); anything else is refused with a line saying
  what it runs. It never runs a fleet operation (`fleet spawn`, `stop`, `restart`,
  `switch`, `tell`, `shutdown` …), a launch, a hook, a binding, the config or an
  account: ask for those to run outside the sandbox (an approval), or leave them to the
  operator. `board --watch`, a full-screen view, is refused too;
- as the launch's own board sessions: every `--as` resolves where the board resolves
  it (`services.team._acting_as`), and a command the broker runs may name only a
  session the agent's own hooks bound to this launch (`launch-session:<launch id>`,
  `fleet-session:<row id>`, written by hooks Codex runs outside its sandbox). Another
  session's id is refused ("is not this agent's own session"), and so is any before
  the agent's first hook ran. A filter (`team log --by`) names any session;
- on the launch's own board and in its own project's pool: a request runs only in the
  launch's own tree (a fleet agent's worktree and its project's directory, from its
  launch record; a hand launch's directory, its checkout, its project's principal
  checkout and its project's root),
  never by the board a cwd resolves to (with `AISQUARE_TEAM_HUB`, which every launch
  with a project sets, every directory resolved to the hub's); a task ref names only a
  task of its board (`services.team._own_task`: task ids resolve across every board);
  `remember`/`context add` write only its project's pool, never the user pool, which
  every project's agents are given (`--user` is refused, and the default is the
  project's: review r10 of #113, finding 3), and `inject`, `context list`, `search`,
  `preview` and `log` read the launch's project's, the pool its broker names, past any
  `project switch` pin and whatever project another directory in its tree resolves to;
  `context show` shows an entry of that pool or the user pool, and refuses another
  project's (kind `pool`);
- signed with the launch's nonce, which only the agent's environment holds (another
  launch's sandbox has its own pid namespace and cannot read it): a request carries a
  MAC under it, never the nonce, as a request file is readable from every sandbox, and
  each request runs once;
- never setting the fleet's own signals (`fleet-paused`); reading them is fine. No agent
  tool sets them: the MCP `signal` tool is refused them too (finding 8), and an MCP
  server serving an agent that sandboxes its tools names only its board's tasks.

Every refusal leaves a record outside the caller's stderr, one line in
`<aisquare home>/log/store-broker.log`: each the broker makes (a command off its
whitelist; a request that is not a file, does not parse, is of another version, names no
command, holds a NUL, names no directory it can run in, was run already, is not signed by
its launch, or comes once the agent has exited; a directory outside the tree) and each
scope refusal of a command it runs (another session, another board's task or another
project's pool, a fleet operation or signal, a directory outside the tree). Of those, a
session that is not its own (kind `identity`), another board's task or a write asked
from outside the tree (`board`), another project's pool (`pool`), a fleet operation, its
signals included (`fleet`), and a request not signed by its launch (`signature`) also put
one note on its board for its manager and the human, once per launch and kind (SMOKE5); a
refusal with no launch to name is logged only. A read asked from outside its tree (`cd
/tmp && aisquare board`, kind `read`) is refused and logged, never noted.

The transport is files, not a socket, by measurement: on 0.162.1 the sandbox's seccomp
filter refuses `connect` on every unix socket (a path one, on a read-only or a writable
mount, and an abstract one: "Operation not permitted"), while a file can be written in
a profile's `write` path and a FIFO can be opened for writing on a read-only one. Each
launch has `<aisquare home>/broker/<launch id>/`: `in/` (the sandbox's to write; a
request is opened by name, never through a link and never blocking on a FIFO, and
removed once read), `bell` (a FIFO), and `out/` (read-only to the sandbox), where the
broker answers. The broker takes a request by renaming it, in one step, and the CLI
says a call failed only once it has taken its request back, so the command never ran:
a bell it may not open is waited past (the broker polls `in/` every half second), and
a request taken and not answered says it may have run, so a retrying agent never files
a note twice (finding 2). The CLI goes through the broker when it cannot make a file in
the store: `access(2)` alone sees bwrap's read-only mount, not a sandbox that refuses at
`open` (Landlock), and no variable tells, since Codex gives a command it escalated out of
its sandbox the very environment of a sandboxed one (measured; finding 6). When the
agent exits, the commands still running are ended and nothing more is taken (finding
7); whatever the sandbox leaves in `in/` (a directory, a link) is removed, never taken
twice (finding 9); output past 4 MB is cut and says so, with exit 74 (finding 10). No
whitelisted command reads stdin, so none is forwarded. An MCP
server, by the same measurement, runs outside the sandbox (host namespaces, no
seccomp), with only the variables its launch hands it: the MCP tools (`agents.mcp`)
keep their own identity rule (`services.mcp_server`).

A broker that cannot start costs the agent its board calls from the sandbox, which then
say so ("this agent's broker is not running … restart the agent"), never the launch.
The broker is POSIX only (FIFOs); the fleet's sandboxed agents are too (tmux). Measured
on Linux (bwrap) only: macOS's Seatbelt and Windows' sandbox are unmeasured, which the
transport's two choices above are made to survive.
Escalated commands, which Codex runs outside its sandbox once approved, and the agent's
own hooks write the store directly, as before.

The fleet's launch records stay, as defence in depth: what a sandbox could once write
is still never what a replay, a kill or a typed line reads.

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
home. A row with no record, a record that cannot be read or was written by a newer
aisquare, and a project whose directory in the store is not the record's are refused
before anything is stopped, with the reason and the way out. The reason is also a board
note, once per row and reason, whichever path met it first (restart, switch, the
hand-over, a message told to a busy agent), so an unattended hand-over leaves it where
the manager and `fleet ls` readers look. A spawn that cannot write a record starts no
agent, asked before the project is captured or a worktree made. A record is read back as
it was written, so a release that adds a field reads an older record with that field's
default.

Agents spawned before this version (legacy rows) are trusted as legacy, once: the first
fleet read or spawn of this aisquare, and any launch of an agent that sandboxes its
tools, takes a census (`launch-seals/legacy-census.json`, read-only to the sandbox) of
every fleet row that has no record, while no record has been kept on this machine yet,
so before anything in a sandbox could have written the store. Each such row gets a
record made from the row as it stood then, and is placed, told, stopped, restarted
(fresh: no session is held for it), reaped and shut down like any other; its session is
the one tmux says holds its pane. The census is never taken again, so a row inserted
later with no record (a fake one, or one whose record was lost) is not legacy: it is
told nothing, its word reaches only a dead window of its project's sessions (`fleet
stop` removes it and ends the row), one spawned before this boot is ended as lost, and
`fleet shutdown --forget` ends the rest on your word.

Where the fleet acts on a row's window is decided in one place, the placement
resolver (`services.fleet.resolve_place`): it answers with the pane, the session, the
server, the agent, the role and the label the row's launch record holds, or with a
refusal saying why there is none. Every tmux question or act about an agent goes to
what it answers, never to the row's `pane_id`, `tmux_socket`, `socket_path`, `agent`,
`label` or `ended_at`, nor to the project's codename: `fleet tell`'s typing and a
spawn's prompt, the manager's nudge (which a sandboxed agent's own hooks deliver from
outside its sandbox), `/exit` and the kill of `fleet stop` and with it restart, switch,
the hand-over and `shutdown`, whether an agent runs (`restart`, the listings, `reap`, a
spawn's reconcile, the doctor), its lingering and late windows, the sessions `shutdown`
takes down, `fleet rename` and `fleet attach`. A label names the row whose record holds
it. The record's agent's rules decide whether to type (its readiness check keeps an
Enter out of Codex's dialogs) and whether it redraws while it waits on you (its bell).
A test, `tests/test_fleet_placement.py`, enumerates every place the fleet's and the
board's services, the doctor and asq's views make a tmux server or name a pane, a
session, a socket or a path to one, and fails at a site it does not list with where its
values come from: a launch record, tmux's own answer, or today's config.

A row rewritten in the store — to name another pane, socket, agent or label, or ended
there while its window runs — is not typed into (`tell` files the board note, the nudge
waits), and is listed, stopped and shut down where its record places it, as running, so
one `UPDATE` of its own row neither makes an agent unstoppable nor hides it from
`shutdown` or `restart`; the board says what was rewritten. Whether a row runs is read
from the panes alone — the windows its project's spawns recorded beside the records,
and the census's panes in the legacy session — never from its `ended_at`, so a running
agent keeps its label, its worktree and its place as the project's one manager whatever
its row says. A label names the row whose record holds it; a row with no record is taken
at its word only when no record holds that label. What the fleet types into a pane
carries no control characters but line breaks and tabs, so text from the store cannot
end a bracketed paste on a tmux that does not sanitise one. Sessions are tmux's own ids
(`$3`): the ones a project's spawns made are kept beside the records
(`launch-seals/project-<digest>.json`), a spawn joins one of them (or the legacy
session) whenever it is still there, else makes its own, so a swap of two projects'
codenames never puts an agent into the other's session; `shutdown` takes down every
session the project's spawns made, the ones its records name and its legacy session,
whatever rows are left or how long ago they ended, never `asq-<codename>`; `fleet
rename` renames each of them, under names of their own, and the records' names,
whether or not tmux could be told; `--all` also sweeps every `asq-` session tmux lists,
sparing one that holds a row the run left live.
What the fleet reads of a session to act on outside the sandbox, a Codex turn that
failed on its limit with no hook (which parks the row and may hand it over), is read
from the transcript the record holds, never the board session's, and only by a reader
whose tmux answered, so a Codex manager's sandboxed `fleet ls` leaves it to the app's
tick or a `fleet` command of yours.

Validated, besides: a native id goes on Codex's command line only as a UUID, the
shape Codex makes (`codex resume` and `codex queue` read a value that starts with
`-` as an option of their own), even from the record.

What a sandboxed agent writes on the board is what its broker's whitelist writes, as
its own sessions: tasks, notes, its focus, its signals, memory. Its session's state
(which decides whether `fleet tell` types, queues or files a note) is its own hooks',
written outside the sandbox. A path denied reading (`"none"`) is not used: with one,
Codex reads its own instruction files through its sandbox helper, and where bwrap
cannot bring up loopback the session does not start.

Codex's sandbox leaves an empty file on the host where a path its profile names does
not exist (measured), so the launcher makes the store and the broker's inbox first.
Codex's own guards in the workspace (`.git`, `.codex`, `.agents`) still hold. The
profile holds beside `--approve-for-me` and over a `sandbox_mode` in `config.toml`. A
plain `--add-dir <aisquare home>` (or `sandbox_workspace_write.writable_roots`) would
make the store writable again, so the fleet does not use it.

The reviewer's seat is the same profile over `:read-only`: its verdict reaches the
board through its broker, and the worktree stays read-only to the sandbox, which is a
filesystem guard where Claude Code's reviewer has only `--disallowedTools`. What the
profile does not cover:

- `--sandbox` (and `-c sandbox_mode=…`, `default_permissions`, `permission_profile`,
  `sandbox_workspace_write.*`) given in the native arguments chooses the sandbox the
  older way, which replaces any profile: the fleet adds none and starts no broker, and
  the store is then the operator's to reach.
- A pick in Codex's own `/permissions` swaps the profile for a built-in one until the
  agent is launched again; `aisquare fleet restart <label> --permission-mode
  workspace:ask` (or any `WRITE:APPROVALS`) restores it, with the reviewer that says.
- A Codex started by hand (`codex`, or `aisquare launch` without the fleet's
  permission flags) runs in the sandbox its own config chooses, with no broker. Inside
  one, a board call that cannot open the store says so and names the directory, and
  never tells the agent to move the store aside: nothing in it is damaged.
- `danger-full-access` (`full:*`) has no sandbox, and so nothing to add.

Who answers an approval is pinned with it: `ask` is `-c approvals_reviewer="user"`
(Codex's "Would you like to run the following command?" in the pane), `auto` is
`--approve-for-me` or `approvals_reviewer="auto_review"` (Codex's reviewer, no prompt),
on a spawn, a restart and a resume alike. A resumed thread otherwise keeps the reviewer
it ran under, and `config.toml` may name either.

### The broker's whitelist

What a sandboxed agent's broker runs (`services.store_broker.WHITELIST`); a test holds
this list to it, and every board call a role's cycle teaches to it:

- `aisquare note`
- `aisquare board` (not `--watch`)
- `aisquare recall`
- `aisquare task add`
- `aisquare task list`
- `aisquare task show`
- `aisquare task claim`
- `aisquare task next`
- `aisquare task review`
- `aisquare task reopen`
- `aisquare task done`
- `aisquare task block`
- `aisquare task drop`
- `aisquare task release`
- `aisquare team status`
- `aisquare team focus`
- `aisquare team log`
- `aisquare team verify`
- `aisquare team signal` (not the fleet's own)
- `aisquare team signals`
- `aisquare remember`
- `aisquare inject`
- `aisquare log`
- `aisquare context list`
- `aisquare context show`
- `aisquare context search`
- `aisquare context preview`
- `aisquare context add`

Global output flags (`--json`, `-q`, `-v`, `--no-color`) and `--help` are fine; `aisquare
--version` runs in the sandbox, reading nothing. Everything else is refused there,
`aisquare fleet …` with the reason that the broker never runs a fleet operation.

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
Grok, ...) is implemented. The shared services (hooks, board, launch, fleet, doctor,
Welcome, upgrade and uninstall) ask its adapter and never compare its name:
`tests/test_no_agent_literals.py` fails on `== "codex"`, `in (..., "claude-code")` or a
`match` case on either name anywhere outside that package, and holds a ratchet on the
rest of the agent-specific shapes there: a comparison with a constant naming an agent
(`AGENT`, `CODEX`, `DEFAULT_AGENT`, `DEFAULT_AGENT_BINARY` and any module constant set to
an agent's id or binary), a call of an `is_default_agent` (the binary-name predicate, gone:
a binary is the agent its name runs, `agent_adapters.adapter_for_binary`, and that agent's
adapter answers), and a string constant that is an agent's binary, one of its variables,
files or folders, or one of its flags. The first three are none outside the package. Its
`BASELINE` counts the rest per file; a new one fails with its
`file:line`, and a change that removes one lowers the count (aisquare's own options, prose
naming a variable and a provider's API key are not counted). Keep unsupported capabilities
explicit instead of copying another agent's flags or claiming hookless detection is
integration.

### The files

A new terminal agent touches these nine files, and no other: no shared module names an
agent, compares with an agent's id or constant, or asks a binary's name
(`tests/test_no_agent_literals.py`), and `tests/test_third_agent_seams.py` holds it with the
Gemini-shaped synthetic agent (`tests/gemini_shaped_agent.py`, registered in tests alone),
whose launch, hooks, transcript tail and pages, usage and info line, uninstall and Welcome
run through the shared code: every file that names it is one of the nine.

| # | File | What |
|---|------|------|
| 1 | `src/aisquare/core/agent_adapters/<agent>.py` | A class implementing `AgentAdapter` (`types.py`), with its `AgentCapabilities`. |
| 2 | `src/aisquare/core/agent_adapters/__init__.py` | One line: `register(<Agent>Adapter())`. |
| 3 | `tests/fixtures/agent_events/<id>.json` | `events`: a recorded payload for every hook event it installs, with the `AgentEvent` fields it must decode to; `version_output`/`version`; `hooks_off` (the file and text that switch its hooks off); `lasting`: the `files` of a home that keeps aisquare's MCP server run through uvx (beside one that is not aisquare's) and the `[kind, name, fetches]` its `lasting_installs` must read; with account slots, `accounts`: the files a `login` leaves in a slot's home (in the config dir its accounts' `config_dir` names there), and a `relogin` (the same login after a new sign-in). |
| 4 | `tests/test_adapter_contract.py` | Its row in `EXPECTED_PERMISSIONS` (a coder's and a reviewer's flags), in `EXPECTED_HEADLESS` and `RECORDED_HEADLESS` (its headless call's session and hook switches, and an answer it gave), and in `EXPECTED_TOOLS_OFF` (what leaves its tools-only session no built-in tool). |
| 4a | `tests/fixtures/agent_transcripts/<id>.json` | `moments`: its transcript as it stood at a permission prompt, a question, an interruption and a turn ended on a question (`records`, or the first `lines` of a recorded session file beside it), with what `transcript_reader` must read of each. |
| 4b | `tests/fixtures/agent_screens/<id>.json` | With a `screen` whose `dialog_on_screen` or `screen_prompt` is set only: `dialogs`, its screens at each dialog of its own (`rows`, as `capture-pane -p` gives them) with the `words` `dialog_on_screen` must read of each and, optionally, the `prompt` (`[shape, yes_key, no_key]`) `screen_prompt` must, and `not_dialogs`, screens with none (its empty prompt, a draft, a turn), each with `"ready": true` where `ready_on_screen` (or its default) reads it ready for a typed line; a `modal` on either, where given, is what `modal_on_screen` (or its default) must name on it (`null`: nothing). |
| 5 | `tests/test_<agent>_native.py`, `tests/run_<agent>_native.py` | The opt-in test against the real binary (`AISQUARE_TEST_<AGENT>=1`), as `test_codex_native.py` and its CI job do for Codex. |
| 6 | `docs/coding-agents.md` | Its setup and native settings. |

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
| `decode_event(payload)` | The payload as an `AgentEvent` of one kind: `session_start`, `prompt`, `stop`, `session_end`, `interrupt`, `attention`, `tool_done`, `turn_failed`, `notification`; `None` for one AISquare ignores. Its `model` and `effort` are what the session runs with, wherever the agent records them: Claude Code's payloads carry both; Codex's carry no effort and its `Stop` no model, so its `prompt` and `stop` take them from the rollout's newest `turn_context` (`codex.turn_settings`, measured on 0.162.1). |
| `encode_reply(kind, reply)` | What the hook prints: context for `session_start`/`prompt`, a "keep going" decision for `stop`, `""` for nothing. |
| `model_args`, `resolve_model`, `native_args`, `native_choice`, `validate`, `effort_alias` | Model and reasoning effort: the flags, the configured choice, the native spelling, what a native argv already chose. |
| `permission_args(intent, role, native)` | A `types.PermissionArgs(args, notes)`: the flags for a `PermissionIntent` (below), less what the `native` arguments already settle, with `store_access` for its write scope, and what the spawn's receipt says of them; `ValueError` for one it cannot express. An agent with no reviewer of its own (`capabilities.approves_for_me` False) runs `auto` approvals as `ask` (`types.stepped_down`), so the built-in `workspace:auto` is not refused for every role, and the receipt and the Settings page's launch line say "<agent> has no reviewer of its own: workspace:auto runs as workspace:ask"; Claude Code's `auto` mode and Codex's `--approve-for-me` are reviewers. `fleet.roles.<role>.permission` and `--permission-mode WRITE:APPROVALS` reach every agent through it (`services.fleet.permission_flags`). |
| `store_access(write, store)` | The launch arguments that hold the aisquare store in `store` (`types.store_dir()`) read-only in a session sandboxed at `write`, all of it (`types.store_read_only(store)`), made first by `types.prepare_store` ([the sandbox and the store](#the-sandbox-and-the-aisquare-store)). `[]` without `sandboxes_tools` and for a scope run unsandboxed. |
| `sandbox_writes(args, cwd, home)` | What the sandbox of a session launched with `args` in `cwd` may write (a `types.SandboxWrites`: its writable roots, the read-only paths within them, its workspace and temporary roots), read as the agent reads its settings: its config in `home`, the profile `args` select, then `args`, whatever chose the sandbox (`store_access`'s profile, a native flag or setting, the directories added). `None` only where the session runs unsandboxed. `services.store_broker.refuse_inside` refuses a launch whose sandbox may write an aisquare its hooks or broker run, or a directory inside one, from this answer alone; the conformance suite fails an agent with `sandboxes_tools` that answers `None` for a sandboxed scope. |
| `broker_access(args, store, inbox, *, protect=(), writable=())` | `args` with the launch's broker inbox the one writable path under `store`, `protect` (the launch inputs of every tree the launch touches, the other worktrees, the tmux sockets: `services.store_broker.launch_paths`) read-only and `writable` (the agent's own worktree, for a seat that writes its workspace) writable, the rest of `store` read-only whatever `args` recorded, applied by every launch (`services.store_broker.start_for_launch`); `None` where `args` run no sandbox of `store_access`'s. An adapter with `sandboxes_tools` declares one, and the conformance suite fails one that does not. |
| `fleet_args(role, label, mode, ...)` | A fleet spawn's flags from the role's native permission settings, then `label_args(label)`'s arguments. |
| `resume_args(native_id, transcript)` | The arguments that resume a session, put first: `fleet restart`, `fleet switch` and Restart resume the native id the first hook bound (Codex: `codex <options> resume <id> -- <continue line>`). |
| `fork_args(native_id, transcript)` | A `types.ForkArgs`: the arguments, put first as `resume_args` are, that start a NEW session carrying this one's conversation (a hand-off's Fork), and `mints_id`, whether the launch names the new session (Claude Code: `--resume <transcript> --fork-session`, beside the `--session-id` the fleet mints) or its first hook does (Codex: `fork <id>`, measured on 0.162.1: a new rollout and thread id, the source's turns carried). In the session's own config dir unless `resumes_across_homes`. `None` for an agent that cannot fork: the fork starts fresh with a hand-off prompt, and the receipt says so. |
| `exit_command` | What `fleet stop` types to end the session so its `SessionEnd` runs. |
| `dismiss_key` | The one tmux key that closes a dialog of the agent's own with its least answer and leaves it at its prompt (both agents: `Escape`; never `Enter`). The automatic usage-limit hand-over presses it once where the screen shows anything but the empty prompt, and types `exit_command` once that prompt is back, as Remote's Switch with `dismiss_dialog` does; a screen the key does not bring back is killed as before. |
| `queue_args(native_id, message)` | The arguments that queue a message for a RUNNING session's next turn (Codex: `queue --thread <id> --message <text>`), run with `home_env` set to the session's dir: what `fleet tell` does for a busy agent. `None` for none (a board note). |
| `key_spellings` | tmux key names the agent reads under another one, which the fleet UI's pane and Remote's key pad send instead (Codex: `S-Enter` → `C-j`, its newline). |
| `screen` | How its screen is read, an `AgentScreen` (below, after the capabilities): its empty prompt, a turn running, its dialogs and prompts, and the readings derived from them; `None` for an agent whose screen is not read (`types.NO_SCREEN`). |
| `role_defaults(role, binary, args)` | The flags a role needs on this agent wherever it starts (Claude Code's ui-tester `--chrome`), less what `args` says. |
| `browser_tools(home)` | The browser the agent brings itself, as the doctor's browser-tools line names it (Codex's `browser_use` feature and bundled browser plugin); `None` where the doctor finds it another way. |
| `mcp_args`, `label_args`, `disable_native_teams`, `fleet_settings(home, args)`, `settings_layer(settings, file, home, env, cwd)`, `context_files` | The launch slots, each a `types.LaunchPart(args, env, settings, notes)`: what the agent takes on its command line, in its environment, and in its settings layer of this launch, and what the launch prints of it. AISquare's MCP server for this session; the fleet label in its UI (Claude Code's `--name`); what keeps its own sub-agent teams off; what a fleet window's agent launches with to keep a dialog of its own out of a pane the fleet types into, less what the operator's config layers set, an administrator's managed one included, of which a note says so (Codex's `notice.hide_rate_limit_model_nudge=true` and `check_for_update_on_startup=false`, its layers read once, in `codex_config.layers`), asked only in a fleet window (`agent_launch.fleet_settings`). A launch composes them (`agent_launch.compose`): their arguments in order, their environment into the agent's, and their settings, with those a fleet spawn's own slots handed its window (`agent_launch.carry_settings`, the label's and the teams'), merged table by table (`types.merged_settings`) and given by `settings_layer` as the session's one settings layer ABOVE the operator's, where a value their own user or project settings set loses to it: Claude Code's `--settings <file>`, Codex's `-c` pairs, an agent whose seams are settings files a file of the launch's own under the cache that its variable names, of a layer over every settings file of the operator's (Gemini CLI's system settings, `GEMINI_CLI_SYSTEM_SETTINGS_PATH`, not its system defaults, which are under them; the agent writes `file`). The slot runs with the launch's home, environment and directory: such a file stands in for the machine's own of its kind (Gemini CLI's `/etc/gemini-cli/settings.json`, or the one the variable names), which the adapter reads by them and carries in it, an administrator's value kept over AISquare's with a note that says so (`LaunchPart.notes`, which the launch prints). `None` for an agent with no layer, whose slots answer no settings. And the instruction files it reads. |
| `unattended_env()` | Environment for a session nobody may be watching (the captain's, answered from a phone) that keeps out a prompt of the agent's own which would hold its pane and is no work's. Claude Code: `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1` (its session-rating survey). Codex: none; its dialogs in a running pane are kept out by `fleet_settings`. |
| `tools_only_args(server, command, args, env, *, inherit, home, cwd, launch_args)` | The arguments that leave the session no tool but those of the one MCP server `server` (run as `command` with `args` and `env`, keeping the session's own values of the variables `inherit` names), each allowed without asking, and no other MCP server loaded, the operator's own included: the captain's session, launched from config dir `home` in `cwd` with `launch_args`. `None` for an agent that cannot be confined so, which cannot be the captain; `ValueError` for a server its arguments cannot name. Claude Code: `--strict-mcp-config --mcp-config <the server> --tools "" --allowedTools mcp__<server>__*`, its servers inheriting the whole environment. Codex: `--disable` for each tool feature (`codex.TOOL_FEATURES`), `-c web_search="disabled"`, the server as `-c mcp_servers.<server>.*` with `env_vars` for `inherit` and `default_tools_approval_mode="approve"`, and `-c mcp_servers.<name>.enabled=false` for every other server the launch's config layers name (only those: on a name Codex does not have, it refuses its config), then `codex.TUI_TOOLS_SERVER` named with the server's command and switched off, which keeps the TUI's worktree tools out (0.162.1 and 0.164.0); measured on 0.162.1, the request then offers the server's tools, `request_user_input` (Plan mode only) and the MCP-resource readers. |
| `transcript_reader` | An `AgentTranscript` (`types.py`, the shapes and helpers in `agent_adapters/transcripts.py`), which Remote's needs-you scan and Transcript tab ask of the row's agent (`services.transcript.reader_of`), or `None`. Its `speaker` is how a page names the agent's turns (`* <speaker>`); `records(lines, ahead)` turns the `(offset, line)` pairs a backwards walk reads (`transcripts.lines_backwards`) into its records at those offsets, newest first, `None` for a line that is no record of its own, folding a record a later line completes into it (a tool call's result in a later `{"$patch": …}` line, Gemini CLI's), which the Transcript tab's pager reads (`services.transcript.read_page`); `ahead` is the lines after the walk's first, oldest first and read only as far as the reader asks (`transcripts.lines_forward`: the pager's are the newer pages'), where such a reader reads on for a later line of a record on this page, and nothing of it is yielded; `transcripts.one_per_line` is one whole record a line, Claude Code's and Codex's, which read nothing ahead; `tail_facts(file, size, budget, written)` reads the end of the transcript, backwards and within `budget` bytes (`transcripts.lines_backwards`), into a `TranscriptTail`: the calls still waiting for their result, the newest record's kind, the agent's last words, and `cut_at` for a walk that ran out inside the newest model response of a turn still running (Codex's: a rollout's `function_call` without its `function_call_output`, `turn_aborted`, `task_complete`, the response's `token_count` as its end); `page_lines(record, width)` renders one record as the page's lines (`transcripts.YOU`, `speaker_line`, `wrap`, `dim_note`, `question_lines`); `call_asks(tool)` says what a pending call waits on the human for (`question`, its questions in `input["questions"]` as `AskUserQuestion` holds them, `plan`, `subagent` or `tool`); `call_mark(tool)` what its prompt shows of it, to tell several pending calls apart on screen; `answer_keys(kind, options)` the quick answers of a `permission`, `question` or `plan` card on its dialog (every key one of `remote_needs.NEEDS_ANSWER_KEYS`); `reply_since(file, since)` the agent's words since the newest prompt of the human's, which the captain reads back (`""` an answer without words; `None` with no prompt, nothing of the agent's after it yet, or a prompt older than `since`). Claude Code's reads its session JSONL (`claude_transcript.py`, its reply main's `core.transcripts.last_reply`), Codex's its rollout (`codex_transcript.py`, its reply the assistant `message` records after the newest `user` one that is not its own context). |
| `accounts` | Numbered login slots, an `AgentAccounts` (`types.py`), or `None`: the home's variables (`home_env`, `extra_env` each set to the slot's scratch dir, `plain_vars` where a launch keeps the shell's own) and the slots' directory name (`dir_name`), the plain slot's number and name (`plain_slot`, `plain_label`) and home (`plain_home(home, plain)`), `install_command`, `noun` (what prose calls one of its accounts: a `Claude` account, a `Codex` one), `network_usage` (its `usage` leaves the machine; a local reading is as fresh as the slot's last session, and a listing says how old it is), `legacy_slots` (Claude Code's alone: its slots predate every other agent's, so their arrangement is the store's own `claude_account` table and project key, its plain slot is listed before its home exists, and the listings, `accounts --json` and the doctor's `claude-*` rows name them as they always did; `False` for a new agent, whose slots are keyed by its id), `sign_out` (how a plain home is signed out, as a refusal to remove it says), `shares_settings` (`[accounts] share_settings` links a managed slot's settings to the plain home's: Claude Code's `settings.json`), `launched_account(env, accounts)` (the slot a launch with `env` runs on: its home, `core.claude_accounts.account_in_home`; Claude Code's by the `.claude.json` it reads), `sign_in_args` (the agent's own sign-in command run in a slot, Codex's `login`; `()` for an agent that signs in from its own TUI, Claude Code's and Gemini CLI's: a sign-in window starts the binary in the slot with nothing after it, the human signs in there and leaves it with its `exit_command`, and the window waits for `login_fingerprint` to change), `identity`, `signed_in(account, fresh=)`, `subscription`, `token_state` (`ok`, `expired`, `missing`, or `unreadable` when the store its login is kept in would not say), `usage` (a `ClaudeUsage` of the windows its plan has, shortest first, each a `models.UsageWindow(name, minutes, used_percent, resets_at)`: Claude Code's five-hour `session` and seven-day `week`, Codex's primary and secondary by length, an agent of daily per-model quotas one a model; `session_percent` and `week_percent` are views of the two so named, and the headroom pick, the sidebar's `#slot·%` and the info line go by the window that limits, `ClaudeUsage.limiting`, skipping a reading whose shortest window went unread), `limit_from_transcript`, `failed_turn`; `login_fingerprint(account)`, a digest of the login stored in a slot (never a token), which a *Log in* over a login still on disk waits to see change, `None` with none stored, raising `LoginUnreadable` when the store would not say; and `trust`, a `FolderTrust`, or `None` for an agent that asks nothing per folder: `file_name` (the file in a home that holds it), `scope(base)` (the folder and the key a yes there records, from `core.claude_sharing.trust_scope`, in the agent's own rules: its `covering` entries in the order it reads them), `read(account)` (`{key: trusted}`; `False` is a no that no share overrides; `SharingError`, or `NotStartedYet` with nothing to add to yet) and `grant(account, *keys)` (a yes for each, nothing else in the file changed). `types.trusted_by(records, scope)` is the entry that decides. `share_for_launch`, `accounts trust` and `--sync` copy trust only between accounts of one agent. And `config_dir(slot_dir)`: the config dir the agent reads in a home its `home_env` names, where a slot's sign-in connects aisquare (`core.claude_accounts.agent_config_dir`) and the uninstall plan finds its hooks: the slot itself for Claude Code and Codex, `<slot>/.gemini` for Gemini CLI. |
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
telemetry's lane, and with neither it reads untraced), `positional_prompt`, `first_context_file_only`, `approves_for_me` (a reviewer of its own decides `auto` approvals; without one `auto` runs as `ask`), `sandbox_permissions`,
`sandboxes_tools` (its tools run in an OS sandbox, which `store_access` must open to
the store), `redraws_while_asking` (its UI redraws while it waits on the human, so an
attention row holds until a hook moves it or its screen's `working_on_screen` reads a turn running),
`hookless_through` (the last aisquare release that writes
none of its hooks, Codex's 0.8.2: an upgrade or move back to it leaves them and says so),
`legacy_fleet_args`, `value_options` (its options that take a value; one that is also a short
option of `aisquare launch`'s, Codex's `-a`, is named in launch's refusal of a value that is
none of launch's), `switch_options`.

Its screen is read through one optional reader, `screen`, an `AgentScreen` (`types.py`): nine parts, each optional, and three readings derived from them where the agent declares nothing more of its own, `AgentScreen.ready` (a typed line lands as a prompt: `ready_on_screen`, else its empty prompt with no turn running on it), `AgentScreen.drawn` (its prompt drawn, busy or not: `prompt_on_screen`, else its empty prompt) and `AgentScreen.modal` (what a line must not go into: `modal_on_screen`, else `dialog_on_screen`'s words, else `screen_prompt`'s question). An adapter with no screen (`screen = None`) reads as `types.NO_SCREEN`, never at its prompt and never ready (`types.screen_of`): the Gemini-shaped one in the suite runs so. Remote reads its empty prompt by `idle_on_screen` and `idle_prompt_mark` alone (`AgentScreen.reads_empty_prompt`, `at_empty_prompt`), never by `ready_on_screen`. The parts: `idle_prompt_mark` (what the last line of the screen starts with at an empty prompt,
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

The store profile is [the sandbox and the store](#the-sandbox-and-the-aisquare-store)'s.

### What the conformance suite fails on

`tests/test_adapter_contract.py` runs every case for every registered adapter and for
two synthetic ones registered only there: `tests/fake_agent.py` and
`tests/gemini_shaped_agent.py`. It fails when:

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

The Gemini-shaped adapter has Gemini CLI's shapes as its docs give them (nothing of it is
installed or measured): `GEMINI_CLI_HOME` names the folder its config dir `.gemini` is in,
its hook timeouts are milliseconds, every hook reply is JSON, and a call's result reaches
its log in a later `$patch` line. Both, through `tests/test_third_agent_seams.py`, are
connected and disconnected in their own handler shape, named by the uninstall plan with
their own removals (a purge their uvx server would undo refused, their account homes
counted), offered by Welcome with their own installers, and found, connected and read by
first run, with no edit to a shared file.
