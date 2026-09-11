# Native work briefs and verification

AI Square now has its own requirements and evidence system. You keep using its
manager, workers, and task board. No Spec Kit or Ponytail installation is needed.
These are AI Square's own implementation and working instructions.

A **brief** records what the user requested. Its requirements have stable names
such as `R1` and `R2`. Existing board tasks are linked to those requirements.
Evidence records what was checked and which source files were checked. A final
coverage check identifies missing, failed, blocked, or stale evidence.

Personality is separate. Switching a persona does not change a brief, a working
instruction, a command, a recorded result, or a task's status.

## A login page, from request to finished work

You tell the manager:

> Add a login page. Correct details should open the dashboard. Wrong details
> should show an error. The error must also be visible on a phone.

The native manager instructions ask it to:

1. Record those three outcomes in a short brief.
2. Link the existing implementation task to the requirements.
3. Start only the workers needed, giving each its actual task ID.
4. Require fresh evidence before declaring that work complete.

The coder receives the relevant requirements and its explicit task assignment.
Its native instructions encourage inspecting the affected flow and reusing
suitable existing project code. They preserve the existing checking obligations.
A separate planner is useful for some jobs; it is not a compulsory stage.

A tester runs the required tests through AI Square's command reporting helper.
The UI tester checks the actual interface using available browser tools and
records screenshots or a report. If the phone error is clipped, that requirement
fails and its existing task reopens. The coder fixes it; checks run again.

The reviewer examines the actual changes. The validator checks the requested
outcomes against current evidence and inspects the evidence itself. The manager's
READY decision still requires the existing validator gate. A database row saying
`pass` cannot establish that a test was meaningful.

## Commands you can use today

Run these inside the relevant project. Replace `BRIEF_ID`, `TASK_ID`,
`PROJECT_ID`, and `REPORT_ID` with the IDs printed by the commands. Add global
`--json` immediately after `asq` when you want machine-readable output.

Create the contract and the normal board task:

```sh
asq brief create "Login page" \
  -r "Valid details open the dashboard" \
  -r "Invalid details show an error" \
  -r "The error remains visible on a phone"

asq task add "Build the login page" --role coder \
  --detail "Use the existing login service. Implement the three brief requirements."

asq brief link BRIEF_ID TASK_ID -r R1 -r R2 -r R3
asq brief update BRIEF_ID \
  --check "R1=Run the successful-login check" \
  --check "R2=Run the invalid-details check" \
  --check "R3=Try the error flow in a browser at the requested phone size"
```

`asq --json brief show BRIEF_ID` includes the board's `project_id`. The manager
can launch the implementation worker with `asq fleet spawn coder --task TASK_ID`.
The worker's startup context names that task and tells it to use the existing
atomic claim command; it does not silently take the next unrelated task.

Run the project's actual required test command. This example assumes its login
checks live at `tests/test_login.py`; use the complete checks your contract
requires, including any broader suite required by the project:

```sh
asq exec --project PROJECT_ID --task TASK_ID -- pytest -q tests/test_login.py
```

The command prints a saved report ID. `--project` enables source identity capture
before and after execution. The command still runs exactly once. Capture failures
are reported as unknown evidence rather than preventing the command from running.

If the report really covers both requirements, record it against both:

```sh
asq brief evidence BRIEF_ID R1 --task TASK_ID --verdict pass \
  --summary "Successful-login check passed" --report REPORT_ID
asq brief evidence BRIEF_ID R2 --task TASK_ID --verdict pass \
  --summary "Invalid-details check passed" --report REPORT_ID
```

A failing command cannot be submitted as a pass through `--report`. A pass also
requires complete retained output, no launch error, and matching source identities
before execution, after execution, and when evidence is recorded. Running checks,
changing code, then recording the old report as a new pass is rejected.

Inspect original output without running the command again:

```sh
asq reports show REPORT_ID --raw
```

## Browser evidence and other manual evidence

A screenshot is a real file, but its existence does not prove the page was checked
correctly. `--artifact` therefore records **manual** evidence. It is labelled as
manual in the evidence history and coverage output and requires independent
validator review.

Keep screenshots and reports outside the source checkout, or in a Git-ignored
output directory. A new nonignored report inside the source tree looks like a
source change, because source identity includes nonignored files.

For example, after actually capturing the failing phone screen:

```sh
asq brief finding BRIEF_ID R3 --task TASK_ID \
  --summary "At 320px, the error message is clipped. Submit invalid details to reproduce." \
  --artifact /absolute/path/outside/source/phone-failure.png
```

This reuses the linked task. A finding against a task in review or done returns
it to todo; a task someone is still working on keeps its owner and status (the
failure reaches them through the brief), and a dropped task stays dropped. Without
`--task`, the command uses an existing linked task, or creates one correction task
with an idempotency key. Repeating the latest record exactly does not add another
finding, but an identical failure reported after a later pass supersedes that
pass. Three distinct failures by the same task against the same requirement
revision block that task so the manager must re-plan or ask the user, rather than
creating an endless chain of replacement tasks; correcting the requirement starts
the count again, and one task's failures never block another.

After the fix and a real new browser check:

```sh
asq brief evidence BRIEF_ID R3 --task TASK_ID --verdict pass \
  --summary "Error is fully visible and the form remains usable at 320px" \
  --artifact /absolute/path/outside/source/phone-fixed.png
```

The automatic source identity check also makes the earlier automated checks stale
if the code changed. Re-run the applicable required checks and record the fresh
command report before completion.

## Corrections, coverage, and completion

User corrections replace the authoritative brief text while keeping its ID:

```sh
asq brief update BRIEF_ID -r "R3=The error must be visible at both 320px and 768px"
asq brief update BRIEF_ID --add "The login form supports keyboard-only use"
```

Changing a requirement or its expected check invalidates that requirement's old
evidence. Changing assumptions or boundaries invalidates all requirements.
Affected tasks in review or done return to todo. History is retained.

A named build revision can also make affected requirements stale:

```sh
asq brief update BRIEF_ID --source-revision build-2 --affected R3
```

Omit `--affected` to apply the revision change to every requirement. Independently,
automatic source fingerprints detect actual edits even if this command is
forgotten. That file check is conservative and it wins: any edit to the checkout
invalidates the command evidence of every requirement checked in that checkout,
including ones you did not name in `--affected`. `--affected` therefore narrows
the bookkeeping (which requirements' revisions move), not the safety check; after
a real edit, rerun the affected checks and expect the fingerprint to demand the
rest too.

Now inspect coverage and finish verified work:

```sh
asq brief check BRIEF_ID
asq task done TASK_ID --note "Verified against the current brief; evidence is recorded"
```

`brief check` exits with status 1 while anything lacks coverage, fails, is blocked,
or is stale. Every linked task needs its own evidence for its contribution; a new
task cannot borrow an old task's pass. An individual task can finish its verified
contribution before later linked tasks finish, while the whole brief remains
incomplete. `task done` rejects missing or stale proof for linked requirements.

Brief changes and task completion are coordinated in the store: if a correction
arrives after the evidence check but before completion, the completion is rejected.
A subsequent correction reopens affected completed tasks.

## Which checkout does a pass describe?

By default, coverage checks the current files of **each checkout recorded in its
evidence**. This supports a shared task board used by multiple repositories and
Git worktrees. It proves coverage for those checked versions; it does not prove
that their changes have been assembled or merged together.

For one assembled delivery checkout, ask the validator for a strict check:

```sh
asq brief check BRIEF_ID --source-root /absolute/path/to/assembled-checkout
```

The path must exist and be this project's checkout, a directory inside it, or a
git worktree of it; a typo is an error rather than a page of "stale". All proof
must match that checkout's source identity. If it does not, run and record fresh
checks there. Deliveries spanning several repositories also need
explicit integration checks across them; one repository's passing tests cannot
certify another repository's behaviour.

## Inspect, export, and compare working modes

```sh
asq brief list
asq brief show BRIEF_ID
asq brief export BRIEF_ID --output login-brief.md
asq --json brief export BRIEF_ID --output login-brief.json
asq brief mode off
asq brief mode native
```

Exports are snapshots. Update the stored brief through commands; editing an export
does not update the board. Export refuses to overwrite an existing file.

`mode off` keeps the existing role cycles and turns off the additional native
working rules and the evidence-recording instructions for new sessions; the
linked requirements are still shown, because they are facts about the project.
`mode native` enables the rules for new sessions. Sessions retain their recorded
rule version when resumed; the rule text follows the session's current role, so a
session relaunched as a tester gets tester habits. These controls are independent
of persona selection.

For a file shortlist, a worker can use `asq context focus "login error"`. This
uses the existing snapshot index. It is a map to possible files, not proof or a
replacement for reading originals. Ordinary search remains necessary for missing
or newly added files.

## Limits worth understanding

- Native instructions guide the existing AI; they cannot guarantee it follows
  every instruction. Completion checks enforce the structured evidence boundary.
- A zero-exit command report proves the recorded command completed successfully
  on unchanged source. The reviewer/validator must still assess whether it checks
  the requested behaviour. Manual screenshots remain manual claims.
- The software detects exact requirement/boundary overlap, not every possible
  contradiction in natural language. The manager/planner must resolve ambiguity.
- Source fingerprints hash the contents of tracked and nonignored files, plus the
  commit each submodule records. They deliberately exclude Git HEAD (an empty
  commit or a branch switch that changes no byte keeps evidence valid) and
  well-known generated directories (`__pycache__`, `.pytest_cache`, `node_modules`,
  virtual environments and the like) even when a project forgot to gitignore them.
  Ignored build outputs, external services, environment changes, and production
  data can change behaviour without changing that fingerprint; relevant checks
  must account for them.
- A command report captured with `--task` can only back evidence for that task.
  A report captured without `--task` may back any task linked to the requirement,
  so pass `--task` whenever the check belongs to one task.
- Evidence files must remain available and unchanged. Removing or changing a report
  makes its coverage stale. Command report retention also applies. Keep referenced
  reports if coverage must remain checkable; exporting a brief copies its records
  and paths, not the report files themselves.
- A brief supports up to 64 requirements and 512 evidence records. Split larger
  projects into briefs. Concurrent updates are rejected with a retry instruction
  instead of silently dropping another worker's changes.
- The core upgrades an existing context database additively. It does not install
  another product, change the underlying model, merge a PR, or deploy your project.
