# Handoff: rc/fixes-usage-onboard — usage reads that back off, moving before the limit, onboarding large repos

> **Status, 2026-10-26 (the 0.9.1 audit of this handoff, board task #86): shipped. This file is now a
> record.** Each item went into 0.9.0 or its own PR; the audit checked every "How to solve it" bullet and
> every "Acceptance" line against `rc/0.9.1`, and found one gap, built here.
>
> | Item | Where it shipped | State |
> | --- | --- | --- |
> | F1: a rate-limited usage read backs off | 0.9.0, #408 (merged into #401) | Done, bar the statusline samples (#287) |
> | F2: move an agent before it hits a limit | #432 | Out of this PR |
> | F3: onboarding a large repo, no Repomix | 0.9.0, #399 (the snapshot and Repomix are gone) and the P2 train #297 (the code index, #285, built in #327 and #385) | Done, bar what is decided below |
>
> **What this PR adds.** This status block, and one sentence in `docs/fleet.md` (Accounts, usage): to hear
> that an agent hit a usage limit, run `aisquare watch` instead of a loop around `accounts usage`. F1's
> "Spend fewer reads" asked for it beside the polling floor, which 0.9.0 documented. It is the only gap the
> audit found in code or docs. The test is `test_the_docs_point_a_scripter_at_watch_and_not_at_a_loop_of_accounts_usage`
> in `tests/test_usage_backoff.py`.
>
> **Not built, and why.**
>
> - **Statusline samples (F1, "Spend fewer reads").** #287 is still open and shipped no feed: nothing
>   ingests Claude Code's statusline, so there is nothing for a read to prefer. #287 owns it.
> - **F3's budgeted repo map, and the session-start directive naming `aisquare code find|refs` (F3's
>   "How to solve it" and its second acceptance line).** Decided, not missed. The owner gave the repo map
>   to #92's phase B, the next train (call 22 in `docs/plans/handoff-p2-capacity-setup.md`), and withdrew
>   the directive's two `aisquare code` lines (call 26: the live sample showed no token saving, and 0.9.0
>   cut session start to about 130 characters for a solo repo). Session start names no snapshot and no
>   file to read first, so the "never say read this FIRST" rule holds. Agents learn of the index from the
>   `code_lookup` MCP tool. No `aisquare code map` is built for this either.
> - **F3's "record the timing before and after in the PR".** No large-repository run was recorded. The
>   only numbers are P2's, on this repository: onboard 9.47 s and 5.64 s before, 2.42 to 2.57 s after, with
>   the index built in the background (`docs/plans/handoff-p2-capacity-setup.md`, W5's row).
>
> **For whoever edits the issues** (#92 and #91 still describe the Repomix snapshot as current; this PR
> writes no issue). Proposed wording:
>
> - **#92 (phase B).** Retitle without "a tiered snapshot": *Code intelligence phase B: a budgeted repo map
>   from the symbol index, `core/curator.py` with git signals, tiers 0 to 3, doc collapse and noise
>   detection (absorbs #93)*. Replace "Current state" with: the Repomix pack, `core/snapshot.py`,
>   `[snapshot]` and the doctor's packer rows are gone (#399, 0.9.0); session start names no snapshot
>   (#346) and its two `aisquare code` lines were withdrawn (owner call 26). Restate the output without a
>   pack on disk: tier 0 the repo map and entry points; tier 1 the signatures of every kept source file,
>   read from the code index (#285), not from a compressed pack; tier 2 the bodies of the top-ranked files,
>   read from the checkout, in rank order until the budget is spent; tier 3 one card per surviving doc.
>   Count the budget with an estimate that needs no tokenizer (`tiktoken` is not used any more). Drop
>   "Surfaces: the directive names the tiers" and say how an agent reaches the map (a session-start line,
>   or a command asked for on demand) is the owner's call, with the phase's measurements. The acceptance
>   keeps its numbers (under 150k tokens, byte-identical rebuild, under 60 s without an LLM) and gains: no
>   Node, no process but `git`.
> - **#91 (umbrella).** Add a dated note: phase A shipped in 0.9.0 (#285; `aisquare code find|refs|similar`,
>   the `code_lookup` MCP tool); the Repomix snapshot is gone (#399), so phase B no longer tiers a
>   snapshot but builds a budgeted map from the index (#92); the "read this FIRST" directive is gone too
>   (#346). Update the "Current state" bullets and the phase table's "Tiered snapshot" row to match.

Written 2026-10-24 as a draft handoff, against `main` @ 09f48563. Branch: `rc/fixes-usage-onboard`.
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

> **Status: built in PR #408, which goes into #401 (`feat/versioned-installers`, the 0.9.0 release), not
> into this branch. Whoever builds #400 must not redo F1.**
> The text below is the original brief, kept for the record.
>
> What #408 delivers, against F1's acceptance:
>
> - [x] **`Retry-After` honoured across processes and by `--refresh`.** A 429 (or a 503 with `Retry-After`)
>       records a wait per account on disk (a `ui_state` row, no schema step). The wait is the header, clamped
>       to 1 s–6 h, or 60 s doubling to 30 min when it is absent, and a good read resets it. No request goes
>       out during the wait from any process: the page, `--refresh`, a launch's pick, a hand-over or
>       `doctor --live`. Tests count requests with a fake, including a second process. The wait belongs to
>       the login that was refused, and clearing or replacing it is compare-and-set.
> - [x] **A failed read shows the last good numbers, their age and the reason; `—` only when nothing was
>       ever read.** A window of that reading that has reset since shows as `reset 1h ago`. CLI: a line
>       under the table. Page: the age in the session cell (`▮▮▯▯▯ 40% · 12m`), the reason in its tooltip.
> - [x] **The headroom pick is predictable and tested.** A kept or backed-off reading is never taken for a
>       live one: the pick uses it only inside its own window with no window reset since, and otherwise
>       skips it with a note giving its age. This holds for both a launch and a hand-over. The fleet dot
>       ignores kept readings older than 5 min.
> - [x] ***Log in again* on a healthy row**, the same sign-in flow as *Log in*.
> - [x] **Docs and CHANGELOG:** `docs/fleet.md`, `docs/reference.md` and CHANGELOG. There are no new config
>       keys. `doctor` also gets a `claude-account-backoff` row listing the backed-off accounts.
>
> What's left: **prefer statusline samples** ("Spend fewer reads") is board task #11, blocked on #287, which
> is still open (no feed exists). The other half of that bullet, pointing a scripter at `aisquare watch`,
> was built in #400 (the status block above).
>
> **Related since this was written:**
> - #403: one shared verified TLS context for every HTTPS call, so macOS python.org Python can read usage.
> - #406: Accounts page, where rank 1 is the default and there is no status or default column. Reasons
>   show in the session cell and its tooltip. Read F1's UI wording ("status", `—`) against that layout.

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

> **Status: built in #432, not in this PR.** The text below is the original brief.

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

> **Status: shipped in 0.9.0.** #399 removed the snapshot and Repomix completely (`docs/plans/remove-repomix.md`:
> the packer, its doctor rows, the Node check, `[snapshot]`, and the old packs are deleted once), and the P2
> train's code index (#285, `aisquare code find|refs|similar`, the `code_lookup` MCP tool) answers "where is
> this, who uses it" in Python. The budgeted repo map and the directive lines were decided against (status
> block at the top). The text below is the original brief.

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
