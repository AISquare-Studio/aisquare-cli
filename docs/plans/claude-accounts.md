# Accounts: the AISquare sign-in and the Claude Code accounts, in one page

Status: implemented 2026-09-09 on top of PR #77 (`aisquare login`), which lands
first; §9 (the default, the priority order, aliases — #145) and §10 (headroom,
limits, hand-over — #146) added 2026-09-13.
The user guide is the "Accounts" section of `docs/fleet.md`; the README's
"Several accounts, one team" covers the command line.

This document is a reference, not a script: commands appear as inline code.

## 1. The ask

Anmol switches between three Claude Code logins with `c1`, `c2` and `c3`: three
symlinks to one script that exports `CLAUDE_CONFIG_DIR=$HOME/.claude-<name>`
and `exec`s `claude`. The CLI should own that mechanism: an **Accounts** section
in the fleet UI's sidebar, with the AISquare account on top (the login PR #77
adds) and the Claude Code accounts under it — the default one first, then every
account the user adds as 2, 3, … — each showing who is signed in and how much of
its session limit is used, plus *Add*, *Remove*, and a way to launch a session
on a given account without any alias. Decided with the owner on 2026-09-09:
the AISquare sign-in is a native card, not a pane; removing an account keeps its
directory under a `.removed` name; the CLI never adopts a directory it did not
create — only CLI-made slots exist, for consistency.

## 2. The one idea

A Claude account **is a directory**. Claude Code keeps a login inside the
directory `CLAUDE_CONFIG_DIR` names (`~/.claude` when unset), so a second
account is a second directory and a launch that points the variable at it.
`core.claude_accounts` owns such directories:

```text
~/.aisquare/claude-accounts/<n>/                       CLAUDE_CONFIG_DIR of slot n (n ≥ 2)
~/.aisquare/claude-accounts/<n>/.aisquare-account.json the marker: slot, created_at, created_by
~/.aisquare/claude-accounts/<n>.removed-<stamp>/       a removed slot: kept, never listed
~/.aisquare/cache/claude-accounts/<n>/                 CLAUDE_CODE_TMPDIR of slot n
```

- **Slot 1 is never a directory of ours.** It is whatever a plain `claude` is
  in the shell `asq` was started from — `CLAUDE_CONFIG_DIR` if set, `~/.claude`
  otherwise — and launching it sets nothing. `CLAUDE_CONFIG_DIR=~/.claude` is
  *not* a no-op: Claude Code keeps the default install's `.claude.json` at
  `~/.claude.json`, beside the directory, and moves it inside only when the
  variable is set, so a "default" launched that way re-onboards into an empty
  config (verified on this machine: `~/.claude.json` exists, `~/.claude/
  .claude.json` does not; every `~/.claude-c<n>/.claude.json` does).
- **Both variables, always.** `CLAUDE_CODE_TMPDIR` goes with the config dir or
  two parallel sessions share one scratch directory (README, "Several
  accounts, one team").
- **The directories are the record.** A slot is a numbered directory carrying
  the marker; identity is read from `.claude.json` (`oauthAccount.emailAddress`,
  `organizationName`, `accountUuid`) and the token state from
  `.credentials.json` (`claudeAiOauth.accessToken`, `expiresAt` in epoch
  milliseconds, `subscriptionType`, `rateLimitTier`). No registry file to drift.
- **Read-only towards Claude Code's files.** Nothing writes either file; an
  expired token is reported, never refreshed — Claude Code refreshes it on the
  account's next session, and a second writer racing the agent is how a login
  gets corrupted.
- **Removal renames.** `<n>.removed-<UTC stamp>` beside the slot keeps the
  login, settings and transcripts for a hand-made restore; the scratch
  directory is deleted; the number is free again and the next add takes the
  lowest gap. Slot 1 refuses.
- This is not the "accounts" cut `core.config.RoleLaunchProfile` records
  deleting: that one expanded a bare name into the *operator's* layout. These
  directories are the tool's own. A role bound with `team bind --env` to any
  other layout is untouched.

## 3. Signing in

There is no non-interactive login in Claude Code (`claude setup-token` mints a
CI token, a different thing). Signing a slot in means running `claude` with the
slot's two variables and letting its first-run flow send the user to the
browser. The login has **landed** when the directory holds both files a signed-in
Claude Code writes (`services.claude_accounts.sign_in_landed`); on macOS the
credentials live in the Keychain, so the identity alone counts there.

- **In the UI** (`cli/ui/views/accounts.py`): *+ Add Claude account* creates the
  slot, spawns a window in the fleet's tmux server (session `asq-accounts`)
  running our own `aisquare accounts run <n>` — so the environment is decided
  in one place — and renders it in a `TerminalPane` on the page. A one-second
  timer polls `sign_in_landed`; the moment it answers, the account is recorded,
  `agents connect claude-code --config-dir <slot>` installs the hooks, the
  window is killed and the pane folds away. A pane that dies first, or *Cancel
  sign-in*, discards a fresh slot (`abandon_sign_in`) and leaves an existing
  one alone. The row's *Sign in* runs the same flow for a slot that has no
  login, slot 1 included.
- **On the command line**: `aisquare accounts add` runs Claude Code in the
  foreground (`services.claude_accounts.run_session`, an excluded, identity-
  stripped seam), the user signs in and leaves it, and the command records what
  landed — or discards the slot and says nothing was added.

## 4. The AISquare card

Reads `services.iam.current_session()`. *Sign in* runs the RFC 8628 device grant
PR #77 implements, as a card: `iam.discover`, `iam.start_device_authorization`,
the code and link painted on the page, `core.browser.open_url` when a browser
can reach the user, then `services.device_flow.wait_for_token` in a thread
worker — the terminal's poll loop with the clock, the sleep and a cancel check
as parameters, raising the same `IamError` codes — and
`services.auth.complete_sign_in` to store the session and retire the previous
one. *Sign out* is `services.auth.sign_out`. An `AISQUARE_TOKEN` session is
shown and cannot be signed out from the page. `cli/auth.py` is untouched; #77's
files are not edited by this work.

## 5. Usage

`GET https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer
<accessToken>` and `anthropic-beta: oauth-2025-04-20` — what Claude Code's own
`/usage` reads. It answered on 2026-09-09 (Claude Code 2.1.266) with
`five_hour.utilization`, `five_hour.resets_at`, `seven_day.utilization`,
`seven_day.resets_at` and more; the recorded shape is `LIVE_USAGE` in
`tests/test_claude_accounts.py`. It is **not a documented API**, so it is best
effort by construction: a token already expired is not sent; a 401, another
status, non-JSON or a payload without the two windows becomes a row that says
`usage unavailable` with the reason, and nothing else on the page depends on it.
The UI fetches once a minute while the page is on screen and never elsewhere;
`accounts list` is offline unless `--usage` is passed; no hook or session path
reaches the endpoint (`tests/test_no_network_on_the_primary_path.py` still
holds).

## 6. Surface

- `aisquare accounts` → `list [--usage]` (bare group lists), `add`, `run <slot>
  [claude args]`, `usage [slot]`, `remove <slot>`. Reporting commands take
  `--json`; `add` and `run` refuse it (their stdout is Claude Code's) and `add`
  refuses outside an interactive terminal. A slot is a number or the email it
  is signed in as.
- `aisquare launch <role> --account <slot>` and `aisquare fleet spawn <role>
  --account <slot>` set the two variables over the role's binding; the board
  labels such sessions `account N` (`services.team.account_label`).
- `aisquare doctor` gains `claude-accounts`: one line naming each added slot and
  whether it is signed in; reads only, creates nothing.
- Sidebar: an **Accounts** section above Doctor (`✓ AISquare · 2 Claude`, one
  detail line: the worst thing, or who is signed in). Click → the page. The
  shell re-reads the accounts on its two-second tick (small JSON files) and the
  page refreshes usage on its own minute.

## 7. Files

New: `core/claude_accounts.py`, `services/claude_accounts.py`,
`services/device_flow.py`, `cli/accounts.py`, `cli/ui/views/accounts.py`,
`tests/test_claude_accounts.py`, `tests/test_ui_accounts.py`, this document.
Changed: `models.py` (the account models), `core/paths.py` (the two
directories), `core/spawn.py` (two excluded seams), `cli/launch.py` and
`cli/fleet.py` + `services/fleet.py` (`--account`), `services/team.py`
(`account_label`), `services/diagnostics.py` (the doctor line),
`cli/ui/sidebar.py` and `cli/ui/app.py` (the section and the wiring),
`cli/app.py` (the group), the README, `docs/fleet.md`, the CHANGELOG, and the
test censuses (`tests/test_stubs.py`, `tests/test_no_traceback_on_a_damaged_store.py`,
`tests/test_documented_commands.py`'s sentinel flag).

## 8. Later

- ~~A spawn that picks the account with the most headroom~~ — done in §10
  (#146): `[accounts] pick = "headroom"`, and `fleet switch` for the running ones.
- ~~The Settings tab could bind a default account per role~~ — done in §9
  (#145): the account select beside each role, `team bind <role> --account`.
- Reading the macOS Keychain for usage.

## 9. Choosing one: default, order, aliases (#145, 2026-09-13)

**The gap.** Several accounts could be added and seen, and none chosen. Slot 1
was the default by constant (`DEFAULT_SLOT = 1`), the order was the slot
number, and a role ran elsewhere only through a `CLAUDE_CONFIG_DIR` buried in
`team bind --env` — which the UI never showed and one typo of which started an
unauthenticated Claude.

**The record stays the directories; the arrangement goes to SQLite.** §2's
argument against a registry file — a second copy of facts the filesystem
already holds — still stands for *which accounts exist and who is signed in*,
and that is still read from disk every time. What a directory cannot carry is
how the operator ARRANGED them: a name, a rank, a choice. Those live in the
`claude_account` table (`core.store`, v15: `slot, config_dir, alias, position,
is_default, disabled, created_at`), joined to the directories by `slot`. The
two are reconciled on every read (`services.claude_accounts._arranged`),
directories winning: a slot with no row gets one at the end of the order, a
row whose directory is gone is dropped. That is also the migration: an existing
machine is the "no rows yet" case, and its first read arranges the slots in
slot order, which is the order they were always listed in. Two invariants are
the schema's rather than a caller's — at most one default and no duplicate
alias, both partial unique indexes — because both had been left to callers
elsewhere in this repo and both drifted.

**The ladder.** `services.claude_accounts.choose` is the one place a launch's
account is decided, and `aisquare launch`, `fleet spawn` (and so a manager
spawning a coder) both ask it: the `--account` flag, else the role's binding
(`RoleLaunchProfile.account`, written by `team bind <role> --account` and the
Settings tab), else the project's default (`project_setting` key
`claude_account`, a slot number), else the machine default (`is_default`), else
**nothing** — and "nothing" means the launch environment is left exactly as it
was, byte for byte, so a machine that never arranged anything is unaffected. A
rung naming an account that does not exist refuses the launch with the rung
named (a binding to a removed slot must not quietly run somewhere else); a
rung naming a `disabled` account is skipped with a note and the ladder goes
on. `tests/test_one_account_resolver.py` pins structurally that neither
launching module decides an account any other way, with positive and negative
controls (CONTRIBUTING, "Writing a guard that still guards").

**References.** `--account` takes a slot number, an alias or a signed-in email
everywhere. The three cannot collide: an alias must start with a letter and
cannot contain `@` (`core.normalise_alias`, lowercase, ≤ 32). The project
default is stored as the resolved slot number; the role binding is stored as
typed, so renaming an alias changes what it means the way the operator expects.

**Removal and reuse.** `remove` drops the row and every project default that
named the slot, after the rename — the number is free again the moment the
directory moves, and a default or alias left behind would be inherited by
whatever `add` puts in that slot next. Role bindings in `config.toml` are not
edited by a removal (a file people hand-edit is not ours to rewrite as a side
effect); `doctor` reports them as dangling and the launch refuses with the rung
named.

**Slot 1 is "plain claude" now.** It was labelled `default`, and once "default"
meant the chosen account, a slot that was not the default could not keep the
word. The label is the alias when one is set.

**Failing open.** Readers (`list_accounts`, `resolve`, `choose`) fall back to the
directories' view with a note when the store cannot be opened — the Accounts
page and `accounts list` keep working on a wedged `context.db`, and a launch
starts (on no default) rather than dying; `doctor` reports the cost. Writers
(`set_default`, `set_alias`, `reorder`, `set_disabled`) refuse with
`AccountsUnreadable`, which the CLI reports as `store_unreadable`.

**Surface.** `aisquare accounts default [REF] [--project P] [--role R]
[--clear]`, `alias <slot> <name> [--clear]`, `order <ref>…`, `move <ref>
up|down|top|bottom`, `disable`/`enable <ref>`; `team bind <role> --account A |
--clear-account`; `accounts list` stars the default and lists in priority order;
the Accounts page rows carry ★ *Default*, ↑/↓ and *Disable*; the Settings tab
binds an account per role; the agent header and `fleet ls` show the resolved
slot (`fleet_agent.account_slot`, recorded at spawn). `doctor` gains
`claude-account-default` and `claude-account-bindings`.

## 10. Spending them: headroom, limits, hand-over (#146, 2026-09-13)

**What was measured before anything was built.** Claude Code's own signals,
read from this machine's transcripts and its hooks reference on 2026-09-13:

- A usage limit ends the turn with an API error the transcript records as
  `"error":"rate_limit"`, `"apiErrorStatus":429`, and the rendered text
  `You've hit your session limit · resets 12:30am (America/Toronto)` (the
  weekly one adds a weekday: `resets Mon 12:00am`). Claude Code fires the
  **`StopFailure`** hook instead of `Stop` for it, with `error`, an optional
  `error_details` and `last_assistant_message` (that text); the hook's output
  is ignored. That is the reliable, in-band signal the issue's design listed as
  "unverified"; pane text and the proxy lane are not needed.
- `Notification` types include `quota_auto_resume_fired` (the "Usage limit
  reset — continuing" notice #153 saw) and its `_stale`/`_disabled` siblings.
- Since v2.1.234 Claude Code **waits in the open session and continues by
  itself after the reset** (`autoContinueAtUsageLimit`, on by default for
  interactive subscription sessions — which fleet windows are — but not when
  the reset is more than 24 h away, i.e. most weekly limits).
- `claude --resume <absolute transcript path>` resumes a session by its file
  and keeps its session id; `--fork-session` mints a new one. The status line
  receives per-session `rate_limits.five_hour.used_percentage/resets_at`
  (epoch seconds); hooks do not.

**The three schemes, decided.** The issue compared a threshold hand-over,
a continuous hand-off packet and post-limit recovery. Shipped: post-limit
recovery on the `StopFailure` signal, with the threshold as the *spawn-time*
rule rather than a mid-run switch, and no per-turn packet — `--resume` by
transcript makes the model's own context the packet, and the board-built
hand-off prompt is the fallback when the transcript is not there. Mid-run
proactive switching (stopping a working agent at 85 %) was left out on purpose:
it interrupts work that may finish before the window does, and Claude Code's
own wait covers the short resets; `on_limit = "switch"` is the automatic path
for the long ones.

**The pieces.**

- `[accounts]` in `config.toml` (`core.config.AccountsSettings`): `pick`
  (`default` | `headroom`), `switch_at` (85), `on_limit` (`wait` | `switch`),
  `wait_if_reset_within_minutes` (15). On the Settings tab too.
- `claude_usage` (store v16): every reading made through
  `services.claude_accounts.sample_usage` — the page's minute tick, `accounts
  usage`, `list --usage`, a headroom pick, `doctor --live` — kept a week.
  `usage_trend` rates the latest reading against the oldest of the SAME window
  (same `resets_at`) within an hour; `describe_trend` is the `≈ 40 min to the
  limit` on the row (and `resets before the limit` when the window lifts
  first).
- `headroom_choice`: enabled, signed-in accounts in priority order, minus the
  slot a hand-over leaves; usage read once, concurrently; the first under
  `switch_at`, else the one with the most room; unreadable ones skipped with a
  note; nothing measurable → `None`, and `choose` falls to the machine default.
  It is the `headroom` rung of `choose` (§9), switched on by `pick` or by
  `spread=True`, which `fleet switch` passes so a switch goes where the room is
  whatever `pick` says.
- `hook stop-failure` → `services.hooks.turn_failed` →
  `team.hook_stop_failure`: `rate_limit` parks the row `limited` with the reset
  `core.claude_accounts.parse_limit_notice` read from the message (a clock time
  in the named zone, resolved to the next such moment, on the named weekday),
  emits `limited` ON THE TRANSITION only (Claude Code re-fires for the same
  window), nudges a waiting manager; `limited` and `switched` are wake kinds.
  Any other error: `waiting` + a `turn_failed` feed line. The turn's metrics
  row is closed either way. The sixth hook installed by `agents connect`; an
  older install reads as partial and `doctor` says to reconnect.
- `services.fleet.switch` / `fleet switch <label> [--to A] [--fresh]
  [--reason R]`: target through `choose(to, …, exclude={current}, spread=True)`;
  the agent stopped as `fleet stop` stops it (its `SessionEnd` releases its
  claims); spawned again with the same label, task and worktree on the target,
  `--resume <transcript>` when the file exists (the row records the SAME
  session id up front — `ResumeSpec` — so the board session carries on), else
  a hand-off prompt: who it is, the task and its contract, the previous
  session's last board entries, and the instruction to read the board and the
  tree before continuing. A `switched` event closes the loop.
- The automatic path lives in the hook (`_hand_over_if_configured`): a limited
  FLEET agent, `on_limit = "switch"`, reset farther than the wait window →
  `switch`, which starts and records the replacement before it kills the
  window the hook is a child of. A refusal (no headroom) is a board note and
  the agent stays parked with Claude Code's own wait intact.
- `_derive` trusts a `limited` row until its reset (+10 min) rather than the
  30-minute stale window, because a parked agent fires no hook; `⏳ limited`
  chips in the sidebar, project view and `fleet ls`; `ALIVE_STATES` includes it.
- `doctor`: `claude-account-limits` (offline, the parked agents and their
  resets), `claude-account-headroom` (`--live`, every window against
  `switch_at`; warns when all are over the line or none can be read).

**Unverified, and said so.** Resuming a transcript written under one
`CLAUDE_CONFIG_DIR` from another. The path form is documented and the id is
kept, but this machine holds one login, so the cross-account leg has not been
run; a resume that fails leaves a pane whose error is visible, and `--fresh` is
the documented way around it. Copying transcripts between config directories
was not done, per the issue.
