# Usage & stats

*Settings ▸ Usage & stats* shows what the Claude Code sessions on this machine have spent —
tokens, cost at list prices, sessions, repositories, accounts, models — for **Today**, **7 days**,
**30 days** or **Lifetime** (keys `1`–`4`). `aisquare metrics stats --range today|7d|30d|lifetime
[--json]` prints the same figures.

Everything here is recorded and shown **on this machine only**. Nothing is sent anywhere unless you turn on the separate, optional [usage sharing](usage-telemetry.md) (off by default; the switch is at the top of the page).

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
- **Every repository you run Claude Code in, including ones you have not added to aisquare.**
  The sweep counts them regardless of `[projects] capture` (which only decides whether a hook
  writes rows for a directory nobody added); capture settings do not change this. A repository
  you never added has no project, but it has a usage profile (its root, remote and coarse
  traits) like any other. With [usage sharing](usage-telemetry.md) on, those repositories are
  part of the counts that are sent, as a salted hash and four coarse traits each, like the rest.
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

## Forgetting a project

`aisquare project forget --purge` (and `prune --purge`) deletes a project's usage rows and its
repository profile for good, and **leaves a tombstone** so that the sweep does not read the same
transcripts again (Claude Code keeps them for about a month): the repository is not counted,
shown or shared again. The tombstone ends when the project is added back on purpose
(`project onboard` / `team on`); from then on its usage is counted again, from whatever
transcripts Claude Code still has. A directory merely captured by a hook does not end it.

## Repositories

A repository is identified by its **normalised git remote** (`github.com/org/repo`), and by its
root path when it has none. So the clones and checkouts of one remote — `ws2` to `ws9` of the same
project — are **one repository** on the page, in the count of active repositories and in a day's
rollup (one entry per repository, keyed by the remote). The name shown is the one of the checkout
that spent the most. A repository is merged once it has been profiled (the sweep profiles a few
a pass), so a repository seen for the first time counts alone until then.

## How days are cut

Days are **local** calendar days. A turn belongs to the day it ended on; the sweep cuts a turn
that crosses local midnight in two, each half charged to its own day. *Today* is since local
midnight; *7 days* and *30 days* are that many local days, today included. *Lifetime* is
everything recorded, with no end: it equals `aisquare metrics tokens` for the same sessions. The
sweep never dates a turn after the moment it reads it, so a transcript from a machine whose clock
was ahead is charged to today rather than to a day that has not come.

## Known limitation

Counting is deduplicated per session, by message id. Two session files that carry the *same*
messages — a transcript copied or forked from another — would count them twice. Claude Code's
own transcripts do not do this (none of ~640 on the development machine shared a message id
with another), and the Stop hook has always had the same limit; a machine-wide message-id
index was left out as the cost of a lookup per request for a case not seen.

## Where it lives

`~/.aisquare/context.db` (schema v40): `usage_session` and `usage_turn` (tokens; sessions now
carry `account_slot`, `account_type`, `repo_id`), `repo_profile` and `feature_use`. The figures
are summed on read by `services/usage_stats.py` — `stats(range)` for the page and the command,
`daily_rollups(since, until)` for one record per active day.
