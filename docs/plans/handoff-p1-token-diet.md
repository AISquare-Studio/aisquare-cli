# Handoff: P1 token diet — fewer wakes, leaner context, healthier long-lived managers

Status: draft handoff, 2026-10-23. Base: main @ b26dd140 (includes #201, #240, #267);
#272 (head 2d80fd5c) merged 2026-10-23; nothing here touches `cli/ui/terminal.py`,
`cli/ui/app.py` or the refresh loop, so no workstream depends on it.
Branch: `rc/p1-token-diet`. Doc path once committed: `docs/plans/handoff-p1-token-diet.md`.

> **Base update (2026-10-23).** #272 merged at 362dd5b0 and #273 refreshed `uv.lock`; `main` is now 09f48563. References marked (#272) are to `main`. Other `file:line` references were taken on b26dd140 and still hold on `main` (#272 changed only `cli/ui/*`, `core/tmux.py`, `core/tmux_control.py` and `core/spawn.py`, whose references here are already updated).

Releasing this train means a fleet spends its tokens on work, not on overhead: a manager
is woken only for events it must act on, and each wake carries one short line per event
with the full text one command away. Every role launches with only the tools and skill
listing it uses, without losing the reviewer's safety deny list. A long-lived manager
compacts at quiet points and restarts from a ≤ 20k-char start pack instead of re-reading
its history.

No figure in this handoff is a measured saving. The ranking (§1.2) follows from how often
each overhead is paid (on every request, on every wake, or once per start) and from
public figures in the Claude docs. WS-0 builds the shared ruler and records the baselines
on a scratch fleet (§1.7) before any other workstream claims a saving. Every PR then
reports before/after numbers counted with that one method.

A work order for coding agents: commands are in `text` fences or inline; proposed ones do not exist yet.

---

## 1. How to run this train with agents

### 1.1 Workstreams

| ID | Title | Issues | Size | Depends on | Parallel-safe? | Hot files |
|---|---|---|---|---|---|---|
| WS-0 | Token ledger and baseline run (the shared ruler) | (part of each issue's measurement) | S | — | yes | new `services/token_ledger.py`, `cli/metrics.py`, `core/transcripts.py` |
| WS-1 | Lean role context: tools, skills, safe deny list | #280, #129(f) | M | WS-0 for baselines | yes | `core/config.py`, `core/harness.py`, `cli/launch.py`, `cli/team.py` (spawn), `services/fleet.py` (spawn args) |
| WS-2 | Wake & delta diet | #279 (absorbs closed #214's actor/`--force`) | M–L | P0 landing its `hook_stop`/`nudge_manager` changes | partly (owns `services/team.py` wake/delta code) | `services/team.py`, `services/fleet.py` (stop/exit emits), `core/store.py` (schema step 34), `models.py`, `cli/fleet.py`, `cli/team.py` |
| WS-3 | Over-budget snapshot still says "read this FIRST" | #282 | S | — | yes | `services/hooks.py` (`_directive`) |
| WS-4 | Manager hygiene: quiet-point compaction, start pack, brief builder | #281, #129(c) | L | P0 verified delivery + #123 fix; WS-2 classifier | no (after WS-2) | `services/hooks.py`, `cli/hook.py`, `services/team.py` (session start), `core/transcripts.py`, `cli/task.py`, `core/harness.py` |
| WS-5 | #129 remainder: per-task verification, spawn honours the ladder, pytest-xdist | #129 (rewritten) | M | (d) none; (a) after WS-2's cycle edit | (d) yes, (a)(b) partly | `cli/task.py`, `services/team.py` (`next_task`), `core/harness.py`, `services/fleet.py` (spawn), `pyproject.toml`, `Makefile`, `uv.lock` |

### 1.2 Ranking by impact × effort

Every overhead token is paid on one of three schedules. The schedule decides its weight
more than its size does.

- **Per request.** "Claude Code sends your full conversation with every request", and
  each batch of tool results is one more request ([Claude Code costs](https://code.claude.com/docs/en/costs)).
  Whatever sits in the prefix (system prompt, tool schemas, the skill listing) is paid on
  every request of every agent for the whole session. Whatever is injected into the
  conversation (a delta block, a file an agent was told to read first) is paid once and
  then carried by every later request until the next compaction.
- **Per wake.** A Stop-hook continuation is one more turn. It re-sends the manager's full
  context at least once, plus one request per tool call the wake triggers. The manager
  holds the largest context in the fleet, and nothing on main compacts it before Claude
  Code's auto-compact window (~967K tokens by default on 1M-context models, [model
  configuration](https://code.claude.com/docs/en/model-config)).
- **Per start.** What a session reads at SessionStart or just after it (briefing, recent
  updates, a snapshot it is told to read first) is paid once and then carried like any
  injected text.

Public figures that bound each term:

- Cache reads bill at 0.1× base input, 0.05× on Opus 5.5 / Sonnet 5.5, 0.025× on Fable
  5.1 ([API prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)).
  So a token saved from a cached prefix is worth less in API dollars than a fresh token.
  It still counts in full against context size, attention and plan usage.
- A request that lands after the cache TTL (5 min, or 1 h for a subscription's main
  conversation) re-processes the whole context at the uncached rate ([Claude Code prompt
  caching](https://code.claude.com/docs/en/prompt-caching)). A wake after a quiet spell is
  the expensive kind.
- The skill listing rides every turn and is capped at 1% of the context window by
  default (`skillListingBudgetFraction = 0.01`, [settings reference](https://code.claude.com/docs/en/settings-reference)):
  up to ~10k tokens per request on a 1M-context model.
- Hook output over 10,000 characters goes to a file with a 2,000-character preview
  ([hooks](https://code.claude.com/docs/en/hooks)). The wake reason's 8,000-char budget
  sits under that cap.
- Read returns at most 25,000 tokens per call by default ([tools reference](https://code.claude.com/docs/en/tools-reference)),
  so a larger file takes several reads, each kept in context.

Impact combines how often a cost is paid, by how many sessions, and its size bound:
High = 3, Medium = 2. Effort: S = 1, M = 2, M–L = 3, L = 4. Score = impact ÷ effort.
WS-0 is the prerequisite and is not scored.

| Rank | Workstream | Paid | By whom | Size bound (code or public docs) | Impact | Effort | Score |
|---|---|---|---|---|---|---|---|
| 1 | WS-3 snapshot directive | once per start, then carried | every agent on a repo whose skeleton is over 25k tokens | `ready`: up to `[snapshot] max_tokens` (default 150,000, `core/config.py:518`); `skeleton_only`: more than the whole context | Medium (none on small repos, severe on large ones) | S | 2.0 |
| 2 | WS-1 lean role context | every request | every agent, for its whole session | eight unused tool schemas, plus a listing of up to 1% of context | High | M | 1.5 |
| 3 | WS-2 wake & delta diet | wakes: one full manager context each; delta: once per prompt, then carried | managers (wakes); every agent (delta) | a wake ≥ the manager's whole context, up to the auto-compact window; the delta is unclipped (`_render_delta`) | High | M–L | 1.0 |
| 4 | WS-4 manager hygiene | every manager request (context size); each manager start (re-reading) | managers | manager context per request, up to the auto-compact window | High | L | 0.75 |
| 5 | WS-5 #129 remainder | wall time and model price, not tokens | — | #129's estimates: 18–35 min per run from per-task verification, 5–8 min from xdist | latency | M | not scored |

WS-0's baseline run (§1.7) turns the Impact column into token counts per manager-hour and
per agent-hour. Re-rank in the release PR if the counts change the order.

**Re-scored from the reduced baseline run (WS-0, 2026-10-23; shape and caveats in §1.7).**
The counts keep the order. Measured on 106 requests and 5,666,363 input tokens
(97% cache reads), over 10.5 minutes:

- **WS-1:** each request's prefix, the baseline, was 38,220–40,026 tokens per session.
  WS-1's measured cut on the same Claude Code release (#311) is about 17k per request
  in an interactive `/context` (26.9k → 9.9k for a coder), and 7.1k on a first request
  under `-p`. Over these 106 requests that is about 0.75M–1.8M tokens, 13–32% of the
  run's input. It is still the largest per-request saving: High.
- **WS-2:** the manager took 4 wakes. They cost 346,949 input tokens, 17% of the
  manager's 2.01M, and 2 of the 4 took no action. Two made no tool call at all; the
  other two each ran `fleet tell`, a write. That holds under the board-read rule as
  fixed in review (`team log`, `task next` without `--claim`, a read behind `cd`). 20 delta blocks (21,723 characters)
  were carried for 311,384 characters across later requests. High.
- **WS-3:** this fixture's skeleton is 715 tokens, below the read-first limit, so it
  cost nothing here, by design. It stays Medium: none on small repos, severe on large
  ones (WS-3's own runs).
- **WS-4:** there was no compaction in 10 minutes. The manager's context was 54,441 on
  average and 65,785 at peak, so this scale cannot measure WS-4. It stays High by
  mechanism. The waves below
follow dependencies, so WS-1 and WS-3 ship together in wave 1 whichever ranks first.

### 1.3 Recommended order

- **Wave 1 (parallel, day 1):** WS-0, WS-1, WS-3, WS-5(d). xdist goes first because every
  later coder runs `make check` many times. WS-0's baseline run happens before WS-1 or
  WS-3 merges.
- **Wave 2:** WS-2, once P0's `hook_stop` change (turn-end delivery of `fleet tell` notes)
  and `nudge_manager` change (the draft guard: never type into a human's half-written
  input) are on `main` or on P0's branch. WS-5(a)(b) can follow WS-2's manager-cycle edit.
- **Wave 3:** WS-4. It needs P0's verified prompt delivery (to type `/compact` and confirm
  it), P0's #123 fix (session start must not force `working`), and WS-2's wake
  classifier (a quiet point is one with no wake-class event pending).

### 1.4 File-overlap hotspots

- `services/team.py`: WS-2 (classifier, renderers, delta scope), WS-4 (session-start
  pack), WS-5(a) (`next_task`), P0 (turn-end delivery in `hook_stop` :2083–2149, #74
  briefing lines). WS-2 owns :133–202, :1859–2290 and :3834–3990; the others rebase onto it.
- `core/harness.py` manager cycle (:1028–1060): WS-2 (`fleet stop --as`), then WS-5(a)
  (tester sentence), then WS-4 (state file, rules). No PR rewrites another's sentence.
- `services/fleet.py`: WS-1 (spawn args :3285, replay :3761), WS-2 (`_stop_row`
  :5176–5416, `_end_dead_rows` :4720–4735, emits :7053/:7641, `_emit_exit` :8730), WS-5(b)
  (spawn model), P0 (`type_then_enter` :439, `_type_prompt` :4369). Rebase, don't reformat.
- `core/store.py`: only WS-2 adds a schema step (34). `core/config.py`: WS-1 (role keys,
  the one config-version step, 2 → 3), WS-2 (`board_context`), WS-4 (`quiet_compact_*`).
- Cross-train: P2's auto-stop of idle agents emits `agent_exited` with actor `auto-stop`
  (info under WS-2); P2's `aisquare watch` should wake through WS-2's classifier.

### 1.5 Staffing

One coder per workstream, own worktree, starting from the failing-first test in its
section. One cold reviewer per PR, who checks: the failing-first test fails on b26dd140;
the PR's numbers use §1.7 with raw totals against the baseline run; any agent-facing
renderer change matches the `docs/plans/fleet-tui.md` §7.3 text; docs and CHANGELOG.
WS-4's reviewer also checks that a typed `/compact` cannot land in a human's draft (P0's
draft guard) and that the row reads `waiting` after a quiet compaction.

### 1.6 Definition of done (every workstream)

Production-ready, not "code exists". The failure is reproduced by a test that fails
without the change (CONTRIBUTING's "prove a test can fail"). Works on macOS and Linux,
and on WSL where the area is platform-sensitive (WS-4 types into tmux panes). `make
check` is green: ruff, mypy strict, pytest on 3.11–3.13. User docs are updated under
`docs/`, and the README stays under 250 lines. There is a CHANGELOG `[Unreleased]` entry in
the repo's existing voice. New config keys are documented in `docs/reference.md`. New
commands are added to `IMPLEMENTED` in `tests/test_stubs.py`. Services return data and the
CLI prints, honouring `--json`. Every item here is about tokens, so every PR body carries
before/after numbers from §1.7.

### 1.7 Measuring tokens from transcripts (applies to every workstream)

The ruler is one read-only reader over the Claude Code transcripts the board already knows.
`team_session.transcript_path` is recorded at SessionStart. WS-0 builds the reader. Until it
lands, a PR may use a throwaway script that applies the same definitions.

- **Request cost:** for each `assistant` entry with `message.usage`, input = `input_tokens
  + cache_creation_input_tokens + cache_read_input_tokens`; output separately. Skip
  synthetic entries (`model == "<synthetic>"`), as `core/transcripts.py:69`
  (`first_turn_tokens`) already does.
- **Baseline:** `first_turn_tokens(path)` is the first request's prompt: system prompt,
  tool schemas, skill listing, memory. Its before/after difference for the same role,
  account type and Claude Code version is the per-request saving of WS-1. The **carried
  cost** is that difference × the requests in the session up to the first compaction.
- **Wakes:** a Stop-hook continuation shows in the transcript as the reason text, which
  starts "board update(s) from your fleet arrived while you were finishing this turn"
  (`services/team.py:3935`). Count them, and sum the input of the requests until the next
  turn end. A **no-action wake** is one with no board write by the manager (`team_event`
  rows with its session id between the wake and its next `waiting`) and no tool call
  besides board reads. Cross-check the count with the store: the
  `continuations:<session>:<UTC hour>` keys in `team_meta` (`services/team.py:2166`,
  `continuation_key`) already count every wake.
- **Delta:** count the `<aisquare-team-delta>` blocks and their characters per session. The
  carried cost is the block's characters × the later requests until compaction. Convert
  characters to tokens on a sample with the API's token counter, or report characters;
  never assume a fixed ratio.
- **Compaction:** the context per request over time (sawtooth), the number of compactions
  and their trigger (quiet/auto/idle), and the manager's input tokens per manager-hour.
- **Tool calls:** the count of every tool name in `tool_use` entries, per role. WS-1's
  defaults depend on it.
- **Protocol:** the same task script, the same fleet shape and the same Claude Code version
  on both sides. Report raw totals and per-hour rates, never percentages alone. Attach the
  JSON from `aisquare metrics tokens --json` (proposed, WS-0) to the PR.
- **Baseline run (owned by WS-0, before any other workstream merges):** one scratch fleet
  on b26dd140. Shape: a small fixture repo; one manager, two coders, one tester and one
  reviewer; a fixed script of five tasks added with `aisquare task add`, one of which
  fails review once; at least two hours of wall time, so the manager sees several wakes
  and at least one gap longer than the cache TTL. Record the Claude Code version and the
  account type. Keep the ledger JSON (counts only) and the `context.db` as the train's
  baseline; each workstream re-runs the same script on its branch. WS-3 adds two fixture
  repos: one whose skeleton is between 25k tokens and `max_tokens`, and one over
  `max_tokens`.
- **Baseline as run (2026-10-23, reduced scale by owner decision).** Base `f6110b62` (main
  `09f48563` plus this handoff; no code change), Claude Code **2.1.309**, the default
  logged-in account (Claude Team), Opus 5.5 for every role.
  - Fleet: 1 manager, 2 coders and 1 reviewer, 4 agents, on a scratch `AISQUARE_HOME`
    and a scratch tmux socket (`asqbl`). Claude Code ran with `--setting-sources
    project,local` and a scratch `--settings` holding only the base's six hooks, so no
    user hook and no `~/.aisquare` was touched.
  - Work: a 5-file fixture repo (`textkit`, no remote) and 3 tasks. One task failed
    review once.
  - Time: 10.5 minutes of wall time, 18:02–18:13Z. That falls short of the two hours
    and the gap past the cache TTL asked above; the full run happens once all four
    trains are on main.
  - Kickoff: the manager's goal was typed into its pane by hand. `fleet tell` filed it as
    a note, because the board read the idle manager as `working` (#123, P0).
  - Ledger: `aisquare --json metrics tokens --all` over that home, counts only, is
    attached to the WS-0 PR. The store's `continuations:` key agrees with its 4 wakes.
  - **Known gaps; do not compare against this run for them:**
    - No request came after a gap longer than the cache TTL, so it shows no
      cache-expiry wake.
    - It has no compaction, so it is not a baseline for WS-4's quiet-point compaction
      or its context sawtooth.
    - WS-4 measures against the full run, or a manager run of its own long enough to
      compact.

---

## 2. WS-0 — Token ledger and baseline run

**Problem.** The Definition of Done asks for measured numbers, and §1.2 ranks the work by
mechanism, not by measurement. Without one shared reader each PR measures differently and
the totals cannot be compared or summed. Without one baseline run, "before" means a
different fleet in every PR.

**Where the code is today.** `core/transcripts.py` reads transcripts on hot paths with
bounded head/tail reads; `first_turn_tokens` (:69) sums the first request's usage.
`aisquare metrics show|list` (`cli/metrics.py:34,47`, hidden) reports CI per-turn rows,
not tokens. Wake counts are already in `team_meta` (`continuation_key`).

**What to build.** `services/token_ledger.py` `ledger(sessions, *, since, until) ->
list[SessionLedger]`: requests, input (fresh / cache write / cache read), output, baseline,
wakes, no-action wakes, delta blocks, characters and carried cost, compactions, peak and
mean context, and tool-call counts by name. It streams JSONL line by line and skips lines
that do not parse. `core/transcripts.py` gains `latest_context_tokens(path)` (tail reader;
WS-4 reuses it). The command prints one row per session plus a total; `--json` emits the
ledger objects; add it to `IMPLEMENTED`:

```text
aisquare metrics tokens [--project P | --all] [--session ID] [--since 2h] [--role R]
```

Then run the baseline (§1.7) on b26dd140 and attach its ledger JSON to the WS-0 PR.

**Acceptance / tests.** `tests/test_token_ledger.py` (new; failing-first via
`tests/test_stubs.py`): a fixture transcript built in the test gives the hand-computed
numbers; a truncated last line is skipped; a large file streams in bounded memory. The
WS-0 PR carries the baseline run's ledger (counts only) with the Claude Code version and
fleet shape, and §1.2's table re-scored from it.

**Docs.** `docs/fleet.md` Troubleshooting, `docs/reference.md` row. **Risk:** transcripts
hold prompts; the ledger prints counts, never text.

---

## 3. WS-1 — Lean role context (#280)

**Problem.** Tool schemas and the skill listing are part of every request's prefix, so
each is paid on every request of every agent for its whole session (§1.2). Fleet roles
launch with Claude Code's full built-in tool set, including eight tools that no role
cycle in `core/harness.py` and no aisquare command uses: Artifact, ArtifactComments,
Workflow, ReportFindings, ShareOnboardingGuide, PushNotification, ScheduleWakeup and
SendFeedback. The skill listing (each installed skill's name and description, up to 1% of
the context window by default) rides every turn as well, although no worker cycle asks
for a skill. Today the only way to trim either is a hand-written `--disallowedTools` in
each role's `extra_args`, which has the bug below, and nothing in aisquare says whether
the flag reaches subagents. #129(f): flags a role "always launches with" live in two
places. `launch reviewer` and `team spawn reviewer` get no deny list at all, while
`fleet spawn reviewer` does.

**Baseline to measure first** (WS-0 run). Per role: `first_turn_tokens`, requests per
session, and the call count of every tool name. Once per role: `/context`'s Tools and
Skills rows in a live coder and a live reviewer. The eight-tool default ships only if none
of the eight is called by a fleet role in that run. Skill's call count per role informs
the `skill_listing` default (Q2).

**The bug inside it.** Setting `[fleet.roles.reviewer] extra_args` to trim tools REPLACES
the reviewer's safety deny list. `_overlay` merges tables key by key, but "any other value
replaces" (`core/config.py:891–901`), so a list is replaced whole. Reproduced on b26dd140 with a temp
`AISQUARE_HOME` whose config.toml holds `extra_args = ["--disallowedTools=Artifact
Workflow ReportFindings"]`. `services.fleet.role_settings("reviewer")` returns only that
list, and `_denied_tools(REVIEWER_ARGS) <= _denied_tools(extra_args)` is `False`. The
reviewer then launches with Edit, Write and NotebookEdit. `docs/fleet.md:973` documents
that `extra_args = []` gives every tool. It does not warn that an unrelated deny list has
the same effect.

**Where the code is today.**
- `FleetRoleSettings` (`core/config.py:287–306`): `permission_mode`, `worktree`,
  `extra_args`, `persona`; no tool or skill key, no fleet-wide key. Reviewer default
  `extra_args = list(REVIEWER_ARGS)` (`:335`), `REVIEWER_ARGS = ("--disallowedTools=Edit
  Write NotebookEdit",)` (`:346`). The docstring at `:316–330` claims its subagents cannot
  edit either; no test pins that. `_denied_tools` (`:421`) already parses both spellings
  and value forms: reuse it.
- Spawn takes `role_args = list(role_config.extra_args)` (`services/fleet.py:3285`); the
  window runs `aisquare launch <role> … <role_args> <extra>` (`:3354`); restart replays
  the spec through `replacing_the_former_reviewer_default_first` (`core/config.py:375`,
  called at `services/fleet.py:3761–3765`).
- `launch` builds `[binary, *profile.args, *role_defaults, *ctx.args]` (`cli/launch.py:509`);
  `team spawn` likewise (`cli/team.py:482`). `RoleProfile.default_args`
  (`core/harness.py:98–117`) is the only code-level home (ui-tester's `--chrome`). Binding
  args come from `team bind --arg` (`cli/team.py:858`).

**What to build.**
1. *Tool policy, one home, applied wherever a role starts.* Add
   `harness.tool_policy(role, *, binary, args, config) -> RoleDefaults` beside
   `role_defaults`, and call it from `cli/launch.py` and `cli/team.py` spawn. The effective
   deny set is:
   `RoleProfile.denied_tools` (new; code-level safety: reviewer = Edit, Write, NotebookEdit)
   ∪ `[fleet] deny_tools` ∪ `[fleet.roles.<role>] deny_tools` ∪ every `--disallowedTools`
   already in `args`, minus `[fleet.roles.<role>] allow_tools`. `allow_tools` is the only
   way to hand back a safety-listed tool. It never overrides a `--disallowedTools` typed on
   this command line. Strip every `--disallowedTools`/`--disallowed-tools` occurrence from
   `args` and append ONE `--disallowedTools=<names, space-joined, sorted>`. Do this only
   for the default agent binary (`is_default_agent`), with a note when withheld, exactly
   as `role_defaults` does.
2. *Keys and defaults* (`core/config.py`):

   ```toml
   [fleet]
   deny_tools = ["Artifact", "ArtifactComments", "Workflow", "ReportFindings",
                 "ShareOnboardingGuide", "PushNotification", "ScheduleWakeup", "SendFeedback"]

   [fleet.roles.reviewer]
   deny_tools = []          # added to [fleet] deny_tools and the role's safety list
   allow_tools = []         # removed from the result; the only way to give back Edit/Write
   skill_listing = "names"  # "full" | "names" | "off"
   ```

   Built-in defaults: `[fleet] deny_tools` = the eight tools above, if the baseline run
   confirms no fleet role calls them (owner Q1). `skill_listing` = `"full"` for manager,
   `"names"` for every other role (owner Q2). The reviewer's `extra_args` default becomes
   `[]`, since the deny now comes from `RoleProfile.denied_tools`.
3. *Config migration* (version 2 → 3, the `_MIGRATIONS` ladder at `core/config.py:543`):
   a reviewer `extra_args` entry exactly equal to `REVIEWER_ARGS` is dropped, because it
   is now the role's own. Every other list stays as written. Recorded launch specs need no
   migration: the merge de-duplicates, and a 0.6/0.7 `--restricted` spec still goes
   through `replacing_the_former_reviewer_default_first`.
4. *Skill listing* (`skill_listing`). `"full"`: nothing. `"names"`: pass `--settings
   '{"skillListingBudgetFraction": <min>}'`; Claude Code then keeps every name ("always
   contains every skill name") and drops descriptions least-invoked first. Verify the
   smallest value the current Claude Code release accepts; fall back to
   `skillListingMaxDescChars`. This route also covers plugin skills, which
   `skillOverrides` does not. `"off"`: add `Skill` to the deny token; acceptance is
   `/context`'s Skills row at 0. Do not use `--disable-slash-commands` ("Disable all
   skills and commands") unless proven to keep `/exit` (typed by `fleet stop`,
   `services/fleet.py:5311`) and `/compact` (WS-4) working. Args that already carry
   `--settings` are never merged blindly: note "skill_listing withheld: --settings already
   given" and launch as asked.
5. *Doctor:* one row per role giving the effective deny set and skill-listing mode. A row
   warns if a role's config lists a name the running Claude Code does not know (if Claude
   Code reports unknown names; verify).
6. *#129(f):* tool and skill policy now applies to `launch`, `team spawn` and fleet windows
   alike. `extra_args` stays fleet-only and is documented as such. Moving `extra_args`
   into `launch` too would double the fleet's copy and change replay semantics. That is
   owner Q3; this train does not do it.

**Acceptance criteria.**
- [ ] With `[fleet.roles.reviewer] extra_args = ["--disallowedTools=Artifact"]`, a fleet
  reviewer's argv denies Artifact, Edit, Write and NotebookEdit in ONE `--disallowedTools`
  token.
- [ ] `aisquare launch reviewer` and `aisquare team spawn reviewer` deny Edit, Write and
  NotebookEdit (today they deny nothing).
- [ ] `allow_tools = ["Edit"]` gives Edit back; a `--disallowedTools=Edit` typed after `--`
  still denies it.
- [ ] A non-`claude` binary gets no deny token, and a note says why.
- [ ] A restart of a reviewer spawned on 0.8.2 (spec holds `REVIEWER_ARGS`) launches with
  one de-duplicated token.
- [ ] `skill_listing = "names"` and `"off"` change the measured baseline as §1.7 describes.
  `/exit` still stops the agent gracefully under both.
- [ ] Manual, recorded in the PR: in a reviewer, a subagent asked to Edit reports the tool
  unavailable. The subagents doc states that subagents inherit the main conversation's
  tools and permission rules; this check pins it on the Claude Code version the PR records.

**Tests.** `tests/test_config_layers.py` (failing-first: a layer setting reviewer
`extra_args` keeps the safety deny in the effective launch policy; fails on b26dd140).
`tests/test_role_profile.py`: `tool_policy` merge, de-duplication, both spellings,
`allow_tools`, non-default binary, stable sorted order (byte-identical argv).
`tests/test_launch.py` (one token), `tests/test_fleet_service.py` (spawn, replay),
`tests/test_config.py` (2 → 3 runs once, keeps hand-written lists),
`tests/test_ui_tester_role.py` if `--chrome` ordering moves.

**Docs to update.** `docs/fleet.md` §"Permission modes and every default you can change"
(:959–1126: the reviewer row :973, the `[fleet]` block :1061–1098) and the roles table
(:194). `docs/reference.md` gets the keys. CHANGELOG `[Unreleased]` → Changed: "fleet
roles launch without eight tools no role uses". Fixed: "a reviewer's own `extra_args` no
longer drops its Edit/Write deny list".

**Telemetry.** §1.7 baseline, before/after, per role, on one Claude Code version: the
expected drop is the eight tool schemas plus the skill descriptions, as `/context` sizes
them in the baseline run. Also report requests per session to give the carried cost.
Cross-check once with `/context` (Tools and Skills rows) in a live coder and reviewer, and
paste both in the PR.

**Risks & open questions.**
- Q1: ship the eight-tool deny list ON for every role, the manager included? (Recommended:
  yes, if the baseline shows no calls. The board, `remote` and `fleet tell` cover what
  Artifact, PushNotification and ScheduleWakeup would do.)
- Q2: default `"names"` for workers, `"full"` for the manager?
- Q3: unify `extra_args` into `launch` (full #129(f)) now or later?
- Removing a tool mid-session changes the cache only when tool search is off. At launch it
  is a new prefix anyway ([prompt caching](https://code.claude.com/docs/en/prompt-caching),
  "Denying an entire tool"). A restart that changes the deny set costs one uncached first
  request; say so in the doc.
- Older Claude Code versions may reject unknown tool names in `--disallowedTools`. Verify
  on the minimum supported version, and fall back to "withheld" with a note.

**Industry grounding.**
- [CLI reference](https://code.claude.com/docs/en/cli-reference) (read 2026-10-22):
  `--disallowedTools` "A bare tool name removes the matching tools from Claude's context";
  `--tools` restricts the built-in set; `--settings` overrides keys for one session.
- [Skills](https://code.claude.com/docs/en/skills): descriptions always in context, bodies
  on use; the listing budget "scales at 1% of the model's context window", tunable with
  `skillListingBudgetFraction`/`skillListingMaxDescChars`; `skillOverrides` skips plugins.
- [Subagents](https://code.claude.com/docs/en/sub-agents): subagents "inherit the built-in
  tools and MCP tools available in the main conversation" and its permission rules;
  background subagents keep `Artifact`, so denying it in the parent matters there too.
- Anthropic, [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  (2025-09-29): "bloated tool sets" are a common failure; curate a minimal viable set.

**Linked issues.** #280; #129 item (f) moves here.

---

## 4. WS-2 — Wake & delta diet (#279)

**Problem.** The Stop hook blocks a manager's turn end for every kind in
`MANAGER_WAKE_KINDS`. That set includes `agent_exited` for every exit, clean or not, and
`switched`. A clean exit, an account switch, a restart, or a stop the manager issued
itself therefore each cost one more manager turn, and each such turn re-sends the
manager's whole context (§1.2) to act on nothing. Only done, review, blocked, reopened,
question, result, limited and non-zero exits need the manager; the rest can ride along
with the next real wake. The per-prompt team delta has the same shape: `_render_delta`
prints each event's full text, unclipped, with `[board]` on every line, and the injected
block is then carried by every later request until compaction. The wake reason clips each
line at 400 chars with no pointer, so reading the rest means `aisquare board`, which
re-reads every event. At session start every role sees the last five events, so a
reviewer meant to judge a change cold reads other agents' verdicts first, and
`AISQUARE_TEAM_DELTA` cannot be set per agent. Closed #214: stop already emits
`agent_exited`. What is missing is the actor and `--force`, and a manager-issued stop
should be info-only.

**Baseline to measure first** (WS-0 run). Per manager-hour: wakes (`continuations:*`
keys), no-action wakes and their input tokens (§1.7), and the share of wake input that was
a cache write rather than a cache read (wakes after the TTL). Per agent-hour: delta
blocks, characters and carried cost. Keep the run's `context.db` for the replay check.

**Where the code is today.**
- `MANAGER_WAKE_KINDS` (`services/team.py:133–147`) includes `agent_exited` (every exit)
  and `switched`. `_wakes_manager` (`:190–202`) = kind ∈ set, or `--to manager`.
- `_stop_row` emits `agent_exited` for every non-hand-over stop (`services/fleet.py:5411`,
  through `_emit_exit` `:8730–8746`, text `"<label> exited (<status>)"`) and nudges the
  manager (`:5415`). The event's `session_id` is the stopped agent's. A manager's own
  `fleet stop` is therefore not filtered by `exclude_session` and wakes it at its next
  Stop. `_end_dead_rows` emits and nudges the same way (`:4726–4734`).
- `fleet stop` has no `--as` (`cli/fleet.py:498–534`); spawn, restart and switch do.
  `AISQUARE_FLEET_AGENT` (`core/orchestrator.py:71`) names the caller's own row in any
  fleet window. `switched` (`services/fleet.py:7053–7060`) and `restarted` (`:7641–7647`)
  carry the moved agent's session id.
- The wake reason renders every event since the cursor, info included, up to
  `_DELTA_LIMIT = 10` (`:58`; `_manager_wakeup` `:2239`, `_render_wakeup` `:3935`), each
  clipped to `_WAKEUP_LINE_BUDGET = 400` (`:3888`) with no pointer.
- `_render_delta` (`:3853–3876`) is unclipped: `- [<board>] <8-char id> (<role>) <kind> →
  <to>: [#n tsk_…] <full text>` (`event_line` `:3834–3850`), no seq, no `note show <seq>`.
  `team verify <seq>` (`cli/team.py:1105`) prints one event's line, as a receipt check.
  The cursor already advances past what a wake showed (`:2269`), so the next delta does
  not repeat it.
- Session start renders the last `_BOARD_EVENTS = 5` events (`_briefing_events`
  `:1859–1867`, used at `:1955–1969`), unclipped, for every role. `AISQUARE_TEAM_DELTA=0`
  (`core/orchestrator.py:107–109`) mutes deltas and wakes only, shell-wide; it is
  deliberately not in `TEAM_ENV_VARS` (`core/spawn.py:204–206`). No role key, no spawn flag.
- Since #240 every agent-facing reader leaves `HUMAN_BOARD_KINDS` out in the query (`:164`):
  keep filtering before the LIMIT for any new exclusion (review of #240, finding 11).

**What to build.**
1. *Actor and details on events* (store schema step 34; `core/store.py` `_MIGRATIONS`
   currently ends at step 33, `:1061`): add `team_event.actor TEXT` and
   `team_event.meta TEXT` (JSON), nullable, through `_PRODUCTS` columns (added only where
   absent). `TeamEvent` gains `actor: str | None` and `meta: dict[str, object] | None`.
   `as_envelope` carries both. `_emit` (`services/team.py:368`) takes `actor=` and
   `meta=`. Actor values: a session id, or one of `user` (asq UI or a shell with no
   session), `usage-limit` (`hooks.HANDOVER_SPAWNER`), `auto-stop` (reserved for P2's
   auto-stop). NULL means the agent's own exit or unknown (a crash found by `reap`).
   `agent_exited.meta = {"status": int|null, "force": bool, "released": int, "why":
   str|null}`. Text gains the actor: `coder-auth exited (0) — stopped by manager`,
   `… (--force)`.
2. *`fleet stop --as SESSION`* (`cli/fleet.py`), falling back to the caller's
   `AISQUARE_FLEET_AGENT` row's session, else `user`. `_stop_row`, `bulk` and `shutdown`
   pass it through. The manager cycle's `fleet stop <label>` gains `--as {sid}`
   (`core/harness.py:1053`).
3. *One classifier* replaces `_wakes_manager`:
   `wake_class(event, me) -> Literal["own", "wake", "info"]`.
   - `own`: `event.actor == me.id`. It is never shown to `me` and is excluded in the query
     (`exclude_actor=`), like `exclude_session`.
   - `wake`: kind ∈ {`task_review`, `task_done`, `task_blocked`, `task_reopened`,
     `result`, `question`, `limited`}; or addressed to the manager; or `agent_exited` with
     `meta.status` ≠ 0 (None counts as non-zero: a kill or loss) and actor ≠ `me`; or
     `agent_exited` with `meta.released > 0` (it left an open claim; owner Q4).
   - `info`: everything else, including `agent_exited` status 0, `switched`, `restarted`,
     `task_claimed`, notes and decisions. Info events stay past the cursor and ride along
     the next wake or prompt delta. That is the existing fold, unchanged.
   `MANAGER_WAKE_KINDS` keeps its name for the kind list, minus `agent_exited` and
   `switched`. Update `tests/test_manager_loop.py::test_the_wake_kinds_are_the_plans` and
   `docs/plans/fleet-tui.md` §7.3 together.
4. *Nudges follow the classifier:* `_stop_row` (`:5415`), `_end_dead_rows` (`:4734`), the
   restart failure path (`:7633`) and `_abandon_handover` (`:8051`) nudge only for a
   wake-class exit. A human Stop from asq therefore no longer types a nudge into a waiting
   manager.
5. *One short line per event, full text one command away:*
   `#<seq> <who> <kind>[ #<n>]: <first line, ≤ 160 chars>[ … → aisquare team show <seq>]`.
   `who` = the author's fleet label on this board, else role, else `cli`. The `tsk_…` id is
   dropped when the board number exists (`task show` accepts `#12`/`12`). The board name
   moves to the block header (`<aisquare-team-delta board="…">`) instead of every line;
   one delta reads one board. Apply this to the per-prompt delta, the wake reason (keeping
   its 8,000-char budget; the clip now has a pointer, so the `_WAKEUP_LINE_BUDGET` comment
   changes), and the session-start "recent updates". The human `aisquare board` output
   keeps full text.
6. *`aisquare team show <seq>`* (new; reuses `verify_receipt`'s board check). It prints
   seq, time, board, author (session, label, role), actor, kind, task (#n + title),
   addressee, meta and the full text. `--json` emits the envelope. It refuses a seq on
   another board, naming it as `verify` does. Add it to `IMPLEMENTED`. The protocol lines
   in `_render_board` (`:2929–2938`) name it.
7. *Per-agent board context:* `[fleet.roles.<role>] board_context = "all" |
   "relevant" | "none"`, `fleet spawn --board-context …`, recorded on `LaunchSpec`
   (`models.py:1032`, new `board_context` field) and replayed by restart and switch. The
   window gets `AISQUARE_BOARD_CONTEXT`, which `launch` and `team spawn` honour from the
   shell. Add it to `TEAM_ENV_VARS` so it never reaches a tmux server's global env.
   - `none`: no "recent updates" at session start and an empty per-prompt delta, with the
     cursor still advanced. Its assignment, task list, protocol and cycle stay.
   - `relevant`: keep only events on its assigned or claimed task, events addressed to its
     role or label, and `decision` notes. The filter runs in the query.
   - Defaults: reviewer `none`; everyone else `all` (owner Q5 on coder/tester
     `relevant`). The manager refuses anything but `all`, with a config-load warning and a
     doctor row.

**Acceptance criteria.**
- [ ] A manager's `fleet stop coder-x` writes `agent_exited` with `actor` = the manager's
  session and `meta.force`. Its next Stop does not block, and no nudge is typed.
- [ ] A coder whose pane dies with status 1 wakes a waiting manager (nudge) and blocks a
  working manager's Stop.
- [ ] `switched` and `restarted` never block a Stop. They appear in the next wake or delta.
- [ ] A 2 KB note reaches other agents as one ≤ ~200-char line ending in `aisquare team
  show <seq>`, and that command prints the full note.
- [ ] A reviewer spawned with defaults sees no "recent updates" and no deltas.
  `--board-context all` restores them and survives `fleet restart`.
- [ ] Replay: on the baseline run's `context.db` (§1.7), the new classifier's
  would-have-woken count is ≥ 80% below the recorded continuations. This is a target to
  measure against, not a promise.

**Tests.** `tests/test_manager_loop.py` (failing-first:
`test_a_managers_own_stop_does_not_wake_it` fails on b26dd140), plus an exit-status matrix
(0 / 1 / None / released > 0), `switched`/`restarted` as info, and the updated
`test_each_wake_kind_wakes`. `tests/test_fleet_service.py`: actor and meta on
stop/reap/shutdown, `--force`, `--as` and the env fallback, nudges only for wake-class
exits. `tests/test_store.py`: schema step 34 columns added once (`_PRODUCTS` presence
test), old rows read NULL. `tests/test_team.py`: line format, clipping, `team show` with
non-ASCII text (the `_output_cost` lesson), `board_context` filtered in the query (40
irrelevant events must not hide a relevant one). `tests/test_stubs.py`: `("team", "show")`.

**Docs to update.** `docs/fleet.md:197–202` (what wakes the manager), `:1435–1437` (the
nudge table), `fleet stop` (:358) for `--as`, and `fleet spawn` (:218) for
`--board-context`. `docs/orchestration.md:119` (delta description), `:342` (env table:
`AISQUARE_BOARD_CONTEXT`) and the receipts section (:154) for `team show`. Add a
`docs/plans/fleet-tui.md` §7.3 amendment note. `docs/reference.md` gets the keys and the
command. CHANGELOG: Changed, Added.

**Telemetry.** Before/after per manager-hour: wakes (from `continuations:*` keys),
no-action wakes and their input tokens (§1.7), delta blocks, characters and carried cost
per agent-hour, and `team show` calls (agents re-reading). Report the share of wake input
that was cache-miss: a wake after the TTL is the expensive kind.

**Risks & open questions.**
- Q4: should a clean exit (0) that released an open claim wake the manager? (Recommended:
  yes; it is an orphaned task.)
- Q5: coder/tester default `relevant`? It saves more, but coders stop seeing other agents'
  decisions unless those are `--kind decision`.
- Q6: may the replay also use a real project's `context.db` and transcripts? They hold
  prompts. If yes, run it only on the machine that holds them, through the ledger's
  counts-only output.
- P0's account-switching fix for a double switch on `limited` may make `limited` info
  while an automatic hand-over is in flight. It lands in `wake_class` as one more rule,
  not a second list.
- Column vs anchored text: `signal` events already decode an anchored text format
  (`models.py` `as_envelope`). A column is recommended for queryability; the owner may
  prefer no migration.

**Industry grounding.**
- [Claude Code costs](https://code.claude.com/docs/en/costs): "Claude Code sends your full
  conversation with every request"; cross-session messages delivered as new turns cost
  "your full context each time", which is why the product ships `crossSessionInbound =
  "hold"`, the same fold this workstream applies to info events.
- [Claude Code hooks](https://code.claude.com/docs/en/hooks): Stop `decision: "block"`
  needs a `reason`; `stop_hook_active` and an 8-consecutive-continuation cap guard loops;
  hook strings over 10,000 characters go to a file with a 2,000-character preview.
- Anthropic, [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  (2025-09-29): keep "lightweight identifiers" and load data just in time. That is the
  `#seq` + `team show` pattern.
- Anthropic, [multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
  (2025-06-13): multi-agent systems use ~15× the tokens of a single conversational
  exchange. Subagents should "pass lightweight references back to the coordinator"
  instead of copying output through the conversation.

**Linked issues.** #279; #214 (closed; actor/`--force` carried here).

---

## 5. WS-3 — Over-budget snapshot still says "read this FIRST" (#282)

**Problem.** #129 (2026-09-10) measured a generated skeleton of 1.497M tokens against
`[snapshot] max_tokens = 150000`, with SessionStart still saying "read this FIRST". An
agent that obeys pages through a file larger than its whole context. The Read tool
returns at most 25,000 tokens per call by default ([tools reference](https://code.claude.com/docs/en/tools-reference)).
Below the budget the same directive tells every agent to read a skeleton of up to
150,000 tokens before it starts, and that text is then carried by every later request.

**Baseline to measure first** (WS-0 fixture repos). On a repo whose skeleton is between
25k tokens and `max_tokens`, and on one over `max_tokens`: Read calls on the skeleton
path and context tokens at the tenth minute of a fresh coder session.

**Where the code is today.** `_directive` (`services/hooks.py:501–541`). The
`skeleton_only` branch (`:514–524`) says "Skeleton (read this FIRST)" (`:521`), although
that status means that even the compressed pack exceeded `max_tokens`
(`core/snapshot.py:589–593`, `_write_skeleton_only` `:636`). The token count is on the
meta (`skeleton_token_count`), and `skeleton_only_detail` (`:604`) already says it for
onboard and doctor. The `ready` branch (`:504–512`) says "read this FIRST" for any
skeleton up to the budget. That can be ~150k tokens (`core/config.py:518`), read by
every agent at start.

**What to build.**
- `skeleton_only`: never "read FIRST". New text: "aisquare has a per-file index into a
  skeleton of this codebase. The skeleton is {n} tokens, over the {budget}-token budget:
  never read it from the top. Find the files you need in the index and read only their
  slices (char offsets)", then the index path and the skeleton path.
- `ready`: "read this FIRST" only when the skeleton (or the pack, when no skeleton exists)
  is ≤ `_READ_FIRST_MAX = 25_000` tokens, one default Read. Above that, use the index-first
  wording with the size (owner Q7).
- No new config key. The 25k constant names Claude Code's documented default in a comment.

**Acceptance criteria.**
- [ ] A `skeleton_only` snapshot of 1,497,000 tokens produces a directive without "read
  this FIRST" that names the token count and the index path.
- [ ] A `ready` snapshot with a 20k skeleton still says "read this FIRST". One with a
  90k skeleton does not (if Q7 = yes).

**Tests.** `tests/test_hooks.py` (failing-first: assert "read this FIRST" not in the
skeleton_only directive; it fails on b26dd140). Extend `tests/test_snapshot.py` only if
the meta needs a field.

**Docs.** Wherever `docs/` describes the snapshot directive (grep "read this FIRST");
CHANGELOG Fixed. **Telemetry.** Read calls on the skeleton path in the first 10 minutes of
sessions on an over-budget repo, before/after (`tool_use` inputs naming the skeleton).
**Risks.** Q7: the 25k threshold here or in P2's #91/#92? Recommended here: one constant.

**Industry grounding.** [Tools reference](https://code.claude.com/docs/en/tools-reference):
Read defaults to 25,000 tokens per call and "reads a larger file in pages". [Effective
context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents):
load just in time from references and metadata, not up front.

**Linked issues.** #282; #129 (c); P2's #91/#92 own the tiered snapshot.

---

## 6. WS-4 — Manager hygiene (#281)

**Problem.** A manager is the longest-lived session in a fleet, and every request
re-sends its whole context (§1.2). Nothing in aisquare compacts it, so it grows until
Claude Code's auto-compaction fires near the auto-compact window (~967K tokens by default
on 1M-context models) or idle compaction fires after about 54 idle minutes. Both are a
threshold or a timer that knows nothing about the plan in flight: lowering
`autoCompactWindow` only moves the point where compaction fires, which can be mid-plan.
The safe moment is a quiet point: the manager idle at its prompt, no wake-class board
event pending, its last turn not a plan or a question, and a freshly written state file
on disk, optionally followed by a resume prompt. After a compaction or a restart, a
manager has only the plain board to start from, so it rebuilds its picture by re-reading
the board, task details and files. A bounded start pack (resume, open items, rules
index) replaces that. Managers also paste scripts and rule text inline into `--detail`,
`--prompt` and Bash heredocs, and that text then rides every later request. A brief
builder that assembles standard rule blocks by path, and refuses a brief missing one,
removes it. #129(c) (2026-09-10): a 227,542-token mean manager context was re-shipped
across 45 responses for 31,484 output tokens.

**Baseline to measure first** (WS-0 run). Per manager: context per request over time
(sawtooth), compactions by trigger, input tokens per manager-hour, tokens read in a fresh
manager's first 30 minutes, and the share of manager input that is tool-call input text
(Bash heredocs, `--detail`, `--prompt`).

**Where the code is today.**
- Nothing on main compacts or watches context size. `grep -r compact src/` finds only
  `_COMPACT_SOURCE = "compact"` (`services/team.py:84`) and the captain's keep-state rule
  (`:1934`).
- A session start of source `compact` re-renders the plain board for a manager
  (`:1955–1969`). `upsert_session` forces `state = 'working'` on every start
  (`core/store.py:2402`). After a compaction that ran while the manager was waiting, the
  row reads `working` and `nudge_manager` (`services/fleet.py:8991`) refuses it. This is
  the same root as #123 (P0).
- The Stop hook payload carries `last_assistant_message` (hooks reference).
  `cli/hook.py:179–204` reads only `session_id` and `stop_hook_active`.
- A detached worker already exists: `services/hooks.py:328` `_detach(argv)`, used by the
  usage-limit hand-over.
- Resume prompts are one line: "re-read `aisquare board` and `git status`"
  (`services/fleet.py:8090–8125`). A fresh hand-off quotes 5 board entries and 600 chars
  of contract (`:8128–8175`).
- The manager cycle is ~2.5k chars (`harness.role_cycle("manager", …)` on b26dd140: 28
  lines, 2,542 chars). The briefing is small; the cost of a new manager is what it reads
  after starting.
- `task add --detail` is free text (`cli/task.py:59–100`). Nothing checks the
  objective/acceptance/boundaries blocks the cycle asks for (`core/harness.py:1036–1039`).
- Claude Code now ships `--autocompact`, idle compaction (threshold
  `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`), PreCompact/PostCompact hooks and SessionStart
  `compact` re-injection; see the grounding below. None of it knows about a plan in flight.

**What to build.**
1. *Quiet-point compaction.*
   - Keys in `[fleet]`: `quiet_compact_at = 0` (tokens; 0 = off; suggested first value
     400000, well under the auto-compact window and above idle compaction's 200,000
     default; tune it from the baseline), `quiet_compact_delay = 45` (s),
     `quiet_compact_cooldown = 30` (min), `quiet_compact_state_max_age = 20` (min),
     `quiet_compact_resume = false`. Ship OFF in this release and flip after measurement
     (owner Q8).
   - Arm: `services/hooks.py:turn_stopped` (`:195`), after `hook_stop` returned no
     decision for a live `manager` row. `cli/hook.py` stop passes `last_assistant_message`.
     Don't arm when it ends in `?`, when the permission mode is `plan`, or when the row is
     `attention`. Otherwise call `_detach([... "hook", "compact-check", <session>])`
     (hidden command; add it to `IMPLEMENTED`).
   - Worker: sleep `quiet_compact_delay`, then require all of the following. The row is
     still `waiting` and not `HANDOVER_STATE`/`limited`; the cursor is unchanged since
     arming; there is no `wake`-class event past the cursor (WS-2); the context is ≥
     `quiet_compact_at` (`transcripts.latest_context_tokens`, WS-0); no quiet compaction
     ran within the cooldown (`team_meta compact:<session>`); and the state file (2.)
     changed within `quiet_compact_state_max_age` and after the last wake. Then P0's pane
     probe must see the agent at its prompt with an empty input box. Only then type one
     line through P0's verified delivery:
     `/compact Keep the goal, decisions and why, every open task by #number with owner and state, live agents by label with their task, unanswered questions, and the state file path. Drop tool output, board updates already acted on, and file contents.`
     A slash command runs no turn, so the ack is our own SessionStart(`compact`) hook
     writing `compact:<session>` within 120 s. No ack means one board note
     (`kind=notice`, human-only) and no retry until the cooldown.
   - After: SessionStart(`compact`) for a manager renders the start pack (2.). If the
     `compact:<session>` mark says quiet, it restores `waiting` instead of `working`
     (coordinate with P0 #123: one rule in `upsert_session` for every source, not two).
     `quiet_compact_resume = true` also types a one-line resume prompt ("Read the start
     pack above; if nothing needs you, stop.").
   - Never lower Claude Code's own window from aisquare. Auto-compaction (~967K) and idle
     compaction stay as backstops. The doc explains the interplay.
   - Optional (M2): wire `PostCompact` → `aisquare hook post-compact` to record trigger
     and summary size for the ledger. That touches `plugins/claude-code/hooks/hooks.json`,
     `core/agents.py` hook list and doctor; skip it if WS-0 can infer compactions from the
     transcript.
2. *Start pack.* `services/team.py` `_start_pack(store, project, me)` replaces the
   plain board for role `manager` at every SessionStart source, ≤ 20,000 chars
   (`_START_PACK_BUDGET`), with each section capped and a pointer when clipped:
   - **Resume:** the manager's state file `paths.project_data_dir(project)/manager/state.md`
     (`core/paths.py:495`), clipped to 8,000 chars, with its age. When missing: "no state
     file yet: write one at <path> before you stop".
   - **Open items:** every open task as one line `#n [status @label] title ⧗ needs` (not
     only the first 8; `_BOARD_TASKS` `:59`), the last 5 unanswered `question` events, live
     agents (label, role, state, task #, account slot), and the count of wake events
     pending.
   - **Rules index:** `.aisquare/rules/*.md` in the project, as `path — first line`
     (never content), then the role cycle as today.
   - The manager cycle gains two sentences: "Keep your state in <path>; rewrite it (≤ 4,000
     chars: goal · decisions · open items · next step) when a plan step completes and
     before you stop. Reusable scripts and rule text live in files; run and cite them by
     path, never paste them inline."
3. *Brief builder.*
   - `aisquare task add … --detail-file <path>` reads the contract from a file.
   - `--rules testing,git` appends `Rules: .aisquare/rules/testing.md, …`. A missing file
     refuses the add and is named in the error.
   - `[team] brief_blocks = ["objective", "acceptance", "boundaries"]`: each must appear
     as `<label>:` in the detail (case-insensitive). By default a missing block warns on
     stderr and in `--json` (`"missing_blocks"`). `[team] brief_strict = true` refuses
     with exit 2 (owner Q9).
   - P0's kickoff for `fleet spawn --task` (#122/#74) carries the contract, so the rules
     pointers reach the coder with it.

**Acceptance criteria.**
- [ ] With `quiet_compact_at = 400000`, a manager at 450k that finishes a turn with no
  pending wake events and a fresh state file is compacted within `delay + 10 s`. Its row
  reads `waiting` afterwards, and a coder's `task review` nudges it.
- [ ] The same manager ending on a question, with a pending `task_done`, a stale state file
  or a half-typed human draft is not compacted, and each refusal reason is logged in
  `team_meta`.
- [ ] After any SessionStart, a manager's injected context is ≤ 20,000 chars and
  contains the state file, every open task and the rules index.
- [ ] `task add --rules nosuch` refuses. A detail missing `acceptance:` warns, and refuses
  under `brief_strict`.
- [ ] Measured (§1.7): manager input tokens per manager-hour and mean context per request
  fall, with numbers in the PR. Tokens read in a new manager's first 30 minutes are
  reported against the baseline run.

**Tests.** `tests/test_manager_loop.py`: `test_a_quiet_manager_is_compacted_once`
(failing-first: no `compact-check` exists) plus a refusal matrix per condition.
`tests/test_hooks.py`: the arm skips on `?`, plan mode, attention. `tests/test_team.py`:
start-pack budget and sections; a compact source keeps `waiting` after a quiet compaction.
`tests/test_harness.py` (cycle text), `tests/test_token_ledger.py`
(`latest_context_tokens`), `tests/test_stubs.py` (`("hook", "compact-check")`). Pane
checks use the fake tmux server pattern of `tests/test_fleet_service.py`, never a real
`claude`.

**Docs to update.** `docs/fleet.md` gets a new subsection "Long-lived managers: state file,
start pack and quiet compaction" under "What runs where". `docs/orchestration.md` gets
task contracts and rules files. `docs/reference.md` gets the keys and the hidden hook.
CHANGELOG Added.

**Telemetry.** The context sawtooth per manager; compactions by trigger (quiet / auto /
idle); input tokens per manager-hour; tokens read in the first 30 minutes of a new
session; and the share of manager input that is tool-call input text (Bash heredocs,
`--detail`, `--prompt`), each against the baseline run.

**Risks & open questions.**
- Q8: ship quiet compaction off (recommended for one release) or on at a threshold chosen
  from the baseline?
- Q9: brief blocks warn by default, or refuse?
- Q10: a resume prompt after quiet compaction? It re-orients the manager at once, but it
  costs one small turn, and the start pack already rides SessionStart. Recommended
  default: off.
- Compaction costs a request of its own, but on a warm cache "a mid-session `/compact` then
  costs a fraction of what the context size suggests" ([prompt caching](https://code.claude.com/docs/en/prompt-caching)):
  arm within the cache TTL, never compact a cold manager.
- CLAUDE.md, memory, the plan file, up to five recent files and skill bodies survive
  ([context window](https://code.claude.com/docs/en/context-window)); board history and
  hook context are summarized away, hence the state file and start pack.
- Typing into a pane is P0's hardest problem (the draft guard and verified delivery): do
  not ship ahead of them.

**Industry grounding.**
- [Model configuration](https://code.claude.com/docs/en/model-config), [settings
  reference](https://code.claude.com/docs/en/settings-reference): auto-compact window ~967K
  by default on 1M models, settable per launch (`--autocompact`, 2.1.221+); idle compaction
  ~54 min after the last request at ≥ 200k tokens on the 1-hour TTL. Both are timers.
- [Hooks](https://code.claude.com/docs/en/hooks), [context window](https://code.claude.com/docs/en/context-window):
  PreCompact can block, PostCompact gets `compact_summary`, SessionStart hooks matching
  `compact` "add their output to the compacted context" — the start pack's delivery path.
- [API compaction overview](https://platform.claude.com/docs/en/build-with-claude/compaction):
  "Use on-demand compaction wherever it is available", chosen when "your application needs
  to control when compaction occurs".
- Anthropic, [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  (structured note-taking: NOTES.md-style state outside the window) and [multi-agent
  research system](https://www.anthropic.com/engineering/multi-agent-research-system)
  (the lead agent saves its plan to memory before the context is truncated).

**Linked issues.** #281; #129 (c) moves here; P0 #123, P0 verified delivery (#122).

---

## 7. WS-5 — #129 remainder

**Problem.** #129 (2026-09-10) ranked levers for a 3-fix run that took ~145 min
against ~48 for one session. Done since: the reviewer `--restricted` conflict (now
`REVIEWER_ARGS`), the coder worktree default (`core/config.py:333`), the attention trap
(idle notices quiet since #153, `QUIET_NOTIFICATIONS` `services/team.py:2291`, and deferred
wake nudges, `_deferred_wake_reason` `:2210`), and stop emitting `agent_exited`
(`services/fleet.py:5411`). Three items remain.

**(a) Per-task verification.** The manager cycle still says to spawn a tester "once work
reaches review" and a reviewer "once a PR exists" (`core/harness.py:1040–1044`). `task
next` has no `--task` filter (`cli/task.py:199–230`). Since #135 a tester spawned `--task`
gets its own task first (`_fleet_row_hint`, `services/team.py:1707`), then "Repeat" drains
the shared pool. Build: `task next --task <ref>` (honours `--status`; nothing else is
returned), and tester and ui-tester cycles that, when spawned for a task, take only that
task and stop after the verdict. The manager sentence becomes "spawn one tester per task
at its `task_review`; the validator stays the single final gate". Acceptance: two testers
spawned for two review tasks each verify their own and exit. Test: `tests/test_team.py`
(failing-first on the missing flag) and `tests/test_harness.py`. #129 estimated 18–35 min
saved per run.

**(b) `fleet spawn` honours the model/effort ladder.** The fleet path still never calls
`resolve_model`. The only callers are `cli/team.py:422,739` and
`services/persona_import.py:107`. Build: when the binary is the default agent and no
`--model`/`--effort` appears in binding args, role `extra_args` or caller args, resolve
with `harness.resolve_model(role, probe=False)` (cached probe only; never the 150 s probe
at spawn). Pass both flags, record them on `LaunchSpec`, and show the model in `fleet ls
--json`. The alternative is to delete the dead path (owner Q11). Test:
`tests/test_fleet_service.py` (failing-first: a coder spawn has no `--model`). This is a
cost lever, not a token count: a light role on Sonnet instead of the account default.

**(d) pytest-xdist.** Add it to the `dev` extra (`pyproject.toml:169`) and `uv.lock`;
`make test` runs `pytest -n auto`; fix or `xdist_group` the tests sharing `AISQUARE_HOME`,
tmux sockets or ports; CI matrix 3.11–3.13. Acceptance: wall time before/after on one
machine in the PR (#129: 339.6 s serial on 12 cores; re-measure on b26dd140) and three
consecutive green parallel runs.

**Moved elsewhere:** (c) manager context → WS-4. (e) explainability proxy on the hot path
→ P2's #51 rewrite. (f) two homes for role flags → WS-1.

**Industry grounding.** [Claude Code costs](https://code.claude.com/docs/en/costs):
"Sonnet handles most coding tasks well and costs less than Opus"; for agent teams, "Use
Sonnet for teammates". Gartner via [TNW](https://thenextweb.com/news/ai-coding-costs-developer-salary-gartner-2028)
(2026-06-24): route simple, high-frequency work to cheaper models; per-developer AI coding
bills are "leaping from $20 or $100 a month … to $2,000 or even $5,000".

**Linked issues.** #129 (rewritten); #122/#123/#74 are P0's.

---

## 8. Release checklist

- Version: minor bump (0.9.0 expected): store schema step 34, config step 3, new default
  tool sets. Coordinate the number with P0, which may ship a patch first.
- CHANGELOG `[Unreleased]`, house voice. Added: `team show`, `metrics tokens`,
  `--board-context`, `fleet stop --as`, start pack, brief blocks, quiet compaction (off).
  Changed: wake rules, delta line format, lean tool and skill defaults. Fixed: reviewer
  deny list survives `extra_args`; over-budget snapshot directive.
- Migration notes (docs/fleet.md "Upgrading"): schema step 34 adds two nullable columns;
  config step 3 drops the reviewer's built-in deny from `extra_args` (now the role's own);
  restarted agents get the merged deny token on their next launch (one uncached first
  request each).
- Rollout defaults, each reversible with one `aisquare config set` line the doc lists:
  `[fleet] deny_tools` on; `skill_listing = "names"` for workers; `board_context = "none"`
  for reviewers; `quiet_compact_at = 0` (off).

  ```text
  aisquare config set fleet.quiet_compact_at 400000
  ```

- Docs: `docs/fleet.md`, `docs/orchestration.md`, `docs/reference.md`, the
  `docs/plans/fleet-tui.md` §7.3 amendment, this handoff; README only if a top-level line
  changes (< 250 lines).
- Measured numbers: one table in the release PR, one row per workstream, against the WS-0
  baseline run with the §1.7 method and the same fleet script before and after.

## 9. Out of scope

- P2: `aisquare watch` (one non-AI watcher in place of per-manager Monitor watchers),
  auto-stop of idle agents, parent-manager routing (#199), code intelligence and tiered
  snapshots (#91–#95). WS-3 fixes only the directive; P2's wakes should use WS-2's
  classifier.
- P0: verified prompt delivery, delivery guards, account switching. WS-4 consumes them.
- Per-plugin enable/disable per role (`enabledPlugins` via `--settings`); only the listing
  budget is in scope. Server-side API compaction or context editing: aisquare drives
  Claude Code sessions, not the Messages API.
