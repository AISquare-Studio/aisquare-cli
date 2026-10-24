# Removing Repomix

> **Status: a handoff, not code.** Build it after #274 merges into main, on a branch from
> main. Every claim about the code below was checked against `fix/followups` (#274's
> head) at `189dffd8`, and every line number is from there. Re-check them on main before
> you start: #274 is large, and main moves.

## Why

aisquare packs a project's codebase with [Repomix](https://github.com/yamadashy/repomix)
into `~/.aisquare/projects/<id>/snapshot/`: a full pack, a compressed "skeleton", a
per-file index and a metadata file. Session start used to point every agent at that pack
("read the skeleton FIRST"). It was often about 110k tokens, on some repos the skeleton
and full-pack lines named the same file, and no measurement showed agents used it well.

#346 (commit `e89958d2`, folded into #274) already did most of the work:

- session start no longer points agents at the pack;
- `aisquare init` no longer packs (it cost about a minute on a large repo);
- doctor's snapshot row reads as opt-in, and `aisquare project onboard` packs on demand.

#274's follow-up commits `d7f9bdb4` and `6a4c45fe` (the owner's decision D5) made sure the
aisquare home, added as a project, is never packed, and that an old pack of it is deleted.

What is left only costs and goes stale: `project onboard` still packs on its first run and
with `--refresh`, and nothing refreshes the pack otherwise. Doctor has Repomix, tiktoken
and snapshot rows. `install.sh` offers to install Node 22+ "for Repomix snapshots". Old
packs sit on disk. `core/snapshot.py` (682 lines) and the `[snapshot]` config remain. The
owner wants Repomix gone (2026-10-24).

## The outcome

No Repomix anywhere: no pack is made, read or reported, and no Node is installed for it.
`aisquare project onboard` still does its other job, seeding project facts from ecosystem
markers. Existing packs are deleted, safely. Old configs keep loading. Doctor is all green
on a machine with no Node.

## Nothing else in aisquare needs Node

Checked before saying `install.sh`'s Node step can go:

- **Claude Code** is installed by its native installer (`CLAUDE_INSTALLER_URL`,
  `https://claude.ai/install.sh`, `install.sh:57`), which needs no Node. `first_run`
  also lists `npm install -g @anthropic-ai/claude-code` as the LAST route
  (`services/first_run.py:97`, `core/claude_accounts.py:95`), for a user who wants it.
  That route brings its own Node requirement, and is the user's choice.
- **The Claude Code plugin** (`plugins/claude-code/scripts/aisquare-hook`) runs
  `aisquare hook <event>`, or `uvx` with the pinned release. No Node.
- **Browser tooling for the ui-tester** (`diagnostics.py:2644` advises
  `claude mcp add -s user chrome-devtools npx chrome-devtools-mcp`; Playwright MCP is the
  same) needs Node, but the user installs it, and Claude in Chrome is the alternative
  that doctor already names. aisquare never runs it.
- **gbrain** (`diagnostics.py:2754`) is the AISquare gbrain CLI, not an npm package.
- **`install.sh`'s Node step exists only for Repomix.** It says so itself: `MIN_NODE_MAJOR`
  is "THE SAME NUMBER AS `core/snapshot.py`'s MIN_NODE" (`install.sh:75`), the plan line is
  "install Node 22+ (Repomix snapshots)" (`:901`), and `_ensure_npx` (`:1501`) installs npm
  because "Repomix is reached through `npx`".

So the Node step can go. Doctor's browser-tools advice may keep naming `npx`: it is advice
for a tool the user chooses.

## The footprint at `189dffd8`, file by file

`git grep -il repomix` finds 35 files. "snapshot" alone also names other things that must
stay (see *Not this snapshot* below).

### Source

| File | What is there | Do |
|---|---|---|
| `src/aisquare/core/snapshot.py` | The whole packer: the Repomix shell-out (`_repomix_base`, `_run_repomix`, `npx --yes repomix`), `RepomixUnavailableError`, `MIN_NODE`/`MIN_NODE_TEXT`, `NEEDS_NODE`, `NO_PACKER`/`NO_PACKER_DETAIL`, `installed_repomix_floor`, `pack_node_floor`, `node_version`, `can_pack`, the token counting (`_tiktoken_count`), `generate`, the detail sentences, the paths (`snapshot_dir`, `pack_path`, `skeleton_path`, `index_path`, `meta_path`), `HOME_SKIPPED`, `is_the_homes`, `exists`, `load`, `head_sha` | Delete the module. Move the few things others still need (see the store trap and the cleanup below). |
| `src/aisquare/core/config.py:635` | `SnapshotSettings` (`max_tokens`, `ignore`, `.repomixignore`), and `AppConfig.snapshot` at `:778` | Delete both. Unknown keys are already ignored (module docstring, `:9`), so an old `[snapshot]` section keeps loading. |
| `src/aisquare/services/settings.py:211` | A comment naming repomix's `--ignore` as the shape of a list value | Reword: `_split_list` stays for other list keys. `config get/set snapshot.*` then raise `KeyError`, an unknown key, which is right. |
| `src/aisquare/models.py:856` | `Snapshot` (with `pack_path`, `skeleton_path`, `index_path`) | Delete. |
| `src/aisquare/models.py:879` | `OnboardReport.snapshot` and `.snapshot_note` (`:883`, `:884`) | Delete both fields. |
| `src/aisquare/services/project.py:1012` | `onboard(path, refresh=, pack=)`: seeds facts from `_ECOSYSTEM_MARKERS` (`:45`), then packs via `_ensure_snapshot` (`:1060`); `snapshot_settings` (`:1052`) | Keep `onboard` for the facts only; drop `pack=`, `_ensure_snapshot` and `snapshot_settings`. Afterwards: first run seeds one fact per marker; `--refresh` re-scans markers and adds any new fact (never duplicates); nothing is packed. |
| `src/aisquare/services/project.py:942` | `_remove_data_dir`: purge deletes `~/.aisquare/projects/<id>/` ("snapshot, brain") | Keep; fix the docstring (brain only). |
| `src/aisquare/cli/project.py:214` | `project onboard`'s help: "Pack the codebase into a snapshot and seed its context pool"; `_PURGE_HELP` at `:247` lists "snapshot" | Reword both. |
| `src/aisquare/cli/common.py:19`, `:693` to `:722` | `emit_onboard`'s snapshot lines (`✓ snapshot: N files, T tokens`, skeleton-only, too large, skipped) | Delete the snapshot half; keep the seeded-facts output. |
| `src/aisquare/services/lifecycle.py:108` | `SNAPSHOT_OPT_IN`, and `init`'s note at `:199` to `:206` ("Snapshot: not packed …") | Delete the constant and the note. Keep `init` deleting a pack (see cleanup). |
| `src/aisquare/services/diagnostics.py` | Rows registered at `:147` (`_check_repomix`), `:148` (`_check_tiktoken`) and `:154` (`_check_snapshot`); `_NODE_FLOOR` at `:681`; the functions at `:695`, `:771` and `:2685` | Delete all three rows, `_NODE_FLOOR` and the Node-floor advice. tiktoken is used for pack token counts only (`core/snapshot.py:516`), so its row goes too. |
| `src/aisquare/services/onboarding.py:21` | A docstring example, `npm install -g repomix` | Pick another example. |
| `src/aisquare/core/spawn.py:76` to `:85`, `:291` to `:297` | The model-call seam registry lists `core/snapshot.py::head_sha`, `::node_version` and `::_run_repomix` as excluded seams | Remove the three entries and their prose. The registry's own guard test then checks there is no stale entry. |
| `src/aisquare/core/paths.py:495` | `project_data_dir`: "codebase snapshots, future sync artifacts" | Reword the docstring. |
| `src/aisquare/core/store.py:541` to `:598` | **The trap**: below. | Keep the evidence check, Repomix-free. |

### The trap: `store.py:575` to `:584`, a snapshot on disk used as evidence

`_adopt_onboarded_projects` (`store.py:541`) is the v16 → v17 backfill (#139). When the
`project.onboarded_at` column is added, it decides which existing registrations were
used on purpose, and so are listed, rather than merely captured from a prompt (hidden).
The evidence is what a row already carries: a codename, linked repos, context entries,
board rows, a fleet agent, **and a codebase snapshot on disk** (`:575`,
`_snapshot_on_disk` at `:583`, which calls `snapshot_core.exists`, so it checks for
`projects/<id>/snapshot/snapshot.json`). Before #346, `project onboard` and `init` always
packed, so a pack was the mark of a project the owner added.

It runs exactly once per store: in the transaction that adds the column, either by the
v17 step or by `_converge_by_presence` for a store another line stamped past 17 without
it (`_BACKFILLS`, `store.py:723`). After that, it never runs again.

**What goes wrong if it is removed carelessly:**

1. Deleting `core/snapshot.py` breaks the lazy import in `_snapshot_on_disk`, and the
   store refuses to migrate an old store.
2. Dropping the snapshot clause, or deleting packs before an old store is migrated,
   loses the evidence. A pre-v17 store (aisquare 0.6.x and older) upgraded straight to the
   new build would leave every project whose only mark was its pack captured and hidden.
   The owner sees projects vanish from `project list` and the fleet.

**Keep it right with no packer:**

- Keep `_snapshot_on_disk` in `store.py`, self-contained: a plain path check,
  `(paths.project_data_dir(project_id) / "snapshot" / "snapshot.json").exists()`, inside
  the same `try/except OSError` (review of #203: an unreadable directory is no evidence,
  and must not escape the migration). Do not import any snapshot module. The aisquare
  home's row needs no special case here: the backfill's other clauses do not exclude it
  either, and D5 is about packing, not listing.
- Order the cleanup after the migration: delete old packs only once the new build has
  opened the store, which migrates it and runs the backfill first. Never from `install.sh`,
  never before `store_session()` has opened the store.
- Pin it with a test: a v16 store whose only evidence for a project is
  `snapshot/snapshot.json` on disk, opened by the new build, lists that project, and the
  cleanup then removes the folder.

### The aisquare home (D5)

`HOME_SKIPPED` and `is_the_homes` (`core/snapshot.py:246`, `:254`) exist so the home is
never packed and a pack of it is deleted (`project.py:1069`, `diagnostics.py:2698`). With no
packing at all, the rule is moot. The cleanup below deletes the home's old pack with every
other. Remove `tests/test_the_home_is_never_packed.py`, or reduce it to one test that a
leftover pack of the home is deleted by the cleanup. `HOME_WARNING` (adding the home as a
project warns) is a different rule and stays.

### install.sh and install.ps1

- `install.sh`: remove `FNM_INSTALLER_URL`, `NODESOURCE_DEB_URL`, `NODESOURCE_RPM_URL`
  (`:58` to `:60`), `MIN_NODE_MAJOR` and its comment (`:75` to `:82`), `NODE_VERSION` and
  `NODE_ACTION` (`:122`, `:126`), `version_of_node` and the survey line (`:599`, `:610`,
  `:618`), the resolve block (`:743` to `:749`), the Node part of the "current" test
  (`:796`), the summary and plan lines (`:826`, `:901`, `:903`), the `repomix` case in
  `:854`, the rules comment from `:1402`, and `install_node` (`:1425`), `_reread_node`
  (`:1491`), `_ensure_npx` (`:1501`) and `_nodesource_setup` (`:1513`). The Homebrew prompt names "tmux/gh/git/Node" (`:1140` to `:1169`): drop Node
  from it. Keep the warning against `sudo npm install -g`, or drop it with the npm code.
- Exit code 2 currently covers "a Node this run was asked for and could not install"
  (`b4d695b5`, `ecafdefe`): re-read what exit 2 means once Node is gone.
- `install.ps1` has no Node or Repomix step (grep finds none); nothing to change.

### Tests

| File | Do |
|---|---|
| `tests/test_snapshot.py` | Delete. |
| `tests/test_repomix_check_gates_on_node.py`, `tests/test_snapshot_optional_without_node.py` | Delete. |
| `tests/test_the_home_is_never_packed.py` | Delete, or keep one test for the cleanup (above). |
| `tests/test_onboarding.py`, `tests/test_project.py` | Keep the fact-seeding tests; remove pack expectations; add "onboard never runs repomix or npx". |
| `tests/test_status.py`, `tests/test_config.py` | Remove the snapshot parts; add "a config with `[snapshot]` still loads". |
| `tests/test_hooks.py:131`, `:145` | Today they pin that session start never runs `_run_repomix` and never names `pack.repomix.xml`. Keep the second as a negative pin; drop the first with the module. |
| `tests/test_team_block_needs_a_team.py` | Its solo-chars measurement mentions the pack line; keep it as a negative pin. |
| `tests/test_doctor_advice_names_this_package.py:100`, `:424` | Remove the repomix advice cases. |
| `tests/test_ui_onboard.py:75` to `:84`, `tests/test_ui_shell.py` | Remove the `snapshot` and `repomix` doctor rows from the fixtures. |
| `tests/test_install_script_is_posix.py:491` | `test_the_node_floor_matches_the_pythons` goes. |
| `tests/test_install_script_functions.py:458` to `:488`, and the Node stubs at `:214`, `:1266`, `:1359` | The Node decision tests go; keep the version-parsing helper tests that other tools use. |
| `tests/install/cell.sh:157` to `:163`, `:176`, `:201`; `tests/install/matrix.sh:15` to `:18` | Stop asserting Node after run 1; drop `node` from the tool loop; the matrix comments about Debian 12's Node 18 go. |
| `tests/conftest.py:941` | The autouse `no_repomix` fixture (it stubs `snapshot._run_repomix` so no test shells out): delete it, and the mention at `:930`. |

### Docs

- `docs/memory.md` (12 hits): the snapshot section goes.
- `docs/reference.md:34`: the `projects/` line says "snapshot/ (Repomix pack), brain/ (gbrain)". Keep brain only.
- `README.md:27` and `:118`: "adds an on-demand codebase snapshot" and "Node 22+, optional, for a codebase snapshot". Remove both.
- `SECURITY.md:104`: the `npx --yes repomix` supply-chain note goes.
- `docs/plans/one-line-install.md` (26 hits) and `docs/plans/fleet-tui.md` (5): historical plans. Add a one-line status note at the top ("Repomix and the Node step were removed in #…") rather than rewriting them.
- `docs/captain.md:61` ("never packed into a codebase snapshot"), `docs/fleet.md:1296` and
  `:1368` (worktrees share the snapshot): reword or drop the snapshot mention.
- `docs/deck/aisquare-pitch-deck.html`: the owner's deck. It names Repomix once (an
  embedded terminal capture, `:709`) and "packed snapshots" or "snapshots" at `:745`,
  `:1965` and `:2023`. Ask before editing (see the questions).
- `CHANGELOG.md`: one bullet for the removal. Leave past entries as they are.

### Not this snapshot: keep

- `services/ci_snapshot.py`, `ci_augment.py`, `ci_contract.py`: the collective-intelligence
  turn snapshot (a git object id for the tree a turn saw). Unrelated to Repomix.
- `store.py:341` and `:342`, `snapshot_ref` and `snapshot_untracked_excluded`: columns
  of the turn-metrics table, written from `ci_snapshot` (`ci_augment.py:173`, `:418`),
  with `models.py:593`. **Not Repomix columns: keep them.** No migration is needed for
  this removal.
- `cli/ui/app.py`'s `FleetSnapshot`, `remote_server.py`'s `Snapshot` callable type, and
  "snapshot" in UI comments: the fleet frame, unrelated.
- `tests/test_ci_snapshot.py`: the CI snapshot's tests.
- `docs/platform.md:44` (a snapshot of a dirty working tree per prompt), `docs/ci-contract.md`
  and `docs/ci-integration-handoff.md`: the CI snapshot. `docs/demo.tape` and
  `docs/demo/render.sh`: vhs screen snapshots.

## Upgrade and cleanup

- **Delete old packs once, after the store is migrated.** On the first open of the store
  by the new build, after migrations (see the trap), delete each
  `~/.aisquare/projects/<id>/snapshot/` folder. Only that folder name, only directly
  under `paths.project_data_dir(<id>)` for each `<id>` directory under
  `~/.aisquare/projects/`, never following a symlink out of the home (check with
  `Path.resolve()` that the target is still inside `aisquare_home() / "projects"`), and
  never the sibling `brain/`. Its files are `pack.repomix.xml`, `skeleton.repomix.xml`,
  `index.json` and `snapshot.json`. Best effort: an error is logged and costs nothing.
  Record that it ran (a `team_meta` key, or a marker file in the home) so it runs once.
  `init` can keep deleting the current project's folder too, as it does for the home today.
- **Old configs.** A `config.toml` with `[snapshot]` keeps loading, because unknown keys
  are ignored. Optionally, `doctor` or the next config save says once that `[snapshot]`
  is no longer used; the next save drops it (`_dumped` writes the model's fields).
- **Doctor.** The `repomix`, `tiktoken` and `snapshot` rows go. No amber row may be left
  behind: check `aisquare doctor` on a machine with no Node and no tiktoken is all green.
- **The store.** Nothing to migrate (the `snapshot_*` columns are the CI snapshot's).
- **`pipx inject` / `uv tool install --with tiktoken`**: users who added tiktoken for
  exact counts keep a harmless extra package. Say so in the CHANGELOG.

## Does anything outside the CLI rely on the pack?

Inside the CLI, nothing reads a pack except `core/snapshot.py` itself, `cli/common.py`,
`services/project.py`, `services/diagnostics.py` and the store's evidence check. Nothing
uploads one: no API client, CI module or remote endpoint names `pack_path`,
`skeleton_path` or `index_path`.

But `core/snapshot.py:3` says it "mirrors AISquare's server-side Repomix packing so
artifacts stay consistent for" a future sync, and `SnapshotSettings`' default budget
"mirrors the server's `REPO_PACK_MAX_TOKENS`". So the platform packs repositories on its
own side, and a future sync was planned. Whether anything on the server expects
CLI-made packs is not visible from this repo: an owner question.

## PR plan

One PR is enough; the parts are small once the trap is handled. In order, as commits:

1. **Store first.** Make `_snapshot_on_disk` self-contained, and add the v16 evidence test.
2. **Remove the packer.** `core/snapshot.py`, `SnapshotSettings`, `Snapshot`, the
   `OnboardReport` fields, `onboard`'s pack half, `emit_onboard`'s lines, `init`'s note,
   the spawn seams, and their tests.
3. **Doctor.** The three rows and the Node floor, and their tests.
4. **Cleanup.** The one-time deletion after migration, with its tests: it deletes
   `snapshot/` folders only, never `brain/`, never outside the home, and runs once.
5. **install.sh.** The Node step and its tests; the container cells.
6. **Docs and CHANGELOG.**

Acceptance:

- `git grep -in 'repomix\|npx\|MIN_NODE\|tiktoken' -- src tests install.sh install.ps1`
  finds nothing, except `npx` in doctor's browser-tools advice and its tests.
- `git grep -n 'core.snapshot\|snapshot_core\|SnapshotSettings'` finds nothing.
- The suite passes with the captain on and off (`AISQUARE_SUITE_CAPTAIN=on|off`), and
  ruff and mypy are clean.
- The install matrix (`tests/install/matrix.sh`) passes on every cell, with no Node
  installed.
- `aisquare doctor` is green on a machine with no Node and no tiktoken.
- A v16 store whose only evidence is a pack on disk still lists that project after the
  upgrade, and its pack is then deleted.
- `aisquare project onboard` and `--refresh` seed facts and never run a subprocess.

## Questions for the owner

1. Does anything on the AISquare platform expect packs made by the CLI, or was "a future
   sync" (`core/snapshot.py:3`) only a plan? If something does, this removal needs a
   server-side note.
2. The pitch deck (`docs/deck/aisquare-pitch-deck.html`) mentions Repomix and packed
   snapshots in four places. Edit it in the same PR, or leave it to you?
3. Should `install.sh` keep an opt-in Node install for browser MCP servers
   (`npx chrome-devtools-mcp`), or leave Node entirely to the user?
