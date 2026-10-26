# Command reports

`asq exec` runs a command once and saves a report of how it ended: its exit status,
both output streams, and, if you ask for it, which source it ran against. A later step
can cite the report instead of running the command again.

```sh
asq exec -- python -m pytest -q
asq exec --project PROJECT_ID --task TASK_ID --session SESSION_ID -- python -m pytest -q
asq reports show REPORT_ID
```

## Running a command

Everything after `--` is the command and its arguments, passed on unchanged. No shell
is added, so `$(…)`, globs and quotes mean only what your own shell already made of
them. Standard input is inherited. The output goes through pipes, not a terminal, so
`asq exec` is for finite, non-interactive commands; run interactive programs directly.

Put global flags such as `--json` **before** `exec`. A `--json` after `--` belongs to
the command, and an option right after `exec` that `exec` does not know is refused
(exit 2), not run as the command:

```sh
asq --json exec -- python -m pytest -q
```

| Option | What it does |
|---|---|
| `--project ID` | Records the checkout's source identity before and after the command. See below. |
| `--task REF` | The task the run is for: its number on the board (`12`, `#12`) or its id (prefix ok), on the `--project` board or yours, recorded as the task's id. A ref the board does not have is refused before anything runs. With no board (the orchestrator off), it is recorded as given. |
| `--session ID` | Recorded on the report as given. |
| `--raw` | Shows the output unshortened. The originals are kept either way. |
| `--max-output-bytes N` | Bytes kept per stream: 1 MiB by default, at most 16 MiB. |

`asq exec` exits with the command's status. A command that died of a signal is
recorded, and then `asq exec` dies of the same signal, so a caller sees what it would
have seen without the wrapper. A command that cannot start is a saved failure: 127
when it was not found, 126 otherwise.

The command's output is printed, followed by one line on standard error:

```
[asq report 3f2a…; exit 1; pytest]
Originals: asq reports show 3f2a… --raw (saved bytes; never reruns).
```

The line also says when a stream was truncated, when you interrupted the command,
when a background process held the output open, and when a `--project` source
identity is unknown.

## Interrupts and background processes

While the command runs, Ctrl-C, SIGTERM and SIGHUP go to its whole process group. A
second interrupt is sent as SIGTERM and a third as SIGKILL, so a command that ignores
Ctrl-C cannot hold your terminal. The report is still saved, and `interrupted_by`
records the first signal you sent, even when the command caught it and exited 0.

After the command has finished, an interrupt that arrives while its output is still
being read (the drain below, up to ten seconds) is recorded in `interrupted_by` too, so
the report backs no pass, and the output is printed as usual. One that arrives while the
report is being saved waits until it is saved; then `asq exec` prints the report's ID and
exits 130 (128 + the signal) without printing the output.

On Windows, an interrupt that arrives before the command has started is recorded in
`interrupted_by` but never reaches the command, which runs on. That fails safe: the
report backs no pass.

If the command exits but leaves a process holding its output open, as
`sh -c 'setsid server & echo started'` does, `asq exec` does not wait for that process.
It keeps reading until two seconds pass with no output, and never more than ten
seconds after the command exited, so a process that keeps writing cannot hold it either.
Then it stops, returns, and records `output_pipes_held_open`. Anything written after
that is not in the report.

On Windows there are no process groups to signal: the command shares the console and
receives Ctrl-C itself, and a second or third Ctrl-C terminates it.

## What is shortened

Only two kinds of standard output, and only for display:

- `git status`: Git's repeated usage hints, such as `(use "git add <file>..." …)`.
  Branch, paths and conflicts are kept. `--porcelain`, `--short`, `-s` and `-z` pass
  through unchanged.
- `pytest` (or `python -m pytest`): the percentage progress lines before the first
  section banner, and only when a final count such as `3 passed in 1.2s` is present.
  Failures, warnings and the summary are kept, and so is a look-alike line inside a
  failing test's captured output. `-s` (also in a cluster like `-sv`) or `--capture`
  turns this off.

Standard error, every other command, unrecognised output, binary data, colour and other
control sequences, and truncated output pass through unchanged. `--raw` or
`AISQUARE_REPORTS=off` turns shortening off.

## Reading a report back

```sh
asq reports list
asq reports show REPORT_ID
asq reports show REPORT_ID --raw
asq reports show REPORT_ID --raw --stream stdout
asq --json reports show REPORT_ID --raw
```

`show` reads the saved bytes and never runs anything. It exits 0 when the report was
read, whatever the command's own status was; that status is in the footer and the JSON.
`--stream stderr` writes the saved standard error to standard error and puts the footer
on standard output, so the stream you recover is byte-exact. `both` keeps the two
streams apart; their original interleaving is not recorded.

On a terminal, control characters are shown escaped. Piped, `--raw` writes the exact
bytes. Under `--json`, `--raw` gives base64.

`asq --json reports show` returns one object: the whole report under `"report"`, its
`"metrics"`, the `"encoding"` and the `"stdout"` and `"stderr"` text. A later step can
check the report without re-running anything. Its fields: `returncode`, `signal`,
`interrupted_by`, `launch_error`,
the `stdout` and `stderr` byte counts with `truncated`, `output_pipes_held_open`, `cwd`,
`source_root`, `source_fingerprint_before`, `source_fingerprint_after`,
`source_capture_error`, the `project_id` and `session_id` you gave, and the `task_id`
your `--task` resolved to.

## Source identity: `--project`

With `--project PROJECT_ID`, `asq exec` takes the checkout's source identity just
before the command and again just after. The identity is a hash of every tracked and
untracked nonignored file, kept file by file.

- **Content, not history.** Git HEAD is not read, so an empty commit, or a branch
  switch that changes no byte, keeps the identity.
- **Build output is left out.** Untracked files under `__pycache__`, `node_modules`,
  `.venv`, `venv`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox` and the like
  do not count. Tracked files count whatever directory they are in.
- **Symlinks count as their targets.** A link to a file or directory inside the
  checkout is hashed with what it points at, links inside it the same way, and
  generated directories skipped; a linked file counts by its content even inside a
  generated directory. Under a linked directory, ignored files count too. A link
  pointing out of the checkout, a loop, or a link to the checkout itself or a
  directory holding the link, counts by its text.
- **Change times are kept too.** Each file's change time (modification time on
  Windows) is saved before and after, nested repositories included, so a file
  changed and put back while the command ran still shows. A file created and deleted
  while it ran does not, and on Windows a change put back with its old modification
  time, e.g. via utime or by renaming the original back, is not seen. They are not
  part of the identity: a branch switch that rewrites a file with the same bytes
  keeps it.
- **Nested repositories count.** A submodule or nested repository adds its own
  files, not only the commit it records.
- **Ignored files are left out, the rules that ignore them are not.** Files git
  ignores do not count, but every `.gitignore` git reads does (one that ignores itself
  too; not one inside an ignored directory, which git never reads), and so do
  `.git/info/exclude` and `core.excludesFile`. Everything else counts, a file the
  command itself wrote included: `pytest --junitxml=report.xml` changes the identity,
  as an edit or a removal does. When a report backs brief evidence, the identity must
  be the same before, after and now, so write check output to a gitignored directory
  that no symlinked directory reaches (`--junitxml=reports/a.xml` with `reports/` in
  `.gitignore`) or outside the checkout ([work briefs](work-briefs.md)).
- **A worktree is its own checkout.** A command run in a linked worktree is about that
  worktree's files, under the board's one project.
- **The home directory is refused.** A directory that is, or contains, your home or
  `~/.aisquare` is never walked.

If the identity cannot be taken (an unregistered project, a working directory outside
it, a git error, an unreadable file, a file that changed while it was read), the
command still runs, and the report says its proof is unknown (`source_capture_error`).
Commands without `--project` never scan the source.

A zero exit is not by itself proof that something works: the command has to check the
thing you care about. The report only makes it possible to show that it ran, on which
source, and how it ended.

## Where reports live, and for how long

Reports are kept under `~/.aisquare/reports` (or `$AISQUARE_HOME/reports`), one
directory each, readable by you only on Linux and macOS. A report appears there only
once its streams and metadata are fully written; a run in progress lives in a
`.pending-…` directory. That directory is removed only once its `asq exec` is gone and
it is over an hour old; a run whose `asq exec` is still alive is never removed. On
Windows, where that cannot be asked, a directory over a day old is removed.

Each run keeps the first 1 MiB of each stream (`--max-output-bytes` raises it to 16
MiB). Truncation is marked with the observed and kept byte counts, and what was past
the limit cannot be recovered.

Every `asq exec` applies retention first: reports older than 14 days go, and so do all
but the newest 64. A report captured with `--project` is kept for the full 14 days
however many newer runs there are, so it is still there when its evidence is recorded.
Retention never removes a report that recorded evidence cites
([work briefs](work-briefs.md): `asq brief evidence --report`), and does not count one
toward the 64. Those reports stay, whatever `--keep` and `--days` say, until the briefs
that cite them are deleted (`aisquare project forget --purge`, or a Start fresh:
`aisquare project fresh`). A Start fresh deletes no report itself; with their briefs gone
the reports they cited are pinned no more (unless another board's brief cites one too),
and retention takes them as it takes any other. `asq reports prune` says
how many it kept for that reason (`kept_for_evidence` under `--json`). When the list
of those reports cannot be read, nothing is removed.

```sh
asq reports prune --keep 20 --days 7
asq reports stats
```

`prune` removes saved reports, never project files. `stats` gives measured byte counts
and token counts estimated as bytes divided by four. They cover the saved report bodies
only, not prompts or other tools, and truncation is not counted as shortening.
