# Handoff: rc/fixes-usage-onboard — usage reads that back off, moving before the limit, onboarding large repos

Status: draft handoff, 2026-10-24. Written against `main` @ 09f48563. Branch: `rc/fixes-usage-onboard`.
Path when committed: `docs/plans/handoff-fixes-usage-onboard.md`.

**Build this after `main` settles.** Many PRs are merging, and the P0–P3 trains (#295–#298) touch the
same areas, so this document describes problems, causes and the shape of a fix, not line-level
instructions. Function and file names are pointers as of 09f48563; re-find them before building. Commands
appear in `text` fences or inline, so the documented-commands guard does not read them as scripts.

**What merging this means.** A rate-limited usage read backs off and says so instead of sticking. Every
`aisquare` process and the UI honour the same wait. The Accounts page keeps showing the last good numbers
with their age, and any account can be logged in again from it. An agent can move to another account before
it hits a limit, at a safe point, without the manager writing its own watcher. `aisquare project onboard`
finishes on a large repository without a Node process running out of memory.

---

## Before you start (re-ground on `main`)

1. Read the CHANGELOG `[Unreleased]` section and the merged train PRs. Several pieces this work builds on
   may have landed by then:
   - P0 "account picks and switches" (#277): per-family windows, refusal marks, one switch per limit.
   - P0 verified prompt delivery (#122): the hand-over this work's F2 reuses.
   - P2 `aisquare watch` (#284) and the native code index (#285, `aisquare code find|refs|similar`).
   - P3 statusline usage feed (#287): usage samples ingested from Claude Code's statusline.
2. Re-locate the code named under each item, and check whether any of it already changed.
3. Keep the three items as separate PRs into this branch. They share no files except, possibly,
   `services/claude_accounts.py` (F1 and F2).

Definition of done for each item: a test that fails without the change; macOS and Linux; `make check`
green; docs and CHANGELOG updated; config keys documented in `docs/reference.md`.

---

## F1 — A rate-limited usage read sticks instead of backing off

### Problem

When the usage endpoint answers 429, `aisquare accounts list --usage` and the Accounts page show
"HTTP 429 from the usage endpoint" for that account and keep showing it. The row shows `—` instead of the
last numbers it had. Every new `aisquare` command asks the endpoint again, and `--refresh` asks even when the
previous read just failed. So anything that polls usage (a script, a manager's loop, several terminals)
keeps the account rate-limited, and the operator loses its numbers while it lasts.

Logging in again does not help, because a 429 is not a login problem. Today the Accounts page offers
*Log in* only when a slot has no login, an expired token, or a token refused with 401, so a person who
wants to force a fresh login on a working slot has to run `aisquare accounts run <slot>` and type `/login`.

### Cause (as of 09f48563)

- `services/claude_accounts.py`: `_http_get` returns only the status and body, so the `Retry-After`
  header is lost. `usage()` turns any non-200 other than 401 into a generic
  `HTTP {status} from the usage endpoint` reading.
- Failed reads are remembered only in a process-local dict (`_UNANSWERED`), so the next process asks
  again. `cli/accounts.py`'s `--refresh` forces a fetch and skips even that.
- The UI and CLI render an unavailable reading as `—`, although an older good reading is usually on
  record (the cache serves readings up to five minutes old, `USAGE_MAX_AGE_SECONDS`).
- `cli/ui/views/accounts.py` `needs_login` gates the *Log in* button to missing, expired or 401.

### How to solve it

- **Keep `Retry-After`.** Return headers from the fetch. On 429 (and 503 with `Retry-After`), record
  "rate-limited until T" per account, using the header, or an exponential default when it is absent.
- **Persist it.** Store the wait per account on disk (beside the recorded usage samples, or in the store),
  so every process, the UI and `--refresh` honour it. `--refresh` during a wait answers "rate-limited until
  HH:MM; showing the reading from N min ago" instead of fetching.
- **Show the last good numbers.** On any failed read, render the most recent good reading with its age and
  the reason ("rate-limited until 14:32", "endpoint unreachable"). Never `—` while a reading exists.
- **Picks and hand-overs respect the wait.** The headroom pick uses the last good reading while an account
  is backed off, or skips it with a note. A hand-over's live read (`read_usage`) honours the wait too.
- **Spend fewer reads.** Prefer usage samples that arrive without a fetch (the statusline feed, #287) when
  they are fresh. Document a polling floor for anyone scripting `accounts usage`, and point them at
  `aisquare watch` (#284) instead of a hand-written loop.
- **Log in again from any row.** Offer *Log in again* on every signed-in row (a secondary action or menu
  item), not only when `needs_login` is true. The 429 reason should say plainly that logging in again does
  not clear it.
- **Doctor.** A row that lists accounts currently backed off, and until when.

### Acceptance

- [ ] A 429 with `Retry-After: N` is honoured for N seconds by a second, separate `aisquare` process and by
      `--refresh`. No request goes out during the wait (a test fake counts requests).
- [ ] With a good reading on record, a failed read shows those numbers, their age and the reason; `—` appears
      only when nothing was ever read.
- [ ] The headroom pick behaves predictably for a backed-off account (uses the last reading or skips it, with
      a note), and is tested.
- [ ] *Log in again* is available on a healthy row and opens the same sign-in flow as *Log in*.
- [ ] Docs: `docs/fleet.md` (accounts and usage), `docs/reference.md` (any new key); CHANGELOG.

---

## F2 — Move an agent before it hits a usage limit

### Problem

With `[accounts] on_limit = "switch"`, an agent moves to another account only after its turn has already
failed on a usage limit. The work stops, and the agent resumes elsewhere from a cold start. Operators who
want to avoid that end up writing their own watchers that poll usage and switch agents early, which costs
tokens and usage reads (and feeds F1's rate limiting).

### Cause (as of 09f48563)

- `core/config.py` `AccountsSettings.switch_at` (default 85) only steers which account a new launch picks,
  and colours the row red. Nothing acts on a running agent that crosses it.
- `on_limit` reacts to Claude Code's `StopFailure` (`rate_limit`), which comes after the limit.
- #146 proposed a threshold hand-over at a safe boundary; only the pick and `on_limit` parts shipped.

### How to solve it

- **A setting, off by default for one release.** For example `[accounts] before_limit = "off" | "warn" |
  "switch"` with a threshold (reuse `switch_at`, or a separate `move_at`), and the existing
  `wait_if_reset_within_minutes` logic: a window that resets soon is cheaper to wait out than a cold start
  elsewhere.
- **Act only at safe points.** Check when a turn ends (the Stop hook) or when the agent is idle at its
  prompt, never mid-turn and never mid tool call. One hand-over in flight per agent, with a cool-down so an
  agent does not bounce.
- **Reuse, don't duplicate.** Use the same hand-over path as `on_limit = switch`, including P0's verified
  delivery (#122) and refusal marks (#277) if they have landed. Read usage from what is already cached or
  pushed (F1's persisted readings, #287's statusline samples). Add no new polling loop; if a periodic check
  is needed, it belongs in `aisquare watch` (#284).
- **Tell the board, not the manager.** Record a `switched` event with reason "before limit". The manager is
  woken only if the hand-over fails.
- **Respect pauses and pins.** Skip agents in a paused fleet or group, and agents pinned to an account by a
  role binding or `--account`.

### Acceptance

- [ ] With `before_limit = "switch"` and an agent's account over the threshold, the agent is moved at its
      next turn end or idle point, not mid-turn. A test drives this with fake usage readings.
- [ ] No move when the window resets within `wait_if_reset_within_minutes`, when the target has no headroom,
      or when the agent is pinned or paused.
- [ ] At most one move per agent per cool-down; no extra usage requests beyond the existing cache rules.
- [ ] `warn` mode records the board event only. Docs and CHANGELOG updated, with the default stated.

---

## F3 — Onboarding a large repository: the Repomix snapshot runs out of Node heap

### Problem

On a large repository, `aisquare project onboard` takes minutes and ends with
`snapshot: skipped — the pack failed: FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap
out of memory`. Onboarding itself completes, and agents work without a snapshot, but the step is slow and
fails loudly. It also makes Node (and `npx`) a requirement for a feature most of the value now comes from
elsewhere.

### Cause (as of 09f48563)

- `core/snapshot.py` shells out to Repomix (`repomix` or `npx repomix`), a Node CLI that packs the whole
  repository in memory. The token budget (`[snapshot] max_tokens`) and the skeleton fallback apply only
  after the pack, so a big tree can exhaust Node's default heap before any budget is consulted. The pack
  has a 600 s timeout (`_PACK_TIMEOUT`).
- The installer and doctor treat Node ≥ 22 as needed for "codebase snapshots enabled".

### How to solve it (owner direction: remove Repomix)

- **Replace the snapshot with the native code index.** P2's index (#285: `aisquare code find|refs|similar`,
  the `code_lookup` MCP tool, built detached at onboard) already answers "where is this, who uses it" in
  Python with no Node. Build a small, budgeted repo map from it (directory outline, top-level symbols per
  file, entry points), capped well under one Read call.
- **Remove the Repomix path.** Drop the shell-out, `_PACK_TIMEOUT`, the Node/npx checks in the installer
  and doctor, and the "the pack failed" messages. Clean up old snapshot files on first run, or leave them
  ignored and documented.
- **Keep the session-start directive honest.** Carry over #282's rule: never tell an agent to "read this
  FIRST" for anything larger than it can read. Point agents at `aisquare code find|refs` and the repo map.
- **Rescope the follow-ups.** #92 (phase B, tiered snapshot) should be restated without Repomix. #91's
  umbrella should note the change.
- **Interim, until this ships:** `[snapshot] ignore` can exclude heavy folders so the pack fits, or the
  skipped snapshot can be left as is.

### Acceptance

- [ ] `aisquare project onboard` on a large repository finishes without starting Node and without a
      memory-related failure. Record the timing before and after in the PR.
- [ ] Doctor and the installer no longer require Node for onboarding. Agents still get a budgeted repo map
      and the code-index directive at session start.
- [ ] Existing projects upgrade cleanly (old snapshot artefacts removed or ignored; no error on first run).
- [ ] Docs (`docs/memory.md`, `docs/install.md`, `docs/reference.md`), CHANGELOG with an upgrade note.

---

## Linked issues

- F1: related to #277 (refusal marks), #287 (statusline usage samples), #284 (`aisquare watch`).
- F2: builds on #122 and #277; revisits #146 (closed; its threshold hand-over was not built).
- F3: builds on #285; affects #282, #92 and #91.

## Out of scope

- Moving CI or installs from pip to uv.
- Changing how Claude Code itself stores logins.
- Multi-machine fleets (#293).
