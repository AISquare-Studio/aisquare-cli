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

The brief is `B1` on this board (its id works too, or any unambiguous part of it).
Link the board tasks that build it. One task can cover several requirements, and one
requirement can need several tasks:

```sh
aisquare brief link B1 12 -r R1 -r R2
aisquare brief link B1 13 -r R2
```

When a task's work is checked, record what the check found, with the file that shows
it:

```sh
aisquare brief evidence B1 R1 --task 12 --verdict pass \
  --summary "Signed in as a test user and the dashboard opened" \
  --artifact dashboard.png
```

Each linked task proves its own part: `R2` above stands at `missing` until both #12 and
#13 have recorded a check of it. `aisquare brief show B1` prints every requirement's
standing, the checks behind it, and the corrections the brief has had;
`aisquare brief list` lists the board's briefs.

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

## Not yet

These are the next part of the work (the evidence-gate handoff,
[#275](https://github.com/AISquare-Studio/aisquare-cli/pull/275)), and are not
in this build: checks backed by a saved command report (`asq exec`) and the source
fingerprint they are compared against; `task done` refusing a linked task without a
fresh passing check; `brief check`, the one command that says which requirements are
proven; and an agent's linked requirements shown at session start. Until then, a
linked task is marked done exactly as before, and `brief show` is where the standing
is read.

## Limits

A brief holds up to 64 requirements and 1,024 evidence records; a requirement or a
check is up to 2,000 characters, a summary up to 4,000, and an artifact up to 16 MiB.
Briefs live in the board's store (`context.db`) and their artifacts' copies under
`briefs/`, and `project forget --purge` deletes both with the rest of the project's
rows.
