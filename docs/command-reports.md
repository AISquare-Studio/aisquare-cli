# Native command reports

AI Square can run a command once, give the agent a shorter useful report, and
keep the original bytes locally. This is AI Square code; RTK is not installed.

```sh
asq exec -- git status
asq exec -- python -m pytest -q
asq exec --session SESSION_ID --task TASK_ID --project PROJECT_ID -- python -m pytest -q
```

The `--` separates wrapper options from the exact command and arguments. No shell
is added and no command flags are rewritten. Shell expansion done by your own
shell before calling `asq` still works normally. This wrapper is for finite,
non-interactive commands: child output uses pipes, not a terminal. Run interactive
programs directly. Standard input is inherited.

Put AI Square global output flags **before** `exec`:

```sh
asq --json exec -- python -m pytest -q
```

The child's `--json`, `--quiet`, `--profile`, and other flags after the separator
belong to the child. A JSON response contains one object with factual report
metadata, output, and byte measurements. The wrapper returns the child's exit
status. Signal termination is recorded and reproduced after the report is saved.
An executable that cannot be started produces a saved failure with exit 126/127.

## What becomes shorter

Ordinary Git status keeps branch information, every path/status, and conflicts;
recognised repeated Git usage hints can be omitted. Machine formats such as
`--porcelain`, `--short`, and `-z` pass through unchanged.

Pytest keeps test counts, failures, warnings, and details. Recognised progress
lines ending with a percentage can be omitted when a normal final count is
present, and only from the collection zone before the first section banner: a
look-alike line inside a failing test's captured output is evidence and stays.
Output capture explicitly changed with `-s` (also folded into a cluster such as
`-sv`) or `--capture` disables this compaction. Lines longer than 512 bytes are
never offered to the progress matcher. Unrecognised output, colour/control sequences, binary data, and
truncated results pass through. Standard error is always retained unchanged.
There is no AI summariser, external service, automatic hook, or broad claim of
support for all test runners.

Agents must actually invoke the wrapper to receive its shorter report. Merely
installing AI Square does not intercept every command or the agent's built-in
Read/Grep tools. Existing JSON task-board commands should run directly.

## Recover original output without running anything again

Each invocation returns an ID:

```sh
asq reports list
asq reports show REPORT_ID
asq reports show REPORT_ID --raw
asq reports show REPORT_ID --raw --stream stdout
asq --json reports show REPORT_ID --raw
```

`show` reads saved bytes. It never executes the saved command, even if that
command wrote files or originally failed. A successful retrieval exits zero;
the original command status is in its receipt/metadata. `--stream stderr` sends
the saved error stream to standard error and puts the receipt line on standard
output, so the recovered stream is byte-exact; otherwise the receipt goes to
standard error. `both` preserves the two separate streams, not their original
interleaving.

Raw output sent to a pipe preserves bytes. Terminal displays escape control
characters. JSON `--raw` returns base64 for exact binary recovery; regular JSON
uses text with controls escaped. Original files always retain their binary bytes.

## Limits and controls

By default, each original stream retains its **first 1 MiB**. Both streams are
still drained and the child is allowed to finish. Larger output is explicitly
marked `TRUNCATED`, with observed and retained byte counts. Anything after that
retention limit is not available for recovery. `--max-output-bytes` changes the
per-stream limit up to 16 MiB for a particular invocation.

Completed reports are kept under `~/.aisquare/reports` (or `AISQUARE_HOME/reports`)
with private directory/file permissions. A report is published only after its
originals and metadata have been written. New commands apply a default retention
policy of 14 days and the newest 64 completed reports. A report that backs
recorded requirement evidence (`asq brief evidence --report`) is protected: it
neither counts against the 64 nor expires, because deleting it would turn a
verified brief stale. In-progress reports are not deleted by retention; an
in-progress directory whose wrapper process is gone for more than an hour is
treated as abandoned and removed.

Signals: while the command runs, SIGINT, SIGTERM and SIGHUP are forwarded to its
process group. A second interrupt escalates to SIGTERM and a third to SIGKILL, so
a command that ignores Ctrl-C cannot hold your terminal. The report is still
saved, with `interrupted_by` recording what you sent; an interrupt that arrives
after the command finished (during the source scan or compaction) is honoured
only after the report is published.

```sh
asq exec --raw -- python -m pytest -q
AISQUARE_REPORTS=off asq exec -- python -m pytest -q
asq reports prune --keep 20 --days 7
asq reports stats
```

`--raw` and `AISQUARE_REPORTS=off` disable compaction while keeping recovery.
Pruning removes saved reports, not project files. An expired/deleted result
cannot be retrieved.

Statistics measure original, retained, and displayed **body bytes**. Token counts
are explicitly estimates using bytes divided by four; they are not provider
usage. Receipts, prompts, other tools, and extra model turns are outside these
numbers. Truncation is not counted as compaction. Reports carry the exact working
directory, optional caller-supplied project/task/session IDs, and a separately
labelled Explainability pipeline ID when available. No personality text is added
to agent reports or factual metadata.

## Use a report as requirement evidence

Passing `--project PROJECT_ID` opts into source capture before and after the
command. AI Square hashes that project's actual checkout, including changed and
untracked nonignored files. Linked worktrees use their own files while retaining
the shared board project ID. Source scans can cost time on large projects;
ordinary commands without `--project` do not scan the source tree.

A saved report carries both fingerprints. A successful process exit is not by
itself proof that a requirement is satisfied: the command must check the relevant
outcome, and its source must match the version being validated. Native brief
report evidence checks the recorded process outcome and source identity. Changed
source during the command, changed source before evidence is recorded, an unknown
source identity, or a failed process cannot be submitted as a fresh successful
command check. The validator still evaluates whether the check covers the request.

If source capture fails, the command still runs and its ordinary output remains
available. The report explicitly records unknown proof; it cannot then provide
fresh source-backed evidence. No persona setting changes these facts.
