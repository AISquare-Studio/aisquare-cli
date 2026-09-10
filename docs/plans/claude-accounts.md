# Accounts: the AISquare sign-in and the Claude Code accounts, in one page

Status: implemented 2026-09-09 on top of PR #77 (`aisquare login`), which lands
first. The user guide is the "Accounts" section of `docs/fleet.md`; the README's
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

- A spawn that picks the account with the most headroom, from the same usage
  numbers.
- The Settings tab could bind a default account per role; today that is
  `team bind coder1 --env …` or `--account` per launch.
- Reading the macOS Keychain for usage.
