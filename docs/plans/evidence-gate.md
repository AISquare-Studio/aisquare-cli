# Work briefs, the evidence gate and command reports

> **Status: built in #300, #306 and #341 (fold after them, in that order).** Part
> 1 is #300, part 2 is #306 plus the report-backed evidence in #341, and part 3 is
> #341. The text below is the handoff as written, kept as the record of what was
> asked. It was written for the parts of #136 that main still lacked. #136 was closed unmerged on 2026-10-23: it was 2,555 commits
> behind main, and two of its four parts had been rebuilt on main in the
> meantime. Its head, `bf822942`, passed review and CI on 2026-09-18 and is the
> prior art to read. Build fresh from main, one part per PR. Do not merge or
> cherry-pick the old branch.

## The gap on main

`asq task done` only writes a status. `services/team.py::finish_task` sets the
status and posts the optional `--note`. Nothing asks whether anything was
checked, or whether the code that was checked is still the code in the tree. A
coder can mark a task done, edit the file afterwards, and the board still says
done.

The acceptance criteria exist, but only as free text: the manager writes them
into `task add --detail` ("objective · why · known · acceptance · boundaries").
Nothing can say which criterion a check covered, which criteria nobody checked,
or which passes a later edit or correction made stale.

The validator's gate is a note (`GATE: PASS|PASS-WITH-FIXES|FAIL`) that nothing
reads back. It is the right judgment, made once, on the assembled deliverable,
but it has no record underneath it to check against.

## The outcome

A task linked to a requirement cannot be marked done unless a passing check was
recorded for that requirement, by that task, against the source that is in the
checkout now. One command tells the manager and the validator which
requirements are proven, which are not, and why. It exits non-zero until every
requirement is proven.

Tasks that are not linked to a requirement keep today's behaviour.

## What is not in this handoff

| #136 part | Where it is now |
|---|---|
| The ui-tester role | On main (`8b38b739`). |
| The spawned agent's task at session start | On main: `team._assignment`, the "ASSIGNED TO YOU" block (`1bfff93a`). |
| A typed first prompt for `fleet spawn --task` | #122, in the P0 train. |
| Manager habits for writing a brief | The P1 manager-hygiene work. |
| `brief mode native/off` and its per-role working rules | Dropped. The role cycles in `core/harness.py` already carry per-role rules. A second, versioned rule layer adds text to every session start for behaviour no test can check. |
| `asq context focus` | Dropped. It matches keywords against file names in the snapshot index. Every session already gets the skeleton and `index.json` at session start, and can search. |
| `asq brief export` | Later, if someone asks for it. |

## Three parts, three PRs

Each part merges on its own, in this order, and each later part needs the one
before it.

1. **Command reports:** `asq exec` records what a command did. No store change.
2. **Work briefs and evidence:** numbered requirements, linked to tasks, with
   recorded checks against a source fingerprint. One store migration.
3. **The gate:** `task done` refuses unproven linked tasks, `asq brief check`
   reports coverage, and the session start shows an agent its requirements.

### 1. Command reports

The point of a report is to record that a command ran, on which source, and how
it ended, so a later step can cite it without running it again. The shorter
output an agent reads is a side benefit, and it stays narrow.

- `asq exec [--project ID] [--task ID] [--session ID] [--raw] -- CMD …` runs
  CMD once. It adds no shell, passes the arguments unchanged and inherits
  standard input. It exits with the child's status: a death by signal is
  reproduced after the report is saved, and a command that cannot start is a
  saved failure with 126 or 127. Global flags such as `--json` go before
  `exec`, and everything after `--` belongs to the child.
- Both streams' originals are kept, the first 1 MiB of each by default
  (`--max-output-bytes` raises it to at most 16 MiB). Truncation is marked with
  the observed and kept byte counts. Reports live under `AISQUARE_HOME/reports`
  with private permissions, and a report appears only once its originals and
  metadata are fully written.
- The command prints a report ID. `asq reports show ID [--raw] [--stream
  stdout|stderr|both]` returns the saved bytes and never runs anything.
  `asq reports list|prune|stats` cover the rest.
- Only two kinds of output get shorter. The first is git status's repeated
  usage hints; machine formats such as `--porcelain`, `--short` and `-z` pass
  through. The second is pytest's percentage progress lines before the first
  section banner, and only when a final count is present; this is off when
  capture is changed with `-s` or `--capture`. Standard error, unrecognised
  output, binary data and control sequences pass through unchanged. `--raw` and
  `AISQUARE_REPORTS=off` turn shortening off.
- Retention is 14 days and the newest 64 reports. A report that backs recorded
  evidence never expires and does not count toward the 64. A report captured
  with `--project` but not yet recorded is kept for the full 14 days.
- `--project` takes a per-file source identity of the checkout before and after
  the command (part 2). If that capture fails, the command still runs and the
  report says its proof is unknown.

#136 measured about 81% less stdout on a 300-test pytest run. Nobody has
measured tokens or cost over a whole job.

### 2. Work briefs and evidence

- A brief is one user request. It has a title, numbered requirements (`R1`,
  `R2`, …) with an optional expected check each, assumptions and boundaries.
  Requirement IDs are stable for the brief's life. The commands are
  `asq brief create|show|list|update|link`.
- `asq brief link BRIEF TASK -r R1 -r R2` links ordinary board tasks. One task
  can cover several requirements, and one requirement can need several tasks.
  Each linked task proves its own contribution.
- `asq brief evidence BRIEF R1 --task TASK --verdict pass|fail --summary …
  (--report ID | --artifact PATH)` records a check. A pass backed by a report
  needs all of these: exit 0, no interruption, complete output kept, no launch
  error, a known source identity, and the same identity before the command,
  after it and now. An `--artifact`, such as a screenshot, is recorded as
  **manual** evidence and labelled that way everywhere, because only the
  validator can judge it.
- `asq brief finding BRIEF R3 --task TASK --summary … [--artifact PATH]`
  records a failure. It sends a task in review or done back to todo, and a task
  someone is working on keeps its owner. Three distinct failures by one task
  against one revision of a requirement block that task, so the manager
  re-plans instead of looping.
- A correction keeps the brief's ID. Changing a requirement or its expected
  check makes that requirement's evidence stale. Changing assumptions or
  boundaries makes all of it stale. Linked tasks in review or done go back to
  todo, and history is kept.
- Every write takes `--as SESSION`, and a session's own writes do not wake it.

**The source fingerprint** hashes the content of the checkout's tracked and
nonignored files, file by file.

- It ignores Git HEAD, so an empty commit, or a branch switch that changes no
  byte, keeps evidence fresh.
- It leaves out well-known generated directories (`__pycache__`,
  `node_modules`, virtual environments), but only among untracked files.
- A nested repository or submodule contributes its own content, not only its
  recorded commit.
- No file is exempt as a check's output (owner's decision, 2026-10-24). A file
  that is new, changed or gone, during the run or since, refuses or stales the
  pass, whoever wrote it. Check output goes in a gitignored directory
  (`--junitxml=reports/a.xml`) or outside the checkout. Identity is kept per
  file so the refusal can name the paths and the remedy.
- The ignore rules are in the identity: every `.gitignore` git reads (a
  self-ignored one too), `.git/info/exclude` and `core.excludesFile`. A symlink
  is hashed as its in-checkout target, file or directory.
- Each file's change time (mtime on Windows) is kept before and after a run, so
  a change put back before the run ended still refuses it.
- Known limit: a case-only rename on a case-insensitive file system is not seen,
  by git either.
- A worktree is fingerprinted as its own files, under the board's one project.

**The store.** Main is at schema v33. The brief table takes the next free
version and is written the way v15 onward are: `CREATE TABLE IF NOT EXISTS`,
columns added through `_add_column_if_absent`, and products listed in
`_PRODUCTS` so that `_converge_by_presence` repairs a store that skipped the
step. Stores that ran a #136 build already have a `work_brief` table
(`id, project_id, revision, data`) stamped at their own v15. Either keep exactly
that shape or use a new table name, because `IF NOT EXISTS` would silently keep
an older shape. The comment above v15 in main's `store.py` explains why: #136,
#201 and #113 each stamped their own v15 while main's was in flight.

### 3. The gate

- `asq task done TASK` refuses a task that is linked to any requirement
  without a fresh passing record from that task. The refusal names each
  requirement and its state (missing, failed, blocked or stale), in text and in
  `--json`.
- The evidence check and the status write are one store transaction. A
  correction that lands between them makes the completion fail.
- `asq brief check BRIEF [--source-root PATH]` prints coverage per requirement
  and exits 1 while anything is missing, failed, blocked or stale. By default
  each pass is checked against the checkout it was recorded in. With
  `--source-root`, every pass must match one assembled checkout, which is the
  check the validator runs. A path outside the project's checkouts is an error,
  not a page of "stale".
- The session start shows an agent its linked requirements and their state,
  inside main's existing 10,000-character hook budget. If building those lines
  fails, only those lines are lost, never the board, in the same way
  `team._assignment` already fails open.

## Traps #136's review found

#136's review on 2026-09-14 (head `24b26a0`) confirmed 35 defects. Every one
below was fixed on the branch by `bf822942`, and each is a regression test the
new PRs need, because a fresh implementation can make every one of them again.

| Part | The defect | What to test |
|---|---|---|
| 1 | When the protected-report list could not be read, the failure read as "nothing protected", and prune deleted reports that backed evidence. | A read failure means "unknown", and prune then deletes nothing. |
| 1 | A background process holding the output pipe kept `asq exec` blocked after the command exited, and a `setsid` child survived the interrupt escalation. | Exec returns once the command exits, after a bounded drain, and records that the pipes were held open. Interrupts go to the process group, and the second and third escalate to SIGTERM and SIGKILL. |
| 1 | 63 newer commands on the machine were enough to prune a fresh report before its evidence was recorded. | An unrecorded `--project` report survives a burst of newer runs. |
| 2 | A non-git working directory under `~` fingerprinted the whole home directory. | The fingerprint refuses the home tree. |
| 2 | Edits, new files and commits inside a submodule or nested repo never changed the fingerprint. | Nested content changes the fingerprint. |
| 2 | The generated-directory filter dropped tracked source under any path named `venv`, `node_modules` and the like. | The filter applies to untracked files only. |
| 2 | A check that wrote a per-run output file into the checkout could never pass, and every rule that exempted such files let a source edit through (an edit made while any run was in flight counted as its output). | No file is exempt (owner's decision): an un-ignored `--junitxml=a.xml` is refused, naming it with the remedy (gitignore it, delete it, or write it outside the checkout); a report written to a gitignored directory passes, re-runs included. |
| 2 | A pass was judged against the recorder's checkout, not the report's. | A pass is compared with the report's own source root. |
| 2 | An interrupted run that still exited 0 was recorded as a pass. | `interrupted_by` refuses the pass. |
| 2 | Re-sending an identical `brief update` bumped revisions, made evidence stale and reopened tasks. | A no-op update changes nothing. |
| 2 | Nothing unlinks a task, so one dropped duplicate made its requirement unprovable forever. | Dropped or vanished tasks are ignored while a live linked task remains. |
| 2 | A manager's own `brief update` woke it at Stop and used up a continuation. | A write made `--as` a session does not wake that session. |
| 3 | One damaged brief anywhere on the board blocked every `task done`, and `--json` said only `unverified_requirements`. | A task is gated only by the briefs it is linked to, and the refusal carries its reason. |
| 3 | An exception in the new session-start block discarded the whole session-start output. | Each block fails open on its own. |
| 3 | Session-start text had no overall budget, so long checks or task detail went over the 10,000-character hook cap. | Requirement text and checks are clipped, and the total is tested against the cap. |

## Decisions for the owner

1. **Gate scope.** Only linked tasks, which is what #136 did and what this
   handoff assumes, or every task in a project that has a brief?
2. **READY.** Should the manager's READY require `asq brief check` to exit 0?
   That is harness text, so if the answer is yes it belongs with the P1
   manager-hygiene work.
3. **Shortening.** Keep it in part 1, or make `asq exec` record only? The
   record is the reason part 1 exists.

## Reading #136

The PR's ref outlives its branch: `git fetch origin pull/136/head`, then read
at `bf822942`.

- Behaviour and limits, as users would read them: `docs/native-workflow.md` and
  `docs/command-reports.md`.
- Services: `services/command_reports.py` (775 lines),
  `core/source_revision.py` (254) and `services/work_briefs.py` (1,004). CLI:
  `cli/command_reports.py` and `cli/brief.py`.
- Tests: `test_command_reports.py`, `test_work_briefs.py`,
  `test_source_revision_hardening.py`, `test_native_hardening.py` and
  `test_native_end_to_end.py`.
