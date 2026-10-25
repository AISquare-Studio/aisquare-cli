# Removing the codebase snapshot

> **Status: the plan of PR #399, implemented there.** #399 removes the codebase snapshot
> and its packer from the CLI completely, on `main` after #274 (merged at `8eb18a8a`). It
> ships in 0.9.0: the owner had it merged into #401's branch (`feat/versioned-installers`),
> the 0.9.0 release PR, on 2026-10-25, so its CHANGELOG entries sit under `[0.9.0]`. The
> owner's decisions of 2026-10-24 are recorded below. This file began as a handoff (#57,
> `4b9ae1fd`); what that handoff found, with its names and line numbers, is kept in
> [History](#history).

## Why

aisquare packed a project's codebase into `~/.aisquare/projects/<id>/snapshot/`: a full
pack, a compressed "skeleton", a per-file index and a metadata file. Session start used to
point every agent at it ("read the skeleton FIRST"), often about 110k tokens, and nothing
showed agents used it well. #346 (folded into #274) stopped pointing agents at it and
stopped `init` packing. What was left only cost and went stale: `project onboard` still
packed, doctor had three rows for it, `install.sh` installed Node for it, old packs sat
on disk, and `[snapshot]` stayed in the config.

## The owner's decisions (2026-10-24)

The handoff's three questions, answered:

1. **Does anything on the AISquare platform expect packs made by the CLI?** No. The
   "future sync" the packer's docstring promised will be built as a new system when that
   work starts. Remove the snapshot completely from the CLI, with no server-side note.
2. **Edit the pitch deck in this PR?** Yes, and the other decks and their PDFs where they
   mention it. A PDF that cannot be regenerated here is said so in the PR and the board
   note, not left stale silently.
3. **Keep `install.sh`'s Node step?** Yes, as an opt-in for the Chrome DevTools MCP, built
   so more optional packages can be added: a small list of optional extras, one prompt
   each. Every line that said the packer needs Node says the new reason. Doctor must not
   go amber over a missing Node. `install.sh` and `install.ps1` set up no MCP then (both
   checked), so registering one went to the owner as a question.

Later the same day: #274 merged into `main`, so #399 is not retargeted; its base stays
`main`, and `origin/main` is merged into it (merge commits only). And the owner answered
the MCP question:

4. **Register the Chrome DevTools MCP?** Yes, opt-in. When the user chooses the Chrome
   DevTools extra, `install.sh` installs Node and then registers the MCP with Claude Code
   at user scope. It skips in one line when Claude Code is missing or the MCP is already
   registered; a failed registration warns, never fails the install, and doctor stays
   green. The survey and the plan line name it, and the summary says how to undo it
   (`claude mcp remove -s user chrome-devtools`). Never when the extra is not chosen,
   under `--yes` without it, or in a CI cell that does not opt in; the matrix grades what
   it graded.

## What #399 does

### The store first: an old pack is still evidence

`_adopt_onboarded_projects` (`core/store.py`) is the v16 → v17 backfill (#139). It runs once
per store, in the transaction that adds `project.onboarded_at`, and decides which existing
registrations were used on purpose (listed) rather than captured from a prompt (hidden).
One mark it reads is a pack on disk: before #346, `project onboard` and `init` always
packed. A store from 0.6 or older, upgraded straight to this build, still reaches it, and
for a project the owner added and never used, the pack is the only mark.

- `_snapshot_on_disk` is a plain path check,
  `paths.project_data_dir(id) / "snapshot" / "snapshot.json"`, inside the same
  `OSError` guard (review of #203: an unreadable directory is no evidence). It imports no
  packer.
- The packs are deleted only after the store has migrated: `open_store` calls
  `_remove_old_packs()` right after `_migrate`, never before.
- `test_a_pack_is_the_evidence_first_and_deleted_after` pins it: a v16 store whose only
  evidence is `snapshot/snapshot.json` lists the project after the open, and the pack is
  gone, with `brain/` beside it kept. Mutations checked: the cleanup moved before
  `_migrate`, the resolve guard dropped, the marker dropped; each fails a test.

### The one-time cleanup

`_remove_old_packs` (`core/store.py`) deletes every `projects/<id>/snapshot/`, once:

- only a directory named `snapshot`, directly in a project's data directory, and only
  while it resolves to one inside `projects/`; a symlink is never followed out of the
  home, and `brain/` and the explainability key beside it are never touched;
- best effort: what cannot be deleted stays, and the open goes on, and so does a
  `projects/` it cannot list or resolve (`RuntimeError` too: Python 3.11 and 3.12 raise
  it for a symlink loop; review of #399, pinned by
  `test_a_projects_dir_the_cleanup_cannot_read_never_breaks_the_open`);
- a marker, `cache/old-packs-removed`, records that it ran, so a later open costs one
  stat. `cache/` is disposable: run again, the cleanup finds nothing to delete.

The aisquare home's own old pack (D5) goes with every other; `init` needs no rule of its
own any more.

### The packer goes

- `core/snapshot.py` is deleted, with `SnapshotSettings` and `AppConfig.snapshot`, the
  `Snapshot` model, `OnboardReport.snapshot` and `.snapshot_note`, `emit_onboard`'s
  snapshot lines, `init`'s note and `SNAPSHOT_OPT_IN`, and the three spawn seams
  (`head_sha`, `node_version`, the packer).
- **`project onboard`** adds the project on purpose and seeds facts: the first run seeds
  one fact per marker in `_ECOSYSTEM_MARKERS`, a plain second run seeds nothing, and
  `--refresh` reads the markers again and adds only facts that are new. It starts no
  process but the `git rev-parse` that finds the root (pinned by
  `test_onboard_and_refresh_seed_facts_and_start_nothing_but_git`). A plain run on a
  project already onboarded prints `✓ already onboarded; --refresh reads the markers
  again`, since it read none; a run that read them and found nothing new prints
  `✓ onboarded — no new project facts to seed`. A forgotten project onboarded again is
  a first run, and prints `✓ onboarded again; its earlier facts are back`, since forget
  kept them (`restored` in `--json`; review of #399, m12).
- **`[snapshot]` in `config.toml`** keeps loading, since unknown keys are ignored. An
  unknown section is otherwise kept by a save (`_keep_unknown`), because a newer build may
  read it; `snapshot` is in `RETIRED_SECTIONS`, so the next save drops it. Until then
  doctor's config row says, in green, `[snapshot] is no longer read (aisquare no longer
  packs a codebase), and the next save drops it`. That is the one-time note: it lasts
  until the save that removes the section. `config get/set snapshot.*` are unknown keys.
- **Doctor** loses its packer, `tiktoken` and `snapshot` rows, the Node floor and
  `_has_module`. `tests/test_doctor_needs_no_node.py` runs the whole doctor with no
  `node`, `npx` or `npm` on PATH: no row is about them and nothing amber names them.
  Doctor's browser tools row names the command `install.sh --with chrome-devtools` runs,
  `npx chrome-devtools-mcp@latest`: it is green, and it is advice for a tool the user
  picks.
- **tiktoken** counted pack tokens and nothing else, and it was never a dependency in
  `pyproject.toml`, so `uv.lock` does not change. `install.sh` still adds it, on
  purpose: the script installs the latest *release*, and a release up to 0.8.2 has a
  `tiktoken` doctor row that goes amber without it on a machine with Node, which the
  Chrome DevTools extra makes. The container matrix measured it: run 4 upgraded to the release,
  its doctor called tiktoken unexpected, and the run exited 2. So `--with tiktoken`
  stays, with a comment saying when to drop it, and Remote's fallback hint matches.
- **The one-click fix** `project onboard --refresh` stays in `onboarding.KNOWN_FIXES`: a
  valid command, the only project-scoped one, whose UI path (disabled without a project)
  stays tested. No doctor row names it today; the tests' fixtures say they stand in for
  one.

### `install.sh`: the Chrome DevTools MCP as an opt-in extra

- `OPTIONAL_EXTRAS="chrome-devtools"` lists the extras. Each has three functions, called
  by a name built at run time with its dashes as underscores (`extra_call`):
  `extra_<name>_line` (what the prompt and the plan call it), `extra_<name>_current`
  (true when there is nothing to do) and `install_<name>`.
  `test_every_optional_extra_has_its_three_functions` holds the list to that. The extra
  was `node` until the owner's fourth decision: it now writes Claude Code's config, so
  its flag says what it adds.
- Each extra that is not already there is asked about with its own prompt, which defaults
  to no, or chosen without asking with `--with NAME`. `--yes`, a run with no terminal and
  `--no-system-deps` choose none unless named. An unknown name is refused.
- Node's floor is 22.12: chrome-devtools-mcp@1.12.0 declares
  `^20.19.0 || ^22.12.0 || >=23`, and NodeSource and fnm are asked for the 22 line. The
  three sources (platform package, NodeSource, fnm) are unchanged. npm is installed beside
  Node only for a run that chose it.
- Node no longer blocks the short-circuit; a current machine without the extra is told
  `optional: … — rerun with --with chrome-devtools`. Exit 2 means an extra the run chose
  did not install (its Node); no doctor row can say so.
- **The registration** (`register_devtools_mcp`) runs after `install_claude`, so a Claude
  Code the run installs is there to take it, and only for a run that chose the extra. It
  runs the package's own command, `claude mcp add chrome-devtools --scope user npx
  chrome-devtools-mcp@latest` (its `docs/client-configurations.md`, read 2026-10-24;
  npm's latest is 1.12.0, whose engines set the Node floor). One line instead, and no
  call, under `--no-agent`, in a dry run, with no Node 22.12+, or without Claude Code;
  a warning when Node has no npx. `claude mcp add` refusing an existing name is the same
  one line; any other failure warns with the command to run by hand, and the run's
  status is untouched. The summary prints the undo command when this run registered it.
- **"Already registered"** is read from the files, not asked of `claude`: `claude mcp get`
  and `claude mcp list` start every server they show to health-check it (measured with
  Claude Code 2.1.310: 30 seconds and an `npx chrome-devtools-mcp@latest` download each).
  A user-scope server is a key at four spaces in `${CLAUDE_CONFIG_DIR:-~}/.claude.json`,
  as Claude Code writes it (a project's local servers sit deeper and do not count), and
  an enabled chrome-devtools-mcp plugin counts, since its guide says not to have both.
  Checked against files the real `claude mcp add` wrote: user scope, local scope only,
  and `CLAUDE_CONFIG_DIR`. The survey reports it, and the extra is current when Node is
  at the floor and the MCP is registered (or `--no-agent`).
- The banner's "Written to" names `~/.claude.json` for a run that will register it.
- **The installed release's rows stay expected** (`RELEASE_ERA_AMBER`: `repomix`,
  `snapshot`, `tiktoken`), whatever the flags, until a release without them is the latest
  on PyPI. The script installs that release, and its doctor still has them: `repomix`
  goes amber with a Node below 22 or no npm, which the script no longer upgrades
  unasked, and 0.8.2's `snapshot` with no project or no Node to pack with. Review of #399
  (cycle 1, MAJOR 1) measured `--yes` on Debian 12's own Node 18 exiting 2 on `repomix`.
  The summary names them in one line, and the short-circuit's reason says so.
  `tests/install/release-cell.sh`, a job of its own in `install.yml`, installs the
  release from PyPI onto Debian 12's Node 18 and npm with `--yes` and no extra, and
  asserts exit 0, Node untouched, and a `--no-system-deps` re-run with nothing to do;
  every other cell grades this tree's wheel and could not see it.
- The extra is current only with npx too, so a Node 22 without npm no longer
  short-circuits past `_ensure_npx`, and an npm that does not install warns where it
  happened and counts as the extra not installing (exit 2). `--with` named twice
  chooses once. The prompt's default is pinned at no.
- The edits stay in the Node step, the extras, the packer's lines and their summary and
  plan lines, away from #59's pin lines.
- The container cells pass `--with chrome-devtools` on run 1, so the Node install paths
  keep their coverage on the five distributions. Every matrix cell runs `--no-agent`, so
  none registers anything, and run 1 asserts that; the one cell with Claude Code installed
  for real asserts the registration is in its `.claude.json`. Runs 2 to 4 name no extra.

### Docs and decks

- `docs/memory.md`, `docs/reference.md`, `README.md`, `SECURITY.md`, `docs/captain.md`,
  `docs/fleet.md` and `docs/install.md` describe what ships: no pack, and the Chrome
  DevTools MCP optional. `docs/install.md` says the server sends Google usage statistics
  unless `CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS` is set, as its README says.
- The three decks: the doctor capture's two rows for the packer and tiktoken become
  `self-invocation` and `browser tools` (eighteen green still), the session capture's
  "snapshot skeleton" goes, and every sentence and figure label that promised a skeleton
  or a pack is rewritten. Every added character was already on its page, so the embedded
  font subsets cover it. The decks' generator lives outside this repo and needs the same
  edits. The PDFs: see the PR body for which could be regenerated here.
- `docs/plans/one-line-install.md` and `docs/plans/fleet-tui.md` are historical plans; each
  carries a status note at the top rather than a rewrite.
- `CHANGELOG.md`: under `[0.9.0]`, a Changed entry for `install.sh` and a Removed
  entry for the snapshot. Released entries are left as they are. Three of 0.9.0's own
  lines the removal made untrue in the same release are taken out: the captain home is
  never packed, `init`'s pack is opt-in through `project onboard`, and the docs sweep
  skips worktrees the way `core/snapshot.py` did.

## Not this snapshot: kept

- `services/ci_snapshot.py`, `ci_augment.py`, `ci_contract.py`: the collective-intelligence
  turn snapshot (a git object id for the tree a turn saw).
- `store.py`'s `snapshot_ref` and `snapshot_untracked_excluded`: columns of the turn
  metrics table, written from `ci_snapshot`, with `models.py`'s fields. No migration.
- `cli/ui/app.py`'s `FleetSnapshot`, `remote_server.py`'s `Snapshot` callable type, and
  "snapshot" in UI comments: the fleet frame.
- `docs/platform.md`, `docs/ci-contract.md`, `docs/ci-integration-handoff.md`,
  `docs/demo.tape`, `docs/demo/render.sh`.

## How to verify

- A case-insensitive `git grep` for the packer's name (see History) finds the CHANGELOG
  (the removal entry, the released entries as history, and 0.9.0's line that session
  start no longer points agents at the pack), this file's
  History section, and the two historical plans.
- `git grep -n 'core.snapshot\|snapshot_core\|SnapshotSettings' -- src tests` finds
  nothing.
- `make check`, with `AISQUARE_SUITE_CAPTAIN=on` and `off`, in a throwaway `HOME`,
  `AISQUARE_HOME` and `TMUX_TMPDIR`.
- `shellcheck --shell=sh`, `shfmt -i 4 -ci -d`, `dash -n` and `busybox ash -n` over
  `install.sh` and the cells; `tests/test_install_script_*.py`.
- `install.yml`'s `release-old-node` job: the latest release from PyPI onto Debian 12's
  Node 18, `--yes`, no extra, exits 0.
- `aisquare doctor` with no Node on PATH, in a throwaway home: nothing amber names Node.
- A v16 store with only a pack as evidence, opened by this build in a throwaway
  `AISQUARE_HOME`: the project is listed, and its `snapshot/` is gone.

## Follow-ups

- Drop `--with tiktoken` from `install.sh` (its comment above `install_cli` says why it
  stays), the cells' run 4, Remote's fallback hint and `docs/install.md`'s by-hand line,
  and `RELEASE_ERA_AMBER` with it, once a release without those doctor rows is the
  latest on PyPI: 0.9.0, the release that ships this script, so it can go in the first
  change after 0.9.0 is on PyPI.
- The decks still say session start hands over the project's prompt history; #346 removed
  that line too. Left for the owner, with the generator's templates.
- `install.sh`'s short-circuit does not look at the project: a current machine set up with
  `--no-project`, run again from a repository, says "Nothing to do" and registers nothing.
  That predates #399 (the old `snapshot` row was green with no project since #346).

## History

What the handoff (#57, `4b9ae1fd`) found, checked against `fix/followups` at `189dffd8`.
Line numbers are from there.

- The packer was Repomix, shelled out to as `repomix` or `npx --yes repomix`
  (`core/snapshot.py`, 682 lines: `_repomix_base`, `_run_repomix`,
  `RepomixUnavailableError`, `MIN_NODE`, `installed_repomix_floor`, `pack_node_floor`,
  `node_version`, `can_pack`, `_tiktoken_count`, `generate`, `HOME_SKIPPED`,
  `is_the_homes`). Its files were `pack.repomix.xml`, `skeleton.repomix.xml`, `index.json`
  and `snapshot.json`. `[snapshot] ignore` took Repomix glob patterns and extended a
  built-in list; a `.repomixignore` at the repo root applied on top.
- `core/snapshot.py:3` said it "mirrors AISquare's server-side Repomix packing so
  artifacts stay consistent for future sync", and `SnapshotSettings`' default budget
  (150 000) "mirrors the server's `REPO_PACK_MAX_TOKENS`". That is what question 1 asked
  about.
- Doctor's rows were registered at `services/diagnostics.py:147` (`_check_repomix`),
  `:148` (`_check_tiktoken`) and `:154` (`_check_snapshot`); `_NODE_FLOOR` was at `:681`.
- `install.sh`'s Node step existed only for Repomix: `MIN_NODE_MAJOR` was "THE SAME NUMBER
  AS `core/snapshot.py`'s MIN_NODE" (`:75`), the plan line said "install Node 22+
  (Repomix snapshots)" (`:901`), and `_ensure_npx` (`:1501`) installed npm because
  "Repomix is reached through `npx`". `install.ps1` had no Node or Repomix step.
- `git grep -il repomix` found 35 files. The pitch deck named Repomix once, in an embedded
  doctor capture (`:709`), and packed snapshots at `:745`, `:1965` and `:2023`.
- The handoff proposed deleting the Node step outright and asked question 3; the owner
  kept it as an opt-in for browser tools.
