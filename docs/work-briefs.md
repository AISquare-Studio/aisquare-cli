# Work briefs

A work brief is one request from you, written as numbered requirements, linked to
the ordinary tasks on the board. It turns "the acceptance criteria are somewhere in
the task's detail" into something a check can be tied to: `R2` is a name, the tasks
that build it are linked to it, and each recorded check says which requirement, which
revision of it, and which task's work it proves.

A brief is not a second task list. The work is still done, claimed and reviewed as
board tasks ([Orchestration](orchestration.md)); the brief is the contract those tasks
answer to.

## A brief, start to finish

The manager writes the request down as requirements. Each gets a stable id, `R1`,
`R2`, …, that it keeps for the brief's whole life:

```sh
aisquare brief create "Login page" \
  -r "A valid login opens the dashboard" \
  -r "A wrong password shows an error that fits on a phone" \
  --check "R1=pytest tests/test_login.py" \
  --assumption "Users sign in with an email address" \
  --boundary "No single sign-on"
```

The brief is `B1` on this board. Its id works too, and so does the start or the end of
its id, when only one brief matches.
Link the board tasks that build it. One task can cover several requirements, and one
requirement can need several tasks:

```sh
aisquare brief link B1 12 -r R1 -r R2
aisquare brief link B1 13 -r R2
```

When a task's work is checked, run the check through `aisquare exec` (`asq` is the
short name of `aisquare`; both work)
([command reports](command-reports.md)) with the project, so the run is saved with the
source it ran on, and record it against the requirement with the report ID it prints:

```sh
aisquare exec --project prj_… --task tsk_… -- pytest -q tests/test_login.py
aisquare brief evidence B1 R1 --task 12 --verdict pass \
  --summary "the login tests pass" --report 3f9c…
```

Each linked task proves its own part: `R2` above stands at `missing` until both #12 and
#13 have recorded a check of it. `aisquare brief show B1` prints every requirement's
standing, the checks behind it, and the corrections the brief has had;
`aisquare brief list` lists the board's briefs.

## Checks backed by a command report

A report backs a **pass** only if the run:

- exited 0, could start, and was not interrupted (an interrupted run that still exited
  0 is no pass);
- kept both streams whole (nothing past the cap, no pipe held open after the exit);
- has a known source identity, so it ran with `--project` for this brief's board;
- saw the same source before it, after it, and now.

The source is compared **file by file**, in the checkout the report ran in, not the one
you record from: a check run in a worktree is about that worktree.

**No file is exempt.** The source is every tracked file and every untracked file git
does not ignore (`.gitignore`, `.git/info/exclude` or your global excludes). In a
checkout that is not a git repository, everything but well-known generated directories
such as `__pycache__`, `node_modules` and `.venv` counts. A pass needs that whole set to
be identical before the run, after it, and now. Who wrote a file makes no difference:
the check itself, another `asq exec` run, or you while the check ran.

- **The ignore rules are source too.** Every `.gitignore` git reads counts, including
  one that ignores itself, and so do `.git/info/exclude` and your `core.excludesFile`
  (named `.git/core.excludesFile` in a reason). A new rule that hides a `conftest.py`
  changes the identity. A `.gitignore` inside an ignored directory does not count: git
  never reads it, so it cannot hide anything, and rebuilding `.venv` or
  `node_modules` leaves a pass alone.
- **A symlink counts as what it points at** inside the checkout, a file or a whole
  directory, even an ignored one, and so do links inside that directory. A linked
  directory is walked as a checkout without git is, so `.git` and generated
  directories such as `__pycache__` are skipped, and a FIFO or socket counts only as
  being there. A link to a single file is hashed with that file wherever it is in the
  checkout, a generated directory included (`conftest.py -> .venv/c.py`). A link out of
  the checkout, a link loop, or a link to the checkout itself or a directory holding
  the link (`loop -> .`), counts by its text: those files already count at their own
  paths.
- **Under a linked directory, ignored files count.** Through `tests_link -> tests`,
  every file under `tests/` counts, gitignored or not, so a `.DS_Store` appearing there
  stales a pass, and a check writing to an ignored `tests/reports/` is refused. The
  reason says when a changed path is such a link.
- **A change put back is still a change.** Each file's change time is kept before and
  after the run (its modification time on Windows), inside nested repositories and
  submodules too, so a file that existed before the run and was edited and restored
  while the check ran, or a `git stash` and `git stash pop` in another shell, refuses
  the run though the bytes are the same.
- **Known limits:** a file that is created and deleted again while the run is going
  is not seen, since only files there before the run have a change time to compare.
  A rename that changes only letter case (`calc.py` to `Calc.py`) on a
  case-insensitive file system, as macOS and Windows have by default, is not seen
  either; git does not see it. And on Windows a change put back with its old
  modification time, e.g. via utime or by renaming the original back, is not seen.

**So check output goes in a gitignored directory, or outside the checkout.** The
directory must not be reachable through a symlinked directory (see below). Commit a
`.gitignore` line such as `reports/` and point the check there:

```sh
asq exec --project P --task 1 -- python -m pytest -q --junitxml=reports/a.xml
```

A check that writes `--junitxml=a.xml` into the checkout instead is refused: the file
is new during the run.

A run during which any source file is created, changed or removed is refused ("the
source changed while it ran"). A recorded pass goes `stale` when any source file is
new, changed or gone since the check, such as an added `conftest.py`. Both name the
first five files and say what to do: if the file is a check's own output, gitignore it,
delete it, or have the check write it outside the checkout; if it is source, record a
check of the source as it is now. In a checkout that is not a git repository, where
gitignoring does nothing, the reason says to delete it or write it outside.

`brief show` and `brief check` print the command a report pass ran (`ran: …`) under the
requirement's expected check, so a pass backed by the wrong command is plain to see. A report taken with `--task` backs only that task;
`--task` takes the task's number or id, as everywhere.

A run of any exit status can back `--verdict fail`, but it must still be a report taken
with `--project` for this brief's board, and, if it was taken with `--task`, for that
task.

A recorded pass also goes `stale` when its report changes or is gone. Reports that
recorded evidence cites are never pruned ([retention](command-reports.md)), and pruning
any other report changes no pass.

## Manual evidence

A check backed by `--artifact`, such as a screenshot or a saved log, is **manual**
evidence. Nothing in the CLI can tell whether the screenshot shows what the summary
says, so only the validator can judge it, and it is labelled manual everywhere: in
`brief show` (`pass (manual)`, `[manual]`), in the board event, and as `"kind":
"manual"` in `--json`.

The artifact is **copied** when the check is recorded, into
`~/.aisquare/briefs/<project>/<brief>/` (or under `$AISQUARE_HOME`), readable by
you only, and its SHA-256 is kept. The check stands on that copy. The original's
path is kept as provenance only, so a screenshot in a temporary directory, or in a
worktree the fleet later reaps, can be deleted without the check changing. If the
*copy* changes or goes away, a pass reads `stale`. A finding, or any failed check,
stays a failure whatever happens to its file. An artifact over 16 MiB is refused.

A manual pass also keeps the **whole-tree fingerprint** of the checkout it was recorded
in, and any change there (an edit, a new untracked file) makes it `stale`. Nothing can
say which files a screenshot covered, so every file counts. If that fingerprint cannot
be taken, a pass is refused; a failure is recorded without it.

## Findings

A failure someone finds goes on the brief, against the task whose work it is in:

```sh
aisquare brief finding B1 R2 --task 13 \
  --summary "The error wraps off the screen at 320 px" --artifact phone.png
```

What it does to the task depends on where the task is:

| The task is | After the finding |
| --- | --- |
| in review, or done | back to todo, unowned, with the finding as its reason |
| being worked on (doing) | keeps its owner and status; the owner reads the finding on the brief |
| dropped | stays dropped |

The third **distinct** failure by one task against one revision of a requirement
blocks that task, keeping its owner, so the manager re-plans the work instead of the
task going round review again. The same finding sent twice is one finding. A
correction to the requirement starts the count again. One task's failures never block
another task.

`brief evidence --verdict fail` has the same effect as a finding. A finding links the
task to the requirement if it was not linked yet; `brief evidence` needs the link
first.

## Corrections

When the request changes, correct the brief. It keeps its id, and every requirement
keeps its id:

```sh
aisquare brief update B1 -r "R2=A wrong password shows an error that fits at 320 px"
aisquare brief update B1 --add "Signing out returns to the login page"
aisquare brief update B1 --boundary "No single sign-on" --boundary "No password reset"
```

| What changed | Whose evidence goes stale |
| --- | --- |
| a requirement's text (`-r R2=…`) or its expected check (`--check R2=…`) | that requirement's |
| the assumptions (`--assumption`, `--clear-assumptions`) or the boundaries (`--boundary`, `--clear-boundaries`) | every requirement's, since what they change cannot be told apart |
| the title, or an added requirement | nobody's |

Linked tasks in review or done whose evidence went stale go back to todo. A task
being worked on keeps its owner, and a dropped task stays dropped. Nothing is thrown
away: the old checks stay on the brief, each requirement keeps the text and check it
had before, and `brief show` lists every correction with what it made stale.

Re-sending a value the brief already has changes nothing: no new revision, nothing
stale, no task moved. `--assumption` and `--boundary` replace the whole list, so pass
every entry you want to keep.

## Who wrote it

Every write takes `--as SESSION`, the way `task claim --as` does. The write and every
task it moves are attributed to that session, so a session's own writes never wake
it: a manager's own correction that reopens a done task does not bring the manager
back at its next Stop, while the same correction by a coder does.

Two writers never overwrite each other. Each write compares the brief's revision, and
one that lost the race is refused with `brief_changed`: read the brief and retry.

## Finishing a linked task

`aisquare task done` on a task linked to a requirement needs a **fresh pass recorded by
that task** for every requirement it is linked to: fresh meaning against the
requirement as it reads now and the source in its checkout now. Otherwise the task
stays as it is and each requirement is named with its state and why: `missing` (no
check recorded by this task), `failed`, `blocked` (the task is blocked) or `stale`:

```text
✗ #12 tsk_… cannot be done: it needs a fresh pass for each of
  B1 R2 [stale] the source in /work/app changed since the check ran
  B1 R3 [missing] no check recorded
```

With `--json`, the refusal is `{"error": "task_not_proven", "requirements": [{"brief",
"requirement", "state", "reason"}, …]}`. A task is gated only by the briefs that link
it: an unrelated or damaged brief elsewhere on the board never blocks it. A damaged one
that names it refuses with a fifth state, `unreadable`, and the reason it cannot be
read. The check and the status write are one
store transaction, so a correction that lands while `task done` runs fails it
(`brief_changed`: run it again). A task no brief links is done exactly as before.

## Checking a whole brief

```sh
aisquare brief check B1
aisquare brief check B1 --source-root ../wt-assembled
```

`brief check` prints every requirement's standing, each pass checked against the
source now, and exits 1 while anything is `missing`, `failed`, `blocked`, `stale` or
`unlinked` (no live task is linked to it: its tasks were all dropped, or it was never
linked); 0 means the brief is proven. By default each pass is compared with the
checkout it was recorded in. `--source-root` compares every pass with that one
checkout instead,
which is the check a validator runs on the assembled work. It must be this project's
directory or one of its worktrees (the same repository), or a checkout one of its
checks was recorded in. Anything else, an unrelated repository included even under
`AISQUARE_TEAM_HUB`, is a usage error with exit code 2, not a page of `stale`, so a
script can tell a typo from NOT PROVEN.
Passes that rest on manual evidence are listed for the validator to judge.

On an assembled checkout, the passes recorded in each worktree usually read `stale`:
the merge changed files they covered, a CHANGELOG at least. Each reason names the
first files that changed. To prove the brief on the assembled work, run each
requirement's check there, without `--task`, and record that one report for each
linked task: `aisquare exec --project PROJECT -- CHECK` in the assembled checkout, then
`aisquare brief evidence BRIEF R1 --task TASK --verdict pass --summary … --report ID`
once per task. `brief check BRIEF --source-root ASSEMBLED` then reads PROVEN.

## At session start

An agent whose task (the one it was spawned for, or one it holds) is linked to a
brief sees those requirements when its session starts: each one's text and expected
check, clipped, and its state **as recorded**. Reading a whole checkout costs a hook
too much, so a pass is shown as recorded and `brief check` is what re-checks it. The
block takes at most 2,500 characters, and only the room the rest of the session start
leaves under Claude Code's 10,000-character limit: on a busy board it shows fewer
requirements, then only a one-line pointer to `asq brief show`, then nothing, and it
never cuts a line or its closing tag. If it cannot be built, only the block is lost,
never the board.

## Limits

A brief holds up to 64 requirements and 1,024 evidence records; a requirement or a
check is up to 2,000 characters, a summary up to 4,000, and an artifact up to 16 MiB.
Briefs live in the board's store (`context.db`) and their artifacts' copies under
`briefs/`, and `project forget --purge` deletes both with the rest of the project's
rows.
