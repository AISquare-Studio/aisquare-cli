# Handoff P2: capacity, throughput, setup

Status: draft handoff, 2026-10-23. Base: `main` @ b26dd140 (#201 personas, #240 captain, experimental
and off by default, #267 agent info line). PR #272 @ 2d80fd5c (tmux control mode, the 2 s refresh off
the UI thread) lands first; every `cli/ui/app.py`, `cli/ui/terminal.py` and `core/tmux.py` reference
below is #272's version and says so. Branch: `rc/p2-capacity-setup`. Committed path:
`docs/plans/handoff-p2-capacity-setup.md`. Commands appear in `text` fences, proposed and existing
alike, so `tests/test_documented_commands.py` does not read them as verified scripts.

> **Base update (2026-10-23).** #272 merged at 362dd5b0 and #273 refreshed `uv.lock`; `main` is now 09f48563. References marked (#272) are to `main`. Other `file:line` references were taken on b26dd140 and still hold on `main` (#272 changed only `cli/ui/*`, `core/tmux.py`, `core/tmux_control.py` and `core/spawn.py`, whose references here are already updated).

**What releasing this train means.** Spawns are admitted on memory headroom and a fleet-wide seat
budget, finished workers give their seats back without a manager turn, and heavy test runs wait for
memory instead of swapping the box. One plain process (`aisquare watch`) watches panes and PRs and
wakes managers only on new items, so they stop paying for their own watch loops. `asq` costs the same
per tick with 50 projects as with 5. `doctor` finds a stale `ANTHROPIC_BASE_URL` wherever it hides and
one command removes it. Agents get a symbol index (`aisquare code find|refs|similar`) before grep. A
parent manager hears its group's children in seconds instead of on its next poll. A session handed
to another account keeps its memory and skills.

## How to run this train with agents

| ID | Title | Issues | Size | Depends on | Parallel-safe? | Hot files |
| --- | --- | --- | --- | --- | --- | --- |
| W1a | Memory-aware admission, fleet-wide seat budget, test-run gate | #283 | M | none | yes, except W6 | `services/fleet.py` `spawn`/`_verify_cap`, `core/config.py`, new `core/memory.py`, `cli/fleet.py` |
| W1b | Auto-stop finished workers | #283 | S/M | W1a; P1 WS-2 for event semantics | yes, after W1a | `services/hooks.py` `turn_stopped`, `cli/hook.py`, `services/fleet.py` `reap`, `core/harness.py` |
| W2 | `aisquare watch`, one non-AI watcher | #284 | M | P0 W2 (delivery guards); W1b optional | yes (new modules) | new `services/watch.py`, `services/github_prs.py`, `cli/watcher.py`; `services/captain/queue.py` |
| W3 | `asq` tick cost vs listed projects; tmux conf residuals | #83 (+#272, #100/#101 residuals) | M | #272 merged | yes | `cli/ui/app.py` (#272), `services/fleet.py` `list_agents`, `core/tmux.py` (#272), `cli/fleet.py` `ui` |
| W4 | Stale `ANTHROPIC_BASE_URL`: doctor, Disable, reset; root post off the launch path | #51 (+#129(e)) | M | none | yes | `services/explainability_ops.py`, `services/explainability.py`, `cli/explainability.py`, `cli/ui/views/explainability.py`, `services/diagnostics.py`, `cli/root.py` |
| W5 | Code intelligence phase A: symbol index, `aisquare code`, MCP tool | #285; #91; #92 (B, stretch); #94 (C, next train) | L | P1 WS-3 (same `_directive`) | yes | new `core/code_index.py`, `cli/code.py`; `services/hooks.py`, `services/mcp_server.py` |
| W6 | Parent manager over a project group (event routing) | #199 | L | W1a; P0 #122/#74; P1 WS-2 | no, land last | `services/team.py`, `core/store.py` (one migration), `services/project_groups.py`, `core/harness.py`, `cli/project.py`, `cli/task.py` |
| W7 | Accounts share memory/skills/agents; verify cross-account resume | #198 | M | coordinate with P0 W3 | yes | `core/claude_sharing.py`, `core/claude_accounts.py`, `cli/accounts.py`, `cli/launch.py`, `services/team.py` |

**Order.** (1) Land #272, then run W3, W4, W5 (phase A), W7 and W1a in parallel; they share only
`core/config.py` and `CHANGELOG.md`. (2) W1b after W1a. (3) W2 after P0's delivery guard (the
watcher's wake is a `nudge_manager` call, which P0 W2 makes safe while a human types). (4) W6 last:
it needs W1a's accounting, P0's brief delivery (#122, #74) and P1's narrowed wake kinds (WS-2), or
every routed event becomes an expensive wake. (5) Phase B of W5 (#92) starts only when phase A is
merged and a coder is free; it is not a release gate.

**Hotspots.** `services/fleet.py` (9,113 lines): W1a (`spawn` :2982, cap :3208-3213, `_verify_cap`
:4346-4366), W1b (`reap` :8551), W2 (`nudge_manager` :8991, read only), W3 (`list_agents` :4569), W6
(`pause` :8798); P0 rewrites `type_then_enter` and `tell` there too. `services/team.py`: W6 (event
append, `_nudge_target` :641), W7 (`session_account` :558), P0 (`hook_stop`), P1
(`MANAGER_WAKE_KINDS` :133). `services/hooks.py`: W1b (`turn_stopped` :195), W5 and P1 WS-3 (both
`_directive` :501), P0 #268 (`capture_prompt` :432). `core/config.py`: add fields at the end of each
class and of the defaults dict (:470-520). `core/store.py` ladder (version 33, :1026-1061): only W6 adds a
migration; take the next free number at merge. `docs/fleet.md` `[fleet]` block (:1061-1068).

**Agents.** One coder per workstream in its own worktree (W1a and W1b may be one coder in turn). One
reviewer per PR, spawned when the PR exists and briefed with this section and the workstream's.
Reviewers check, in order: the failing-first test fails on the base; macOS and Linux branches (and
WSL where noted); every new subprocess call registered in `core.spawn.SEAMS`
(`tests/test_spawn_seams.py` walks the AST); no new runtime dependency without the owner's yes;
config keys documented; `--json` honoured; the fence rule in every changed `.md`.

**Definition of done (every workstream).** Production-ready, not "code exists": the reported
failure is reproduced by a test that fails without the change (CONTRIBUTING's "prove a test can
fail"); macOS + Linux (and WSL where the area is platform-sensitive: W1 memory probes, W3 tmux);
`make check` green (ruff, mypy strict, pytest on 3.11–3.13); user docs updated under `docs/` (README
stays under 250 lines; 149 today); a CHANGELOG `[Unreleased]` entry in the repo's existing voice;
config keys documented in `docs/reference.md` (and the `docs/fleet.md` defaults block); new commands
added to `IMPLEMENTED` in `tests/test_stubs.py`; services return data, the CLI prints (honour
`--json`); measured before/after numbers where the item is about tokens, latency or minutes.

## W1: Resource-aware capacity (#283)

### Problem

- **Admission ignores the machine.** The only limit is a per-project count. It knows nothing about
  available memory, so agents and their test runs can push the box into swap while every project is
  under its cap; and it knows nothing about other projects, so a busy project waits at its cap while
  seats sit unused elsewhere. Heavy test runs (pytest, builds) start whatever the memory state.
- **Finished workers keep their seats.** A tester, reviewer or validator that has delivered its
  verdict sits at the prompt, holding a seat, a Claude Code process and its memory, until a manager
  spends a turn on `fleet stop` and `fleet reap` (observed in multi-agent use).

### Baseline to measure first

On a scratch fleet with two projects at the default `max_agents_per_project`, for one working day:
(1) sample live agents per project once a minute (`aisquare fleet ls` in each project) and count the
minutes each project sat at its cap while another was under its own; (2) from `context.db`, sum per
day each finished worker's `ended_at` minus its session's last `result` or `task_done` (idle
seat-minutes); (3) record available memory and pressure at each spawn (`/proc/meminfo` and PSI on
Linux, `vm_stat` and `kern.memorystatus_vm_pressure_level` on macOS). These are the PR's before
numbers.

### Where the code is today (b26dd140)

- The only limit is `max_agents_per_project = 4` (`core/config.py:628`, defaults :495,
  `FleetSettings` :614-632). `spawn` refuses at the count (`services/fleet.py:3208-3213`) and settles
  races after the row is written (`_verify_cap` :4346-4366, ordered by `created_at, id`). No
  fleet-wide count, although `store.live_fleet_agents()` exists (`core/store.py:3970`).
- No memory reading anywhere in `src/` (no psutil, `MemAvailable`, `vm_stat`). A `sysctl` seam exists
  (`core/tmux.py:409-423`, registered in `core/spawn.py:392-397`).
- Stopping a finished agent is the manager's manual step: "An agent whose task is done, with no
  queued task that fits it: `fleet stop <label>`, then `fleet reap`" (`core/harness.py:1052-1054`).
  Testers and reviewers end with "if nothing, tell the user and stop" (:1002-1004, :1093-1094) and
  sit at the prompt. `FleetSettings` has no idle setting.
- To reuse: `stop(..., nudge=False)` (`services/fleet.py:5123-5174`); `CLOSED_STATUSES` and
  `_closed_tasks` (:8377); `_derive` (:2438); the hand-over's detached worker (`_detach`,
  `services/hooks.py:328`; hidden `aisquare hook hand-over`, `cli/hook.py:232`).

### What to build

**W1a. Admission.**

1. `core/memory.py` (new): `headroom() -> Headroom(available_mb, total_mb, pressure, psi_some_avg10,
   source)`, `pressure` one of `normal | warn | critical | unknown`.
   - Linux/WSL: `/proc/meminfo` (`MemAvailable`, `MemTotal`) and, when present,
     `/proc/pressure/memory` (`some avg10`; warn at ≥ 10, critical at ≥ 40). No subprocess.
   - macOS: `sysctl -n kern.memorystatus_vm_pressure_level` (1 normal, 2 warn, 4 critical; no root),
     `hw.memsize`, and `vm_stat` (free + inactive + speculative pages × page size, psutil's macOS
     `available`). Move `_sysctl` into `core/memory.py` (update its `SEAMS` key) and register
     `aisquare/core/memory.py::_vm_stat` (`EXCLUDED`, `strips_identity=True`).
   - Elsewhere `unknown`: the gate admits and says so.
2. Keys:

   ```toml
   [fleet]
   max_agents_total = 0          # live agents across every project, captain excluded; 0 = no budget
   min_free_mb = 2048            # refuse a spawn below this much available memory; 0 = off
   test_gate_mb = 0              # `fleet gate` floor; 0 = min_free_mb
   test_gate_wait_minutes = 15
   ```

3. `spawn`, after the per-project check: refuse at `max_agents_total` naming each project's count;
   refuse when `available_mb < min_free_mb` or `pressure == "critical"`, naming both numbers and
   `aisquare fleet capacity`; a receipt note at `warn`. After a memory refusal, require
   `min_free_mb + 512` for 5 min (a meta key) so admission does not flap. `_verify_cap` settles the
   fleet-wide count as it settles the project count. JSON errors `capacity_project`,
   `capacity_fleet`, `capacity_memory`.
4. `aisquare fleet capacity [--json]` (`services.fleet.capacity() -> CapacityReport`): headroom,
   pressure, live counts against each cap, and the workers W1b would stop.
5. `aisquare fleet gate [--min-free-mb N] [--wait 15m] [--json] [-- CMD …]`: polls every 5 s; with
   headroom exits 0 or `exec`s `CMD`; at the deadline exits **75**, `not run: <available> MB
   available, <N> MB needed for 15m`. Tester and validator cycles name it only when
   `test_gate_mb > 0`.
6. Doctor row `fleet capacity`: ok with the numbers, warn below the floor or at warn pressure.

**W1b. Auto-stop finished workers.**

1. `finished(status, store) -> str | None` (the reason): role in `auto_stop_roles` (never manager or
   captain); derived state `waiting`; idle ≥ `auto_stop_idle_minutes` by both `last_seen_at` and the
   pane's last output; no claimed task; its spawned task closed, or, task-less, at least one `result`
   written since spawn; no unread note addressed to its label; no queued task `task next --role
   <role>` would hand it; an empty input box (P0's input-box probe), so a human's draft is never
   killed.
2. Three triggers, no daemon. (a) The Stop hook (`turn_stopped`) of an auto-stoppable fleet agent
   whose predicate holds bar the idle clause starts `aisquare hook auto-stop <session> --after <s>`
   detached; the worker sleeps, lists the agent again and calls `stop(project, label,
   agent_id=row.id, nudge=False)` only if `finished` still holds; a later Stop supersedes it (meta
   `autostop:<session>`). (b) `aisquare fleet reap --finished [--dry-run] [--json]`, which W2 calls
   each tick. (c) A W1a refusal with `auto_stop_finished` on stops finished workers first and
   retries once.
3. The event is `agent_exited` with `actor: "auto-stop"` and the reason; info-only for the manager
   (P1 WS-2; until then `nudge=False` wakes nobody).
4. Keys: `auto_stop_finished = true`, `auto_stop_idle_minutes = 5`,
   `auto_stop_roles = ["tester", "reviewer", "validator", "ui-tester"]` (coders opt in: a coder in
   review may be reopened). The manager cycle (`core/harness.py:1052-1054`) says so in one line.

### Acceptance criteria

- [ ] `min_free_mb = 2048` with fake headroom 1,500 MB: `fleet spawn` refuses before any window
      opens, exit 1, `--json` error `capacity_memory` with both numbers.
- [ ] `max_agents_total = 3`, 2 live in A: one spawn in B succeeds, the next is `capacity_fleet`; N
      parallel spawns at budget − 1 settle to exactly the budget.
- [ ] macOS pressure 4 and Linux `some avg10=55.0` refuse; pressure 2 adds a warning.
- [ ] `fleet gate --wait 1s` with low memory exits 75 and runs nothing; with headroom it returns
      `CMD`'s exit code; `--json` prints one object.
- [ ] A reviewer that posted its verdict and idles 5 min with no claim is stopped once,
      `actor=auto-stop`, manager not nudged; a draft in its input box, an unread `--to` note,
      `attention`/`limited`, a claim or a queued task each prevent it; a new turn cancels it.
- [ ] `fleet reap --finished --dry-run --json` lists candidates with reasons and stops none.

### Tests, docs, measurement

Tests: `tests/test_memory_headroom.py` (new; fixture `/proc` files, `vm_stat` output, sysctl values,
missing PSI); `tests/test_fleet_service.py` (failing-first: a spawn at fake 1,500 MB with
`min_free_mb = 2048` succeeds on b26dd140; budget across two projects; the race);
`tests/test_fleet_cli.py` (`capacity --json`, `gate` exit 75 with the injected `_sleep`/`_monotonic`,
`services/fleet.py:167-168`); `tests/test_fleet_autostop.py` (new; predicate table, re-check,
supersession, no nudge); `tests/test_hooks.py` (arms for a reviewer, never a manager);
`IMPLEMENTED`: `("fleet", "capacity")`, `("fleet", "gate")`; `tests/test_spawn_seams.py`.
Docs: `docs/fleet.md` new "Capacity" subsection under "Permission modes and every default" (:959),
spawn refusals (:252), `fleet reap` (:615), the defaults block; `docs/reference.md`;
`docs/plans/fleet-tui.md` §7.4; CHANGELOG.
Measurement: one JSON line per refusal and per admission at budget in
`~/.aisquare/fleet/admission.jsonl` (rotated at 1 MB): capped minutes per project per day, against
the baseline above. Idle seats: `ended_at` minus the session's last `result` or `task_done`, summed
per day from `context.db`, against the baseline; expected residual `auto_stop_idle_minutes` per
finished worker.

### Risks and open questions for the owner

- Default `min_free_mb`: 2048 or 0? 2048 changes behaviour on small machines.
- macOS `available` ignores the compressor, so the pressure level is checked first; WSL2 reads the
  VM's memory, capped by `.wslconfig` (document it).
- Does the budget count managers (proposed yes)? Is `auto_stop_finished` on by default (proposed
  yes, coders excluded)? "Learn from past sessions" (per-agent RSS) is deferred.

### Industry grounding

- Linux kernel, `/proc` docs (https://docs.kernel.org/filesystems/proc.html, undated): `MemAvailable`
  is "an estimate of how much memory is available for starting new applications, without swapping".
- Linux kernel, PSI (https://docs.kernel.org/accounting/psi.html, April 2018): "some" is the share of
  time at least some tasks stall on memory, over 10/60/300 s windows, meant for load shedding.
- GNU parallel `--memfree` / `--memsuspend` (https://manpages.debian.org/testing/parallel/parallel.1.en.html,
  package 20240222): "If less than size bytes are free, no more jobs will be started", and below
  2 × size it suspends jobs: a spawn floor plus a harder second response.
- Jupyter Server config (https://jupyter-server.readthedocs.io/en/latest/other/full-config.html,
  undated): `cull_idle_timeout`, with busy and connected kernels spared by default, swept every
  `cull_interval` (300 s): W1b's "idle and not busy, never while someone is there".

**Linked issues:** #283; #199 (group caps reuse W1a); #129 ("agent cap collision" bucket).

## W2: `aisquare watch`, a non-AI watcher (#284)

### Problem

Managers watch the fleet with their own model turns: a Monitor watch, a `sleep` loop or a `gh` poll
is a turn, and every turn re-sends the manager's whole context, so watching costs as much per check
as working and grows with the session, while most checks find nothing new. A Monitor watch also
expires (5 minutes by default, 30 at most), so it cannot be a long-lived watcher. What is wanted is
one plain process that polls panes every 60 s for dialogs, limits and stalls, and PR heads and
approvals via `gh`, and wakes a manager only on new items, with a count and a file path and no text.

### Baseline to measure first

From one week of manager transcripts on a scratch fleet: input tokens per manager per day, the share
spent in turns whose only tool calls were Monitor, `sleep` or `gh` polling, and Monitor calls per
manager per day.

### Where the code is today (b26dd140)

- "There is no daemon" (`services/fleet.py:1-7`). `nudge_manager` (:8991-9046) types the fixed
  `NUDGE_TEXT` (:129) only into a `waiting` manager reading the news's board, debounced 5 s (:134).
- `RemoteNeedsWatcher` (`services/remote_needs.py:3887`) scans every 3 s (`NEEDS_SCAN_SECONDS`, :103)
  only while `aisquare remote` has a device listening. Its classifier is pure: `scan_needs_you`
  (:2810) over `live_needs_sources()` (:3436), 13 kinds (`NEEDS_KINDS` :86-100), stable ids
  (`needs_item_id` :818). `aisquare remote needs` runs one scan (`cli/remote.py:469`, :4566).
- The captain's queue (#240, `services/captain/queue.py`) has `pr` and `stale` kinds (:70-85), a
  `PullRequest` type (:264-270) and a `Sources.pull_requests` seam, but `StoreSources` plugs in no
  provider (:334-341); `docs/captain.md:374-380` lists the `gh` provider under Phase 2.

### What to build

1. `services/watch.py`: `scan(now) -> list[WatchItem]` composes, without a third classifier: every
   item of `scan_needs_you(live_needs_sources(), …)` with dismissals honoured; `stale` from the
   captain queue's rules; `pr_approved`, `pr_changes_requested`, `pr_checks_done` from the PR
   provider; and operator command sources.
2. `services/github_prs.py`, shared by both consumers: per repo, `gh api
   repos/{owner}/{repo}/pulls?state=open -i -H "If-None-Match: <etag>"` (ETags in
   `~/.aisquare/watch/gh-etags.json`); reviews and checks fetched only when a head SHA changed;
   requests serial. Seam `aisquare/services/github_prs.py::_gh` (`EXCLUDED`, `strips_identity`).
   Plugged into `StoreSources(pull_requests=…)` when `[watch] github = true`, which closes the
   captain's Phase 2 item.
3. Command sources: `[watch.sources.<name>] command = [...]`, `every_seconds = 300`, printing JSON
   lines `{"id", "project", "kind", "summary"}`. A source for an external inbox or message service
   is the operator's own script (for example a `claude -p --model haiku` one-shot); aisquare ships
   no integration code for such services.
4. Delivery: seen ids in `~/.aisquare/watch/state.json` (written whole under a lock, as the queue
   writes `queue.json`). Per project with new items: the full items go to
   `~/.aisquare/watch/<codename>/<UTC stamp>.json` (mode 600); one board event of kind `watch`
   addressed to the manager carries counts and the path only (`watch: 3 new (2 permission, 1
   pr_approved): aisquare watch show <path>`); then `nudge_manager(..., board=<manager's board>)`. A
   working manager gets it at turn end, since an event addressed to it already wakes it
   (`_wakes_manager`, `services/team.py:193-202`).
5. CLI (`cli/watcher.py`; `cli/watch.py` is the board's `--watch`):

   ```text
   aisquare watch [--interval 60] [--once] [--json]
   aisquare watch --detach | --stop | --status
   aisquare watch show [<path> | --last] [--json]
   ```

   `--detach` runs the loop in a window of the fleet's private tmux server (session `asq-watch`) so
   `fleet shutdown` takes it down; one watcher per home by a lock file.
6. Each tick, with `[watch] auto_stop = true`, call W1b's `reap --finished` service.
7. Keys: `[watch] interval_seconds = 60`, `github = false`, `github_every_seconds = 120`,
   `auto_stop = true`, `sources = {}`. The manager cycle gains one sentence: no Monitor or sleep loops
   for panes, CI or PRs; `watch:` events wake you.

### Acceptance criteria

- [ ] `watch --once --json` with an agent at a permission prompt prints one `permission` item; a
      second run prints none; a new prompt is a new item.
- [ ] A waiting manager gets one `watch` event and at most one nudge per tick; a working manager
      gets the event, delivered at its turn end.
- [ ] The event text holds counts and a path, never an excerpt (asserted).
- [ ] `github = true`: requests carry `If-None-Match`; a 304 makes no further call; an approval
      yields one `pr_approved`; `aisquare captain attention` shows the same PR.
- [ ] `--detach` starts one watcher, a second refuses naming the first, `fleet shutdown` stops it;
      a failing source is logged once per streak while the others run; a doctor row shows the last
      scan's age.

### Tests, docs, measurement

Tests: `tests/test_watch.py` (new; fake `NeedsSources` and clock; failing-first: dedupe and the
no-text event), `tests/test_github_prs.py` (new; fake `gh`, ETag, 304), `tests/test_captain_queue.py`
(provider wired), `tests/test_remote_needs.py` as regression, `IMPLEMENTED`: `("watch",)`,
`("watch", "show")`, `tests/test_spawn_seams.py`. Docs: `docs/fleet.md` new "Watching the fleet",
"What runs where" (:1367); `docs/orchestration.md` (long waits); `docs/captain.md` Phase 2;
`docs/remote.md`; CHANGELOG. Measurement: manager input tokens per day and Monitor calls from
transcripts, one week before/after, against the baseline above; scan wall time p50/p95 at 5 and 20
projects; `gh` requests per hour and the 304 share; nudges per manager per hour.

### Risks and open questions for the owner

- P1 WS-2 should list `watch` among wake kinds. Detail files hold pane excerpts (local, mode 600): is
  that enough, or should the redaction level apply? Should `asq` start the watcher (proposed: no,
  opt-in)? Webhooks would be cheaper but need a reachable endpoint: out of scope.

### Industry grounding

- Claude Code hooks reference (https://code.claude.com/docs/en/hooks, undated): `permission_prompt`
  and `idle_prompt` matchers fire inside each agent, and Stop does not fire on a user interrupt; the
  watcher covers what no hook reports (a dead pane, a PR).
- Claude Code tools reference, Monitor (https://code.claude.com/docs/en/tools, undated): every watch
  "has a deadline: 5 minutes by default, at most 30 minutes", so it cannot be a long-lived watcher.
- GitHub, REST best practices (https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api,
  undated): prefer webhooks to polling; a conditional request answered 304 "does not count against
  your primary rate limit"; make requests serially.
- GitHub, REST rate limits (https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api,
  undated): 5,000 requests per hour per user, shared by every loop on that token.

**Linked issues:** #284; #199 (routing to a parent); W1b; captain Phase 2.

## W3: `asq` tick cost scales with listed projects (#83; #272 merged)

### Problem

With many listed projects `asq` lags: keystrokes, window switches and drags trail the input. #272
fixes the input path (one tmux control-mode client) and moves the 2 s read off the UI thread. What
remains is **#83** (filed on 0.6.0 with 305 registered projects): every tick still reads every listed
project, so the worker's read grows with the project count whether or not those projects have agents.

### Where the code is today (#272 @ 2d80fd5c)

- `_read_fleet` (`cli/ui/app.py:303-334`, #272) runs on a worker every 2 s (:791, :923) and calls
  `list_agents(project)` for every listed project; #272 measured ~11 ms a project (:1167-1171), so
  ~550 ms per read at 50 projects, serial.
- `list_agents` (`services/fleet.py:4569-4645`, unchanged by #272) opens a store session per project,
  reads every row (ended too) and every team session, then asks each tmux server. A project with no
  rows still pays the store work. Collapsed groups are still read; `Sidebar.show_projects`
  (`cli/ui/sidebar.py:1377`) paints every card. `aisquare ui` takes no options
  (`cli/fleet.py:1291-1310`).
- Done: `project forget`/`prune` (#85); `[projects] capture` off since 0.8.1
  (`core/config.py:42-53`); the doctor runs once at start (#272 `app.py:931`).
- #100/#101 residuals (both closed 2026-10-23): `set -g mouse off` (#272 `core/tmux.py:145`; main
  :141) sends the wheel to tmux in `fleet attach`; `set -g set-clipboard off` (#272 :149; main :145)
  drops OSC 52 written by a program in a pane (measured on 3.2a in #100). Claude Code's own copies
  go to the tmux paste buffer directly, so the clipboard residual concerns other programs.

### What to build

1. `_read_fleet` reads `store.fleet_label_heads()` once (`core/store.py:3983`, as `bulk_plan` does at
   `services/fleet.py:8320`) and asks for agents only for projects with a live or recently ended
   row; the rest get `[]`, no store session, no tmux call.
2. `fleet_service.list_agents_many(projects)`: one store session, one `_observe_sockets` call over
   every row (each server asked once per tick, `SocketReads` once), the same statuses per project as
   `list_agents`.
3. Collapsed groups show a live count from the label heads and skip the read.
4. `aisquare ui --project <ref>` (repeatable) and `--group <name>` scope the sidebar for the session.
   No navigator config key unless a measurement asks for one.
5. A tick-budget test: 50 projects, 10 with 3 live agents, fake tmux runner; it asserts store
   sessions and tmux calls per read (counts, deterministic), plus a `pytest -s` timing print as #272's
   `tests/test_terminal_control.py` does.
6. Conf: flip to `mouse on` if real-tmux tests show `asq`'s wheel and drag unchanged (`asq` sends
   mouse reports as `send-keys -H` hex over the control client, #272 `core/tmux_control.py:674`) on
   3.2a, 3.4 and 3.8; document Shift/Option-drag for native selection in `fleet attach`. Keep
   `set-clipboard off`; add `[fleet] pane_clipboard = "off" | "on"`. Both apply to a new server.

### Acceptance criteria

- [ ] 50 projects, 10 with agents: one store session per read, each tmux server asked once, no tmux
      call for a project without rows.
- [ ] `list_agents_many` equals `list_agents` per project (property test over existing fixtures).
- [ ] UI-thread time per tick at 50 projects ≤ 10 ms median, recorded in the PR (#272: 2.1 ms at
      4 × 3).
- [ ] `ui --project a --project b` lists only those (plus the captain section when on).
- [ ] With `mouse on`, the wheel scrolls Claude Code in `fleet attach`; #272's mouse tests pass on
      3.2a, 3.4, 3.8.

### Tests, docs, measurement

Tests: `tests/test_ui_shell.py` (#272's, extended; failing-first: a zero-row project makes tmux calls
today), `tests/test_fleet_service.py` (equivalence), `tests/test_ui_tick_budget.py` (new),
`tests/test_tmux.py::test_live_every_bundled_option_is_applied`, `tests/test_terminal_control.py`.
Docs: `docs/fleet.md` `ui` (:719-734), `fleet attach` (:585), Troubleshooting (:1746);
`docs/plans/fleet-tui.md` §3.1; CHANGELOG. Measurement: worker and UI-thread ms per tick at 4, 20 and
50 projects before/after with #272's harness; tmux processes per tick.

### Risks and open questions for the owner

- `mouse on` makes a plain drag in `fleet attach` enter tmux copy mode: acceptable with the documented
  modifier? `pane_clipboard = "on"` lets any pane program set the clipboard, so it defaults off.

### Industry grounding

- tmux wiki, Control Mode (https://github.com/tmux/tmux/wiki/Control-Mode, undated): flow control
  pauses output to a client that falls behind; `no-output` sends no `%output`.
- tmux(1) (https://man.openbsd.org/tmux.1, 20 Oct 2026): `pause-after`, `%extended-output` age (lag),
  and the `set-clipboard` and `mouse` options.
- Textual, Workers (https://textual.textualize.io/guide/workers/, undated): without a worker "the app
  will not be able to process other messages"; `post_message` is the thread-safe path back.
- Claude Code, Fullscreen rendering (https://code.claude.com/docs/en/fullscreen, undated): in tmux,
  wheel scrolling "requires tmux's mouse mode"; copies go to the tmux paste buffer.

**Linked issues:** #83 (rewritten); #272; #100, #101 (closed, residuals here); #129.

## W4: Stale `ANTHROPIC_BASE_URL`, and the proxy on the hot path (#51)

### Problem

- **A stale proxy URL outlives Disable.** Tracing routes an agent through a proxy by setting
  `ANTHROPIC_BASE_URL` (and identity headers). The same variable can also sit in a Claude settings
  `env` block, a role binding (`team bind --env`), an env file, a shell rc or a tmux server's global
  env, for example from an older tracing setup. Disable (the Explainability tab, or
  `aisquare explainability disable`) flips a flag and leaves all of those in place, and the doctor
  does not look for them. When that proxy is gone or serves another deployment, every model call of
  an agent launched afterwards fails with an API error, and the fix is editing files by hand. Needed:
  a Disable that cleans up, a doctor that finds the leftovers, and a one-command reset.
- **#129 item (e):** with tracing on, the proxy buffers and re-uploads each request (#129 records
  `68 ms + 1450 ms × body_MB`, about 2.4 s a call at 1.6 MB), and the launch's Run-root post (3 s
  budget) sits on the pre-exec path (#129: over budget on 2 of 11 launches).

### Where the code is today (b26dd140)

- Disable flips the flag (`cli/ui/views/explainability.py:982-1003`; `cli/explainability.py:993-1055`)
  and warns only when this process exports exactly the target's chosen proxy (:450-462).
- A launch's env is the window's plus the binding's `env`/`env_files` (`cli/launch.py:430-457`);
  `wire_session` stands down when either variable is set (`services/explainability.py:868-890`) and
  warns only on an unparsable URL. A stale, well-formed URL reaches the agent.
- Claude Code applies a settings file's `env` itself; `core/claude_sharing.py` reads `env` only for
  login variables (`LOGIN_ENV` :378-387, `login_keys` :398-404).
- The fleet tmux server starts without the tracing identity since 0.6.0 (`core/spawn.py:358-364`,
  `TRACING_ENV_VARS` :164) and windows `exec` argv directly (`core/tmux.py:1397-1411`), so a shell rc
  does not reach fleet agents; a server started by hand or by a pre-0.6.0 build still can.
- The doctor sees the variable only as model-selection interference (`core/harness.py:776-788`),
  only in its own env, only with the orchestrator enabled and active for the cwd's project
  (`_check_harness`, `services/diagnostics.py:3066-3110`). Nothing reads settings files, bindings or
  the tmux server env.
- **`doctor --fix` turns tracing on** whenever it is off (`cli/root.py:178-182` → `apply_fixes`,
  `services/explainability_ops.py:2380-2414`): the next fix run undoes a Disable.
- #51's original asks exist (`checks` :1456-1506 in dependency order with remedies, `--fix`,
  `sdk_doctor` :1039), so the rewrite repurposes #51.
- The Run root is posted synchronously in `wire_session` (`services/explainability.py:969`;
  `_ROOT_TIMEOUT = 3.0`, `explainability_ops.py:133`).

### What to build

1. `explainability_ops.routing_sources()`, read-only, over: this process; `~/.claude/settings.json`
   and each managed slot's; each listed project's `.claude/settings.json` and `settings.local.json`;
   `[team.profiles.*].env` and env files naming these variables; the fleet tmux server's global env.
   Verdicts: `ours-stale` (a configured target's proxy, the shipped default, or our
   `X-Agent-Name`/`X-Pipeline-Id` headers, with tracing off or another target active), `ours-live`,
   `foreign` (the operator's gateway: info, never changed), `unparsable`. Header values are never
   printed.
2. Doctor row `anthropic routing`, independent of team activation: warn on `ours-stale` or
   `unparsable`, naming each source and its fix.
3. `aisquare explainability reset [--dry-run] [--yes] [--json]`: tracing off (targets kept); remove
   `ours-stale` keys from settings files (backup `settings.json.bak-<stamp>`, atomic write through a
   symlink as `claude_sharing` does, waiting on Claude Code's `<file>.lock`) and from bindings;
   `set-environment -g -u` on the tmux server; print the shell `unset` line and the rc files that
   mention the variables (never edited); re-check. A second run says "nothing to reset".
4. Disable (button and CLI) runs the scan; the UI offers Reset when leftovers remain.
5. `doctor --fix` enables tracing only with `--target` (owner decision below).
6. #129(e): `wire_session` returns at once; the root post moves to a detached worker started at
   launch or to `hook session-start`, which already records the trace join
   (`services/hooks.py:45`); the gateway holds child batches until a root exists
   (`explainability_ops.py:1336-1343`). Docs gain the overhead numbers and the `[explainability]
   roles` trade-off (drop `coder`). Streaming and a pooled HTTP/2 client belong to the SDK's proxy.

### Acceptance criteria

- [ ] A slot's `settings.json` with `env.ANTHROPIC_BASE_URL` at a configured target's proxy makes
      `doctor` warn, naming the file and `aisquare explainability reset`, with the orchestrator off; the same
      for a binding's `env`, an env file and the tmux server env.
- [ ] A foreign gateway is info and survives `reset`.
- [ ] `reset --dry-run --json` changes nothing; `--yes` applies with backups; then green; a second
      run is a no-op.
- [ ] `doctor --fix` with tracing off and no `--target` leaves it off.
- [ ] With tracing on, the window starts without waiting on the root post, and the Run still joins.
- [ ] No output, JSON included, contains a `CUSTOM_HEADERS` value.

### Tests, docs, measurement

Tests: `tests/test_explainability_routing.py` (new; failing-first: a stale URL in a slot's settings
is not reported on b26dd140), `tests/test_explainability_ops.py` (`apply_fixes` without a target
leaves tracing off; `test_fix_can_select_the_target_it_enables` still passes),
`tests/test_explainability.py` (no blocking post), the explainability view's UI tests,
`IMPLEMENTED`: `("explainability", "reset")`. Docs: `docs/connecting-your-agents-to-explainability.md`
(rollback), `docs/runbooks/explainability-prod-cutover.md` (both in `DOCUMENTED`: their fenced
commands must be valid at merge), `docs/explainability-tracing-boundary.md`, CHANGELOG. Measurement:
stale sources found by `doctor --json` on machines that have used tracing; launch pre-exec p50/p95
with tracing on (#129 recorded 2 of 11 launches over the 3 s budget); per-response latency with
tracing on vs off from transcripts.

### Risks and open questions for the owner

- `doctor --fix` enabling tracing was #51's design ("`--fix` wires it"): keep it behind `--target`?
- `reset` writes Claude Code's `settings.json`, a file aisquare does not own: two keys, backed up,
  only on `--yes`.

### Industry grounding

- Claude Code, Environment variables (https://code.claude.com/docs/en/env-vars, undated): the
  settings `env` key takes effect "no matter how `claude` was launched"; `ANTHROPIC_CUSTOM_HEADERS`
  is newline-separated `Name: Value`.
- Claude Code, Settings (https://code.claude.com/docs/en/settings, undated): managed, command line,
  project local, shared project, user; an `env` block follows those levels, so the doctor reads all.
- Claude Code, CLI reference (https://code.claude.com/docs/en/cli-reference, undated): `claude doctor`
  validates settings files; `claude auth status` reports `configDirectory` (v2.1.268+).

**Linked issues:** #51 (rewritten); #129 (item (e) moves here); #50.

## W5: Code intelligence, phase A (#285; umbrella #91)

### Problem

Agents find code by grepping and reading whole files. Each lookup costs tool calls and input
tokens, and an agent that misses an existing helper writes another one. There is no symbol table to
answer "where is X defined, who calls it, does something like it exist?", and nothing points agents
at Claude Code's own code-intelligence (LSP) plugins. The curator program (#91, 5 Sep) was approved
as five slices and has not started.

### Baseline to measure first

Five fixed lookup-heavy tasks on aisquare-cli, each run by a coder on a scratch fleet: Grep/Glob
calls, files read and input tokens per task, from the transcripts; and the SessionStart directive's
size in characters.

### Where the code is today (b26dd140)

`core/snapshot.py:1-23` packs a Repomix full pack, compressed skeleton and per-file char-offset index
under `[snapshot] max_tokens = 150000` (`core/config.py:635-662`); the SessionStart directive points
at the skeleton, "read this FIRST" (`services/hooks.py:501-534`; P1 WS-3 fixes its over-budget
announcement there). No symbol table, curator, graphify, tree-sitter or LSP code in `src/`. gbrain is
optional and not installed (`install.sh:85`, `docs/install.md:84-85`; `core/brain.py:1-20`,
`aisquare recall` at `cli/app.py:156`). The MCP server keeps nine tools on purpose
(`services/mcp_server.py:404-452`). Six hooks are installed, none `PostToolUse`
(`docs/reference.md:5-16`).

**Phases.** A (this train): the index below. B (#92 absorbing #93): tiered snapshot with
`core/curator.py`, doc collapse, noise detection, tier 3 cards; stretch; uses A for symbol-level
inclusion. C (#94 absorbing #95): nodes and graph RAG, graphify optional, gbrain code cards, the
attention record (read from transcripts at SessionEnd, since no hook sees tool calls); next train.

### What to build (phase A)

1. `core/code_index.py`; `~/.aisquare/projects/<id>/code/index.db` (SQLite WAL), separate from
   `context.db` (no migration). Tables `file` (path, sha256, size, mtime_ns, lang, parser),
   `symbol` (name, qualname, kind, path, line, end_line, signature, parent), `ref` (name, path, line),
   `meta` (HEAD, parser versions, built_at), and FTS5 over names split into snake/camel sub-tokens.
2. Files: `git ls-files -co --exclude-standard` (`git_env()`), minus `[snapshot] ignore`,
   `DEFAULT_IGNORE` and nested repos (`core/snapshot.py:433`); skip binaries and files over
   `[code] max_file_kb = 1024`.
3. Parsers: tree-sitter with each language's tags queries (definition and reference captures,
   Aider's approach) from an optional extra `aisquare-cli[code]`; Python's `ast` for `.py` with no
   new dependency; otherwise definitions from the Repomix skeleton on disk, refs reported missing.
4. Incremental: re-hash files whose size or mtime moved, re-parse on a changed hash, drop deleted
   files, rebuild all on a parser version change; one builder per project under a lock.
5. Triggers: `aisquare code index [--full] [--status]`; `project onboard` / `--refresh`; SessionStart
   starts a detached incremental rebuild when HEAD or the dirty count moved (never inline; ≤ 20 ms on
   the hook path when fresh).
6. Queries (service returns data; `--json` everywhere):

   ```text
   aisquare code find <name> [--kind function|class|method] [--limit 20]
   aisquare code refs <name> [--defs] [--limit 50]
   aisquare code similar <name|path:line> [--limit 10]
   aisquare code index --status
   ```

   `find`: exact, case-insensitive, prefix, then FTS. `similar`: overlapping sub-tokens and equal
   arity: the DRY check before a new helper.
7. MCP: `code_lookup(query, mode, limit)`, registered only when the project has an index.
8. The directive gains at most two lines naming the commands and "run `similar` before writing a
   helper". A doctor row `code index` (symbols, files, stale, age, parser coverage).
9. Docs recommend the official code-intelligence plugin per language for coders and reviewers (for
   Python `pyright-lsp@claude-plugins-official`, server binary on PATH); plugins are per
   `CLAUDE_CONFIG_DIR`, so every slot needs it (W7).

### Acceptance criteria

- [ ] aisquare-cli `main`: full build < 30 s, one-file incremental < 2 s; `code find spawn` returns
      `services/fleet.py:2982`; `code refs nudge_manager` lists its callers; `code similar _now`
      lists the existing `_now` helpers.
- [ ] Explainability SDK repo (993 files, #91's baseline): < 60 s with the extra; without it Python
      is indexed and the rest reported.
- [ ] Five fixed lookup-heavy tasks: Grep/Glob calls and input tokens per task with and without the
      directive line, in the PR (no fixed target). No network; nothing leaves the machine.

### Tests, docs, measurement

Tests: `tests/test_code_index.py` (new; Python + TypeScript fixture; incremental, deletion,
fallbacks), `tests/test_code_cli.py` (new; JSON shapes), `tests/test_hooks.py` (directive line only
with an index), the MCP server tests (conditional tool), `IMPLEMENTED`: `("code", "find")`,
`("code", "refs")`, `("code", "similar")`, `("code", "index")`, `tests/test_spawn_seams.py`. Docs:
`docs/memory.md` "Symbol index" beside the snapshot (:81-110), `docs/reference.md`, one README line,
CHANGELOG. Measurement: build and incremental times on both repos; per-task Grep/Glob calls and input
tokens; directive characters before/after.

### Risks and open questions for the owner

- Approve tree-sitter as an optional extra, or ship `ast` only? `code_lookup` only with an index?
  Phase B in this train or the next?

### Industry grounding

- Claude Code, Code intelligence plugins (https://code.claude.com/docs/en/plugins/code-intelligence,
  undated): definitions and references "by symbol instead of by text search", with the language
  server binary installed locally; the LSP tool came in Claude Code 2.0.74 (changelog; npm
  2025-12-19).
- Aider, Repository map (https://aider.chat/docs/repomap.html, undated; blog
  https://aider.chat/2023/10/22/repomap.html, 22 Oct 2023): tree-sitter definitions, a graph ranking
  the most referenced identifiers, a token budget (`--map-tokens`, 1k default).
- Anthropic, Effective context engineering (https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents,
  29 Sep 2025): small upfront context plus just-in-time glob/grep, avoiding stale indexes; phase A
  adds a cheaper first step and keeps grep.
- Cursor, Improving agent with semantic search (https://cursor.com/blog/semsearch, 6 Nov 2025): 12.5%
  higher answer accuracy on average; grep plus semantic search does best; larger gains past 1,000
  files.

**Linked issues:** #285; #91 (umbrella, rewritten); #92 (B, absorbs #93); #94 (C, absorbs #95);
#93, #95 (closed, consolidated); #96 (separate).

## W6: A parent manager over a project group (#199)

### Problem

A manager cannot hear past its own board. When one manager coordinates several projects, a note on
a member's board reaches it only when it next polls that board, because every nudge stays on the
board it was written on (`_nudge_target`, below). Projects should be able to route chosen events,
including notes matching a pattern, to a parent manager and wake it. **#199** (owner, 15 Sep): a
senior manager over a group, linked tasks, per-tier caps, subtree pause, an escalation rule.

### Where the code is today (b26dd140)

Groups (#140): `ProjectGroup` is "a management layer only" (`models.py:920-934`); "no other table
carries a group id" (`services/project_groups.py:1-17`); CLI `project group
create|rename|delete|list|add|remove|move` (`cli/project.py:424-536`). One manager per project
(`MANAGER_LABEL`, `services/fleet.py:112`; enforced :3193-3200). A nudge stays on one board:
`_nudge_target` (`services/team.py:641-689`) and `_nudge_manager` (:732) pass the board, and
`nudge_manager` refuses a manager reading another. Pause is per project (`services/fleet.py:8798-8815`,
`cli/fleet.py:1237-1262`); "Spawn nothing while … paused" is advisory text (`core/harness.py:1046-1047`).
No `origin_task_id`, parent or routing anywhere; schema version 33. The captain (experimental) is one per
home and folds every board into its queue (`docs/captain.md:1-30`); it sits above any parent.

### What to build (#199 tier 2; the machine tier stays with the captain)

1. **Parent.** `project_group.parent_project_id` (nullable) and `aisquare project group parent
   <group> <project> | --clear`. The parent is an ordinary project (a coordination directory), not a
   member; its manager is the group's. Boards stay per project; #140's rule holds bar linkage
   columns.
2. **Route up.** After a board write in a member (where `services/team.py` appends and calls
   `_nudge_manager`), `_route_up` appends `routed` on the parent's board (`routed from
   <codename>#<seq> (<kind> by <label>): <first line, 120 chars> — aisquare board --project
   <codename>`) and nudges its manager. Rules on the group: kinds (default: questions to the manager
   or parent, `task_blocked`, `result`, linked-task status), an optional regex on note text,
   and `aisquare note "…" --to parent`. CLI `aisquare project group route <group> [--kinds …]
   [--pattern REGEX] [--off]`. Storm guard: one routed event per child per 60 s, the rest folded into
   a count, plus the 5 s nudge debounce.
3. **Linked tasks.** `team_task.origin_task_id`, `origin_project_id` (same migration); `aisquare task
   add "<title>" --project <member> --origin <task>`; child status changes route up;
   `linked_all_done` when every child is done; the parent closes the origin itself.
4. **Escalation.** With a parent, the member's "blocked twice" line (`core/harness.py:1057-1058`)
   asks the parent (`--to parent`); needs-you and the captain treat it as the owner's only when the
   parent's manager is down or after `escalate_after_minutes` (default 30) without a reply.
5. **Subtree pause.** `aisquare fleet pause --group <name>` / `resume --group`; an agent-issued
   spawn (`--as`) on a paused board is refused, so a parent cannot restart a paused child.
6. **Caps.** `project_group.max_agents` (0 = none), checked with W1a and settled in `_verify_cap`.
7. **Briefing.** One manager briefing parameterised by scope, not a new role: a parent gets a block
   of at most 600 characters naming members, their managers and the delegation verbs.

### Acceptance criteria

- [ ] A member manager's `note --kind question --to parent` lands as `routed` on the parent's board
      in the same write, and a waiting parent manager is nudged (seconds, not at its next poll).
- [ ] `--pattern 'READY|BLOCKED'`: a matching note routes, another does not.
- [ ] 10 children, 50 events in 60 s: ≤ 10 routed events plus counts, ≤ 1 nudge per debounce window.
- [ ] Linked tasks show their origin on both boards; a child done routes up; all done emits
      `linked_all_done`.
- [ ] `fleet pause --group g` pauses parent and members; an agent spawn on a paused board is refused.
- [ ] A group without a parent behaves byte for byte as today; the migration is forward-only and
      converges (`_converge_by_presence`, `core/store.py:4760`).

### Tests, docs, measurement

Tests: `tests/test_team.py` (routing, coalescing, escalation; failing-first: a member's question never
reaches another board today), `tests/test_project_groups.py`, `tests/test_project_groups_cli.py`,
`tests/test_fleet_service.py` (group pause and cap), the store migration tests,
`tests/test_harness.py`, `IMPLEMENTED`: `("project", "group", "parent")`,
`("project", "group", "route")`. Docs: `docs/orchestration.md` new "A manager over a group",
`docs/fleet.md` (pause `--group`, caps), `docs/plans/fleet-tui.md` §7, CHANGELOG. Measurement:
child-to-parent latency (child event → routed event → parent's next turn), routed events and human
escalations per day, before/after on a scratch group of three member projects and a parent.

### Risks and open questions for the owner

- Linkage columns amend #140's "no group id in board tables": agree before coding. Tokens multiply
  with tiers: keep routed text to one line and a pointer. Hard dependency on P0 #122/#74.
  `AISQUARE_TEAM_HUB` stays for cross-repo execution.

### Industry grounding

- Anthropic, Multi-agent research system (https://www.anthropic.com/engineering/multi-agent-research-system,
  13 Jun 2025): orchestrator-worker; multi-agent runs use about 15× the tokens of a single
  conversation; workers return condensed results; coding has fewer truly parallel tasks than research.
- Claude Code, Agent teams (https://code.claude.com/docs/en/agent-teams, undated): "No nested teams:
  teammates cannot spawn their own teammates"; a tree of managers lives outside Claude Code.
- Claude Code, Cross-session messaging (https://code.claude.com/docs/en/cross-session-messaging,
  undated): notice when another session goes idle or exits; loops throttled; a message is never
  consent. A possible later transport.
- langgraph-supervisor README (https://github.com/langchain-ai/langgraph-supervisor-py, archived
  20 Sep 2026): supervisors of supervisors; pass up the last message and forward replies verbatim.

**Linked issues:** #199 (rewritten); #140, PR #171; #122, #74 (P0); P1 WS-2; W1a; W2.

## W7: Accounts share state; verify cross-account resume (#198)

### Problem

Every managed Claude account is an island for memory and tooling: when a session hits its limit and
hands over to another slot, the new slot has none of the project's auto memory, skills, agents,
commands or `CLAUDE.md`. #198 (owner, 15 Sep) records a manual workaround in its original body: link
each account directory's `projects/` and `plugins/` to one directory, which shares memory and lets
`--resume` continue a session on another account. With automatic switching on by default,
hand-overs are routine, so each one should keep what the session had.

### Where the code is today (b26dd140)

Done: automatic switching, default since 0.8.1 (`pick = "headroom"`, `on_limit = "switch"`,
`core/config.py:702-746`); folder trust copied across accounts (`share_trust`, on;
`core/claude_sharing.py:5-27`, `grant_trust` :210); one `settings.json` by link (`share_settings`,
off; :28-33, :425, :483; `aisquare accounts settings`, `cli/accounts.py:799`). `fleet switch` resumes
by absolute transcript path (`docs/plans/claude-accounts.md:263-264`, :312-318), cross-account leg
"Unverified" (:343-347). Not shared: `projects/` (transcripts, auto memory), `plugins/`,
`commands/`, `agents/`, `skills/`, `CLAUDE.md`. The account is inferred from the transcript path
(`session_account`, `services/team.py:558-572`, called :1917, :2011), while the fleet row records the
launch's account (`FleetAgent.account_slot`, `models.py:1091`; `launch_spec` :1094).

### What to build

1. **Account from the row first:** `session_account` uses the row's `account_slot`, then
   `CLAUDE_CONFIG_DIR` in the hook's env (the launch sets it), then the transcript path.
2. **`[accounts] share = []`** and `aisquare accounts share --status | --enable <parts> | --disable
   <parts> [--json]`; parts `memory`, `skills`, `agents`, `commands`, `claude_md`, `plugins`.
   - `memory`, no symlink: managed launches (and slot 1's when enabled) pass `--settings
     '{"autoMemoryDirectory": "~/.aisquare/claude-shared/memory/<project-key>"}'`. The first enable
     merges each slot's `projects/<slug>/memory/` into it (no overwrite; conflicts kept as
     `<name>.slot<N>.md`; `MEMORY.md` merged by line). Outside any `.claude` path on purpose.
   - `skills`, `agents`, `commands`, `claude_md`: move-then-link to the plain claude's, the slot's copy
     kept beside it (`_keep_beside`, `core/claude_sharing.py:462`); `--disable` restores it. Windows:
     junctions for directories, a copy for `CLAUDE.md`.
   - `plugins`: experimental, off (MCP auth and the plugin store are per directory).
   - Transcripts are not shared: a symlinked `projects/` makes every auto-memory write prompt since
     Claude Code 2.1.280, and a shared directory is swept by the shortest `cleanupPeriodDays`. Resume
     keeps the absolute path.
3. **Verify cross-account resume:** `docs/runbooks/cross-account-resume.md` and an `e2e`-marked test
   with two signed-in slots: spawn on A, send a marker, `fleet switch --to B`; check the row keeps the
   session id, where the continued transcript is written, and that the agent recalls the marker.
   Record the Claude Code version and result in `docs/plans/claude-accounts.md`, replacing
   "Unverified"; on failure `--fresh` stays the documented path.
4. **Doctor** `claude-accounts sharing`: parts per slot, broken or foreign links, the memory
   directory, a warning when a slot's `projects/` is a symlink.
5. #198's auto-switch step is dropped (done by #146).

### Acceptance criteria

- [ ] With `memory` on, every managed launch's argv carries the `--settings` JSON for its project,
      and a memory saved on slot 2 loads on slot 3 in the same repo (manual e2e, version recorded).
- [ ] `--enable skills,agents,commands,claude_md` links with copies kept; `--disable` restores them
      exactly; both idempotent; an entry linked elsewhere is refused.
- [ ] With sharing on, the board attributes each session to the slot it launched on.
- [ ] Cross-account resume is verified, or recorded as failing with the version.

### Tests, docs, measurement

Tests: `tests/test_claude_sharing.py` (parts, copies, junction fallback mocked),
`tests/test_claude_accounts.py` (argv), `tests/test_team.py` (row first; failing-first: a transcript
under a shared root is attributed to the wrong slot), doctor tests, `IMPLEMENTED`:
`("accounts", "share")`, an `e2e` resume test. Docs: `docs/fleet.md` Accounts (:735-958),
`docs/plans/claude-accounts.md` §7–§8 and "Unverified", the runbook, CHANGELOG. Measurement: switches
per day and the share resuming the same session vs a fresh hand-off (`switched` events); memory files
per project across slots before/after.

### Risks and open questions for the owner

- `autoMemoryDirectory` via `--settings` and its write approval are documented but untested here;
  the e2e decides (fallback: per-project `.claude/settings.local.json`). `plugins` stays experimental.
  This extends the reversal of the module's read-only stance that `share_settings` began: keep every
  write narrow, reversible, announced.

### Industry grounding

- Claude Code, Memory (https://code.claude.com/docs/en/memory, undated): auto memory at
  `~/.claude/projects/<project>/memory/`, machine-local; `autoMemoryDirectory` "is read from any
  settings scope: user, project, local, policy, or `--settings`".
- Claude Code, Sessions (https://code.claude.com/docs/en/sessions, undated): transcripts at
  `<config>/projects/<project>/<session-id>.jsonl`; `--resume` takes an absolute path; one session
  resumed in two terminals interleaves into one transcript.
- Claude Code, The .claude directory (https://code.claude.com/docs/en/claude-directory, undated): with
  `CLAUDE_CONFIG_DIR` every `~/.claude` path moves; `cleanupPeriodDays` (30) sweeps transcripts, not
  memory.
- anthropics/claude-code#98044 (open, 29 Sep 2026): since 2.1.280 every auto-memory write prompts when
  a profile's `projects/` is a symlink into `~/.claude`; allow rules, hooks and auto mode cannot
  approve it; a real directory outside any `.claude` segment works.

**Linked issues:** #198 (rewritten); #146, #145, #144; P0 W3 (account picking); W5 (LSP plugins per slot).

## Release checklist for the train

- Version: the next minor after P0 and P1 (0.9.x): new commands and keys. CHANGELOG `[Unreleased]`:
  one Added/Changed entry per workstream in the house voice, naming its issue.
- New keys, each in `docs/reference.md` and the `docs/fleet.md` block: `[fleet] max_agents_total`,
  `min_free_mb`, `test_gate_mb`, `test_gate_wait_minutes`, `auto_stop_finished`,
  `auto_stop_idle_minutes`, `auto_stop_roles`, `pane_clipboard`; `[watch] interval_seconds`,
  `github`, `github_every_seconds`, `auto_stop`, `sources`; `[code] enabled`, `max_file_kb`;
  `[accounts] share`. Keys with defaults need no config migration (the file is written sparse).
- Store: one migration (W6); the code index is its own database.
- Owner calls before tagging: `min_free_mb` default, `auto_stop_finished` default, `mouse on`,
  `doctor --fix` and tracing, the tree-sitter extra. `aisquare watch` and account sharing ship
  opt-in.
- No new core dependency. Regenerate `uv.lock` and run `uv run --frozen` once (P0's pyyaml lesson).
- `make check` on 3.11–3.13; manual passes on macOS and Linux, WSL for W1 and W3; W7's e2e on a
  machine with two logins. README under 250 lines.

## Out of scope

The machine-level manager and runtime onboarding (#199 tier 3); multi-machine fleets (P3);
built-in integrations for message services (W2 has command sources); a GitHub webhook receiver;
phase C (#94) and the CI attention endpoint (slice 4, aisquare-ci); streaming in the SDK's proxy;
replacing tmux or a browser UI; per-agent RSS learning; #96's env rung (size S, separate:
`core/config.py:51-52`, :658-660 and :737-738 still say there is no per-key variable, and the captain
added the one-off `AISQUARE_EXPERIMENTAL_CAPTAIN`, `core/experimental.py:33`); adopting existing
Claude dirs (P3).
