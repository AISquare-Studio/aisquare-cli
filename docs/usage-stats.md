# Usage & stats

*Settings ▸ Usage & stats* shows what the Claude Code sessions on this machine have spent —
tokens, cost at list prices, sessions, repositories, accounts, models — for **Today**, **7 days**,
**30 days** or **Lifetime** (keys `1`–`4`). `aisquare metrics stats --range today|7d|30d|lifetime
[--json]` prints the same figures.

Everything here is recorded and shown **on this machine only**. Nothing is sent anywhere.

## What is recorded

- **Tokens, per API request**, read off Claude Code's own transcripts (the `message.usage` of
  every request, counted once per message id, subagents included). Totals are the ones
  `aisquare metrics tokens` reports for the same sessions.
- **Every session, not only the ones aisquare launched.** A Stop hook records the sessions it
  watches as they go. The *sweep* reads the transcripts of every other Claude Code session in
  every repository, under `~/.claude` and under each account registered with aisquare. It runs
  when the page opens and from the TUI at most every ten minutes, and as
  `aisquare metrics backfill`. A session the hook counted is never counted again by the sweep:
  both keep one scan position per session (see `core/usage.py`). Claude Code deletes a transcript
  after `cleanupPeriodDays` (30 by default), so the first sweep records the history that still
  exists and later ones keep it.
- **Which account** each session ran under (its slot, and the subscription type — `pro`, `max`,
  `team`, … — read from the login) and **which repository** (the git root of the directory it ran
  in; worktrees count as their repository; scratch repositories in temporary directories are
  counted as one).
- **A profile per repository**, refreshed at most daily: its name, root, normalised remote
  (`github.com/org/repo`, no credentials), host (`github`, `gitlab`, `bitbucket`, `other`,
  `none`), main language by tracked-file extensions, size bucket (`xs` ≤ 50 files, `s` ≤ 500,
  `m` ≤ 5 000, `l` ≤ 50 000, `xl`) and whether it is a monorepo.
- **Which features you use**: a count per local day of each command path (`cli.fleet.spawn`) and
  each TUI page (`tui.view.usage_stats`) — names only, never arguments. Hook, MCP-server and
  `uninstall` runs are not counted. Counting is one appended line to `~/.aisquare/feature-use.log`
  (microseconds, no database, failing silent) that the next read folds into the store.

## How days are cut

Days are **local** calendar days. A turn belongs to the day it ended on; the sweep cuts a turn
that crosses local midnight in two, each half charged to its own day. *Today* is since local
midnight; *7 days* and *30 days* are that many local days, today included.

## Where it lives

`~/.aisquare/context.db` (schema v40): `usage_session` and `usage_turn` (tokens; sessions now
carry `account_slot`, `account_type`, `repo_id`), `repo_profile` and `feature_use`. The figures
are summed on read by `services/usage_stats.py` — `stats(range)` for the page and the command,
`daily_rollups(since, until)` for one record per active day.
