# Spawn dialog + personas — `＋ spawn agent` becomes real, and an agent is spawned *as someone*

> **Status: planned 2026-09-15** for the hackathon train `rc/hackathon-v1`, cut
> from `release/2026-09-12` @ `5ec92b5` (0.6.0 plus the pane fixes). Verified
> against that tree with Textual 8.2.8, tmux 3.2a, Claude Code 2.1.272, Python
> 3.12. Paths marked **(new)** do not exist yet. Commands below are *planned*, so
> they appear as inline code or in `text`-tagged fences — never a shell fence —
> because `tests/test_documented_commands.py` validates every shell-fenced
> `aisquare …` line against the live command tree. Keep it that way until the
> commands exist, then move the user-facing ones to `docs/personas.md` and
> `docs/fleet.md` as real `sh` fences.
>
> This is a living document: update it in the same PR as the code it describes
> and keep the *Decisions log* (§10) current. Owner: Anmol. Manager session
> `8e92f6af` (fleet `solar-sparrow`).

---

## 0. The ask, restated as acceptance criteria

From the owner's brief ("fix our spawn dialog — spawn a teammate with a
persona; a persona is the personality prompt, configurable per config, easy to
share, with its own way of plugging in and updating"). Each line is something
the finished feature must do.

1. In `asq`, the `＋ spawn agent` row under a project opens a **Spawn dialog**
   instead of today's toast ("the spawn dialog is not built yet" —
   `src/aisquare/cli/ui/app.py`, `on_spawn_agent`). This is Phase 7 of
   `docs/plans/fleet-tui.md` §9, specified in its §4.1 and §5.7. Fields: role,
   label (prefilled, 🎲), task, **persona**, worktree, permission mode, account,
   binary, extra agent args, first prompt. *Spawn* / *Cancel*. A refusal from the
   service stays in the dialog with its reason; success toasts the receipt and
   its notes and opens the new agent's live pane.
2. A **persona is one file**: `<name>.md` — a small front matter plus the
   personality prompt as the body. One file is the whole unit of sharing: copy
   it, gist it, commit it.
3. Personas live in **three layers**: bundled (ship with the CLI), user
   (`$AISQUARE_HOME/personas/`), project (`<repo>/.aisquare/personas/`, so a team
   shares them through git). Project beats user beats bundled.
4. `aisquare persona list|show|add|new|edit|rm|validate|export` manage them, and
   every reporting verb honours `--json`.
5. `aisquare fleet spawn <role> --persona <name>` and `aisquare launch <role>
   --persona <name>` spawn with one; `[fleet.roles.<role>].persona` in
   `config.toml` gives a role a default. Precedence: per-spawn flag > config >
   none (fleet-tui §3.10).
6. The persona reaches the agent **once, at session start**, as its own fenced,
   labelled block inside the `<aisquare-team>` briefing, *after* the role's
   standing cycle and lane rule, followed by a fixed sentence that says it never
   overrides the role cycle, the lane rule, a task's contract or evidence. With
   no persona the injected bytes are **identical to today's**.
7. The board row, `aisquare fleet ls` and the sidebar all say which persona an
   agent runs.
8. `make check` green at every PR; every existing guard (§8) stays green.

**Non-goals for v1, stated so nobody re-derives them:**

- Re-injecting the persona on every prompt. Measured on the removed feature
  (§3.1): ~175 tokens per turn. Session start only; `/clear` re-briefs anyway.
- Model, effort, binary, permission mode or tools *inside* a persona. One home
  per concept — the rule that deleted `bins` in #56 and keeps model/effort out
  of the Settings tab (`src/aisquare/cli/ui/views/settings.py`). A persona is a
  prompt.
- A narration panel, "voice" packs or response *styles* — the persona surface
  removed in #136 on 2026-09-14 (§3.1).
- Persona inheritance (`extends`), per-persona versioning fields, install from a
  URL. `curl … | aisquare persona add -` covers sharing by link today; a URL
  form is a small follow-up once the stdin form exists.
- A browser-verified UI task. This is a terminal UI; the tester verifies it
  headless with Textual's pilot, exactly as `tests/test_ui_project.py` does.

---

## 1. What already exists, and what this reuses

| Need | Already there |
| --- | --- |
| A place the agent reads standing instructions | the `SessionStart` hook → `services.team.hook_session_start` → `_render_board` → `harness.role_cycle` (the role's cycle and lane rule). One channel. |
| A way to tell the agent process *who it is* | `AISQUARE_ROLE`, exported by `src/aisquare/cli/launch.py`, read by `core.orchestrator.env_role`. The persona takes the same shape: `AISQUARE_PERSONA`. |
| The spawn itself | `services.fleet.spawn(project, role, *, label, task_id, worktree, permission_mode, binary, prompt, agent_args, spawned_by, account)` — every dialog field but persona exists today. |
| Running a spawn off the UI thread | `views/project.py` `_start_manager` → `run_worker(thread=True, exit_on_error=False)` → `on_worker_state_changed`. Copy the pattern. |
| A modal | `ModalScreen` is already used twice in `cli/ui/app.py` (`HelpScreen`, `ThemePicker`). |
| Permission-mode choices | `views/settings.py` `PERMISSION_MODES` and `permission_options`. |
| Label rules and 🎲 | `services.fleet.LABEL`, `next_label`; `core/codenames` word lists (fleet-tui §5.7 names the 🎲 form `<role>-<adjective>-<animal>`). |
| The per-role config rung | `core.config.FleetRoleSettings` (`permission_mode`, `worktree`, `extra_args`) — gains `persona`. |
| Editing a text file in `$EDITOR` | `core.editor.edit_text` (already a registered spawn seam). |
| Stripping control characters from text we inject | `core.injection.sanitise_text`. |
| Store migrations | `core/store.py` `_MIGRATIONS` (v14 on this base); `docs/store-migration-race.md` for the rules. |
| A project-level dotdir | `.aisquare` is already a root marker (`core/workspace.py` `_ROOT_MARKERS`), so `<repo>/.aisquare/personas/` needs no new convention. |

---

## 2. Architecture in one picture

```text
  <name>.md  (bundled | $AISQUARE_HOME/personas | <repo>/.aisquare/personas)
      │ parse → Persona            core/personas.py (new)
      ▼
  catalogue / resolve(name)  ◀──── aisquare persona list|show|…   (cli/persona.py, new)
      │
      ├──▶ aisquare fleet spawn <role> --persona NAME ──▶ aisquare launch <role> --persona NAME
      │        (services/fleet.py: validate, record        (cli/launch.py: validate, then
      │         fleet_agent.persona, receipt line)           AISQUARE_PERSONA=NAME in the env)
      │                                                              │
      │                                                              ▼  the agent process
      │                                              SessionStart hook → hook_session_start
      │                                                 records team_session.persona
      │                                                 renders <aisquare-team> … role cycle …
      └─────────────────────────── briefing(persona) ──▶   <aisquare-persona name= layer=> … </aisquare-persona>
                                                           + the fixed guard sentence
  board row · fleet ls · sidebar badge  ◀── team_session.persona / fleet_agent.persona
  Spawn dialog (cli/ui/spawn.py, new) ──▶ fleet_service.spawn(…, persona=…)
```

Nothing new touches the model API, spawns a process, or reaches the network.

---

## 3. Design decisions, with the alternatives rejected

### 3.1 This is not the persona that was removed

PR #136 (`codex/native-personas-workflow`) built a persona layer in three cuts
and then deleted it on 2026-09-14 (`24b26a0`, "remove the persona feature
entirely … per the product decision"). What it was: a **narration panel** that
re-voiced board records in a chosen "cast" (Studio, Mission Control), then an
opt-in **voice** appended to the system prompt, then per-turn **response
styles** (answer-first, teacher, …) injected by the user-prompt-submit hook. Its
own removal commit calls it "a decorative surface on top of the team".

This plan is a different thing, and the difference is the whole point: a persona
here is a **spawn-time operating prompt** chosen by whoever spawns the agent —
"this coder is a skeptic", "this reviewer is a mentor" — delivered once, recorded
on the board, shareable as a file. It changes how an agent *works*, not how a
panel narrates. Three lessons from #136 are kept on purpose:

- **One delivery channel.** `4cdab46` removed the `--append-system-prompt` path
  "so the two channels cannot fight". We have exactly one (§3.2).
- **Never per turn.** ~175 tokens a turn was measured and is why styles were off
  by default. Session start only.
- **Isolation is a test, not a promise.** Every factual surface — the board,
  `task`, `log`, `--json` — is byte-identical with and without a persona, and a
  test pins it (§7, `tests/test_persona_briefing.py`).

### 3.2 Delivery: the session-start briefing, not `--append-system-prompt`

The persona is rendered into the same `<aisquare-team>` block that already
carries the role's standing cycle — right after the lane rule, before the
closing tag. Reasons, in order of weight:

1. **One place the agent's standing instructions live.** The role cycle governs
   behaviour from there today (it is what makes a manager route instead of
   code); the persona belongs beside it, not in a second channel that can
   disagree with it.
2. **Agent-agnostic.** It works for `--bin claude2`, a wrapper script, and a
   hand-typed `AISQUARE_PERSONA=skeptic claude`. An argv flag is Claude Code's,
   and `harness.role_defaults` would have to *withhold* it for any other binary
   — a persona silently not applied is the worst outcome this feature can have.
3. **Recordable.** The hook is where the session row is written; the persona is
   recorded and rendered in one place, at one moment.
4. **Nothing on argv.** A persona is up to 4,000 characters; argv is visible in
   `ps` and in every receipt.

Rejected: `--append-system-prompt-file`. Stronger placement in the system prompt
proper, but Claude-only, a second channel, invisible to the hook, and frozen in a
way that does not survive `/clear`. If adherence ever measures short, the
hook's block can be promoted for the default binary as a follow-up; the file
format and the board do not change.

### 3.3 Format: Markdown with a flat YAML front matter — no new dependency

A persona is mostly prose with a four-line header, which is exactly the shape
Claude Code's own `.claude/agents/*.md` files have, and the shape people
already share. The header is the **flat YAML subset** those files use — `key:
value` where a value is a string (bare or quoted), an integer, a boolean, or an
inline list `[a, b]` — parsed by a ~60-line standard-library parser. Nested
maps, block scalars, anchors and multi-document markers are **errors** that
name the line and the rule ("front matter is flat: `key: value`; lists inline").

Rejected: **PyYAML** — a runtime dependency to read four keys; the login work
kept to standard-library HTTP for the same reason. **TOML** — in the standard
library, but a multi-line prose body inside `"""…"""` is not what anyone will
paste into a gist. **JSON** — what #136 used; correct and unreadable to edit.

Bonus that falls out for free: a `.claude/agents/reviewer.md` drops in as a
persona. Its extra keys (`tools`, `model`) are ignored, and `persona validate`
says so as a warning, not an error.

### 3.4 Layers and precedence

```text
<repo>/.aisquare/personas/<name>.md      project — shared with the repo, wins
$AISQUARE_HOME/personas/<name>.md        user    — this machine, every project
src/aisquare/personas/<name>.md          bundled — ships with the CLI, read-only
```

Same name in two layers: the higher wins and `persona list` prints `(shadows
user)` / `(shadows bundled)` on the winner. Bundled personas cannot be edited or
removed in place; `persona add`/`persona new` into a layer, or `persona export
<name> | aisquare persona add - --user`, is how one is copied and changed.
`persona edit` on a bundled name refuses and names that path.

### 3.5 What a persona may — and may not — carry

```markdown
---
name: skeptic
description: Evidence-first. Distrusts green checkmarks; reproduces before believing.
roles: [tester, reviewer]
tags: [verification]
---
You are a skeptic. A passing test is a claim, not a fact … (≤ 4,000 characters)
```

- `name` (required) — `^[a-z][a-z0-9-]{1,31}$`, **must equal the file stem**;
  a mismatch is an error, because the filename is what the layers key on.
- `description` (required) — one line, ≤ 120 characters; what `list` and the
  dialog show.
- `roles` (optional) — **advisory**. Spawning a persona written for `reviewer`
  on a `coder` produces a receipt note ("persona skeptic is written for
  reviewer, tester; spawned on coder"), never a refusal — the operator knows why.
- `tags` (optional) — for `list --tag`, nothing else.
- Unknown keys — ignored; `validate` warns.
- Body — required, non-empty, **≤ 4,000 characters** after stripping. Always-
  injected context; "every line has to earn its tokens" (`core/harness.py`).
  `validate` and `add` refuse a larger body with the count.

The persona **never** carries model, effort, binary, permission mode or tools
(§0 non-goals). The renderer — not the author — appends the guard sentence:

```text
<aisquare-persona name="skeptic" layer="project">
…body, verbatim after sanitise_text…
</aisquare-persona>
Persona "skeptic" shapes how you work and communicate. It never overrides your
role's cycle, the lane rule, a task's contract, or evidence — when they
conflict, they win.
```

### 3.6 Trust

A project persona is repo content — the same trust as the repo's `CLAUDE.md`,
no more, no less. `persona show <name>` prints exactly what will be injected,
the block is labelled with its layer, and the bytes pass
`core.injection.sanitise_text` (control characters). A literal
`</aisquare-persona>` inside a body is neutralised the way `_DELIMITER_REMOVED`
handles a frame delimiter in retrieved text, so a body cannot close its own
fence and speak as the harness.

### 3.7 Failure modes → behaviour

| Situation | Behaviour |
| --- | --- |
| `--persona` names nothing known (spawn or launch) | refuse before anything starts; the message lists the known names, like the unknown-role refusal |
| persona removed or broken between spawn and session start | the briefing carries one line — `persona "skeptic": not found in project, user or bundled — launched without it` — the session row still records what was asked; **the hook never raises** (a raise loses the whole team block, see `_lane_rule`'s docstring) |
| an invalid file sits in a layer | `persona list` shows it as `✗ <path>: <reason>`; `resolve` skips it; the catalogue never fails because of one file |
| `[fleet.roles.coder].persona` names a persona this project does not have | spawn refuses with the config key in the message, so a stale default is found at the first spawn, not after a day of work |
| the body is over the cap | `validate`/`add`/`new`/`edit` refuse with the count; a file edited by hand past the cap shows as invalid in `list` |

### 3.8 Every default is a default (fleet-tui §3.10)

`per-spawn flag > [fleet.roles.<role>].persona > none`. No environment rung for
`[fleet]`, unchanged. `AISQUARE_PERSONA` is what `launch` *exports* and what the
hook *reads* — it is not a config input — so a hand-typed
`AISQUARE_PERSONA=skeptic aisquare launch coder` also works, with no fleet at all.

---

## 4. UI specification — the Spawn dialog

`SpawnDialog(ModalScreen[SpawnReceipt | None])` in `src/aisquare/cli/ui/spawn.py`
**(new)**, pushed by `FleetApp.on_spawn_agent` for the project the row belongs
to. One column of labelled fields, *Spawn* and *Cancel* at the bottom, a status
line above the buttons for refusals.

| Field | Widget | Default | Validation / behaviour |
| --- | --- | --- | --- |
| Role | `Select` | `coder` | `fleet_service.FLEET_ROLES` plus roles bound in `team.profiles`; `manager` greyed with "one per project" when one is live |
| Label | `Input` + 🎲 `Button` | `<role>-<task short id>` when a task is picked, else `<role>-<n>` (§5.7, via `next_label`) | live-checked against `fleet_service.LABEL`; invalid → *Spawn* disabled and the rule shown; 🎲 → `<role>-<adjective>-<animal>` from `core/codenames` |
| Task | `Select` | `(none)` | the project's open tasks (`todo`, `doing`, `review`, `blocked`) as `<short id> [status] <title>`; changing it re-prefills an untouched label |
| Persona *(P4)* | `Select` | the role's `[fleet.roles.<role>].persona`, else `(none)` | `core.personas.catalogue(project.root)`; the description shows under the field; follows the role until touched |
| Worktree | `Switch` | the role's `FleetRoleSettings.worktree` | **disabled with "not a git repository"** when `fleet_service.is_git_project(root)` is false (§5.7) |
| Permission mode | `Select` | the role's `permission_mode` | `views.settings.permission_options(current)` |
| Account | `Select` | `(this shell's)` | slots from `services.claude_accounts` as the Accounts page lists them; loaded in a worker so the dialog opens instantly |
| Binary | `Input` | empty; placeholder shows `harness.resolve_binary(role).binary` | passed as `binary=` only when non-empty |
| Extra agent args | `Input` | empty | `shlex.split` → `agent_args`; a quoting error is shown inline |
| First prompt | `TextArea` | empty | passed as `prompt=`; the service's own multi-line note (§`_type_prompt`) surfaces as a toast |

Behaviour:

- *Spawn* disables itself and runs `fleet_service.spawn(project, role, label=…,
  task_id=…, worktree=…, permission_mode=…, binary=…, prompt=…, agent_args=…,
  account=…, persona=…)` in a thread worker with `exit_on_error=False`. Every
  `None` means "the role's default", exactly as the CLI's flags do — the dialog
  sends a value only for a field the user touched or that has no role default.
- `FleetError` → the message in the status line (`markup=False`: it can carry a
  path), the dialog stays open, *Spawn* re-enables. Any other exception → the
  same status line with the class name; never a crash of the app.
- Success → `dismiss(receipt)`; the app toasts `✓ spawned <label> (<id>) →
  <session> <pane>` plus each `receipt.notes` line as a warning toast (the
  Project view's exact pattern), calls `refresh_data()`, and posts
  `AgentSelected` so the new agent's live pane is what the user sees next.
- `Esc` cancels; `Tab`/`Shift+Tab` move; the buttons are the only submit. No
  key the live `TerminalPane` needs is bound here — the modal owns focus while
  open and the pane behind it keeps rendering.
- Header shows the project (`aisquare-cli · solar-sparrow`) so a spawn from the
  wrong project's row is visible before it happens.

Tests: `tests/test_ui_spawn.py` **(new)**, headless, `fleet_service.spawn`
replaced by a recorder, no tmux (reuse `no_real_tmux` from
`tests/test_ui_project.py`). What is asserted is the artefact: the exact kwargs
the recorder received.

---

## 5. Code layout and the interface contract

The contract below is what lets two coders work in parallel: P2 and P4 code
against these names while P1 builds them.

```python
# src/aisquare/core/personas.py (new) — pure: files in, models out. No store, no tmux.
FORMAT = "persona.v1"
NAME = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
DESCRIPTION_MAX_CHARS = 120
BODY_MAX_CHARS = 4_000
Layer = Literal["project", "user", "bundled"]

class Persona(BaseModel):
    name: str
    description: str
    roles: list[str] = []
    tags: list[str] = []
    body: str
    layer: Layer
    path: Path

class PersonaError(ValueError):
    """str(exc) names the path, the line (when there is one) and the rule."""

def parse(text: str, *, path: Path, layer: Layer) -> Persona
def layer_dirs(root: Path | None) -> list[tuple[Layer, Path]]      # precedence order; project only with a root
def catalogue(root: Path | None = None) -> tuple[list[Persona], list[tuple[Path, str]]]
    # loadable personas, first layer wins per name  ·  (path, reason) for every file that did not load
def resolve(name: str, root: Path | None = None) -> Persona          # PersonaError lists the known names
def briefing(persona: Persona) -> list[str]                          # the lines the hook appends; sanitised; guard last
def render(persona: Persona) -> str                                  # canonical file text (export, new)
```

```python
# src/aisquare/services/personas.py (new) — the file operations behind the CLI
def add(source: Path | None, *, layer: Layer, root: Path | None, name: str | None, force: bool) -> Persona   # None = stdin
def new(name: str, *, layer: Layer, root: Path | None) -> Path                  # scaffold from a template, then edit
def edit(name: str, *, root: Path | None) -> Persona | None                      # $EDITOR; invalid result keeps the old file
def remove(name: str, *, layer: Layer, root: Path | None) -> Path                # bundled: PersonaError
def export(name: str, *, root: Path | None) -> str
def validate(path: Path) -> tuple[Persona, list[str]]                            # persona + warnings (unknown keys)
```

```python
# elsewhere
core.orchestrator.env_persona() -> str | None                   # AISQUARE_PERSONA, like env_role
core.config.FleetRoleSettings.persona: str | None = None        # the config rung
models.TeamSession.persona: str | None = None                   # recorded at session start
models.FleetAgent.persona: str | None = None                    # recorded at spawn
services.fleet.spawn(..., persona: str | None = None)           # None → role config → none
```

Files, by task (§6):

| Task | New | Changed |
| --- | --- | --- |
| **P1** persona core | `src/aisquare/core/personas.py`, `src/aisquare/services/personas.py`, `src/aisquare/cli/persona.py`, `src/aisquare/personas/{skeptic,mentor,minimalist,careful}.md`, `docs/personas.md`, `tests/test_personas.py`, `tests/test_persona_cli.py` | `src/aisquare/cli/app.py` (register the group), `CHANGELOG.md` |
| **P2** persona wiring | `tests/test_persona_briefing.py` | `cli/launch.py`, `core/orchestrator.py`, `services/team.py`, `core/store.py` (v15), `models.py`, `core/config.py`, `services/fleet.py`, `cli/fleet.py`, `docs/fleet.md`, `tests/test_fleet_service.py`, `tests/test_fleet_cli.py`, `tests/test_launch.py`, `tests/test_store.py`, `CHANGELOG.md` |
| **P3** Spawn dialog | `src/aisquare/cli/ui/spawn.py`, `tests/test_ui_spawn.py` | `cli/ui/app.py` (`on_spawn_agent`), `docs/fleet.md` (the dialog paragraph), `CHANGELOG.md` |
| **P4** persona in the UI | — | `cli/ui/spawn.py` (the Select), `cli/ui/views/settings.py` (per-role default), `cli/ui/sidebar.py` (badge), `tests/test_ui_spawn.py`, `tests/test_ui_project.py`, `docs/fleet.md`, `docs/personas.md` |

The bundled set, four operating personalities (bodies ≤ 1,200 characters each,
owner reviews the wording in P1's PR): **skeptic** — evidence first, reproduces
before believing, names what it did not check; **mentor** — explains the why
before the what, surfaces the alternatives it weighed; **minimalist** — the
smallest change that meets the contract, no speculative abstraction, says what
it left out; **careful** — prefers reversible steps, states assumptions before
acting, asks one precise question when blocked rather than guessing.

Config, the whole of it:

```toml
[fleet.roles.coder]
permission_mode = "auto"
worktree = true
persona = "minimalist"        # (new) default for every coder spawn; a flag still wins
```

---

## 6. Delivery — four tasks, two coders, one tester

Board: the **aisquare-cli** board (project `prj_61a0b873…`, codename
`solar-sparrow`). Tasks are titled `[HACK][P<n>][coder] …`; `--needs` carries the
ordering. Both coders start at once.

| # | Task | Coder | Needs | Size |
| --- | --- | --- | --- | --- |
| **P1** | Persona core: `persona.v1` format and parser, three layers, `catalogue`/`resolve`/`briefing`, four bundled starters, the `aisquare persona` group, `docs/personas.md` | `coder-persona-core` | — | M |
| **P3** | The Spawn dialog over today's `spawn()` — every field except persona; `on_spawn_agent` opens it | `coder-spawn-dialog` | — | M |
| **P2** | Persona wiring: `launch --persona` + `AISQUARE_PERSONA`, the hook records and renders once, store v15, `fleet spawn --persona`, `[fleet.roles.<role>].persona`, board / `fleet ls` show it | `coder-persona-core` | P1 | M |
| **P4** | Persona in the UI: the dialog's Select (default from role config), per-role default in Settings, sidebar badge | `coder-spawn-dialog` | P2, P3 | S |

Board ids (2026-09-15): P1 `tsk_01m2hkec4s10veyxy1d2rc2jnm` · P3
`tsk_01m2hkecm1tgegphnqa9kt398s` · P2 `tsk_01m2hked3g7e26f4zs0x5ypyjw` (needs P1) ·
P4 `tsk_01m2hkedk67vpy4gnt3bar1hmm` (needs P2, P3).

Tester (`tester-hackathon`): spawned when the first task reaches review; its
prompt names the worktree and branch to check, because nothing moves a tester
into a coder's tree (`docs/fleet.md`, the roles table). Reviewer once a PR
exists; validator once all four are done.

**Rules for every PR on this train**

- Branch from `rc/hackathon-v1`; **PR base `rc/hackathon-v1`**, never `main`.
  The fleet's coder worktrees branch from the root's HEAD, which is this branch.
- Work in a `.venv` inside the worktree (`python -m venv .venv && make install`)
  and gate with `make check` there. **Never `pip install -e .` into the pyenv
  interpreter** — that is the `aisquare` the fleet itself runs on.
- A CHANGELOG `[Unreleased]` entry per PR, in this file's voice: what, why, what
  was measured.
- Docs fences: a planned or illustrative command is inline code or a `text`
  fence; only a command that exists goes in an `sh` fence.
- The plan is updated in the PR that changes what it describes; the Decisions
  log gets a row.

---

## 7. Acceptance, executable

**P1 — persona core**

- `aisquare persona list` prints the four bundled personas with layer and
  description; `--json` parses and carries `name, description, roles, tags,
  layer, path` per entry, plus an `invalid` list.
- `aisquare persona show skeptic` prints the exact block `briefing()` renders,
  guard sentence last; `--json` carries `body` and `briefing`.
- `printf -- '---\nname: x\ndescription: d\n---\nbody\n' | aisquare persona add -
  --user` creates `$AISQUARE_HOME/personas/x.md`; a second run without
  `--force` refuses (exit 1, `persona_exists`); `--name y` writes `y.md` and
  rewrites the header's `name`.
- `aisquare persona new pair --project` inside a repo creates
  `<repo>/.aisquare/personas/pair.md` from the template and opens `$EDITOR`
  (tests use `EDITOR=true`).
- `aisquare persona edit skeptic` refuses: bundled, names the path and the copy
  command. `edit` on a user persona with an editor that returns an invalid file
  leaves the old file byte-identical and exits 1 with the rule.
- `aisquare persona validate <path>`: exit 0 with warnings for unknown keys;
  exit 1 naming the line for nested YAML, a name/stem mismatch, a missing
  description, a body over 4,000 characters (with the count).
- `aisquare persona rm x --user` removes it; `rm skeptic` refuses (bundled).
- `aisquare persona export skeptic` prints a file that `validate` accepts and
  `add -` re-imports byte-identical.
- Precedence: a project `skeptic.md` wins over bundled and `list` says
  `(shadows bundled)`; an unreadable file in a layer appears under `invalid`
  and does not stop the others from loading.
- A `.claude/agents`-shaped file (`name`, `description`, `tools`, `model`) loads;
  `validate` warns on `tools` and `model`.
- Guards: `test_stubs`, the configured-home sweep, the JSON-stdout sweep and
  `test_import_cost_of_the_integration` are green with the new group.

**P2 — persona wiring**

- `aisquare launch coder --persona skeptic` (exec intercepted as
  `tests/test_launch.py` does) exports `AISQUARE_PERSONA=skeptic`; `--persona
  nope` exits with `unknown_persona` and lists the known names; no `--persona`
  → the variable is absent.
- The `SessionStart` injection for a role **without** a persona is
  byte-identical to the rendering before this change (pin the string in the
  test, from this base).
- With `AISQUARE_PERSONA=skeptic`: exactly one `<aisquare-persona …>` block,
  positioned after the lane rule and before `</aisquare-team>`; the guard
  sentence is the block's last line; the session row's `persona` column reads
  `skeptic`; the sessions list line carries `persona:skeptic`.
- The per-prompt delta (`hook_prompt_heartbeat`) contains no persona text.
- `AISQUARE_PERSONA=missing`: the briefing carries the one "launched without it"
  line, the row records `missing`, and the hook returns normally.
- Store: a v14 database opens and upgrades to v15 with `team_session.persona`
  and `fleet_agent.persona` nullable; `docs/store-migration-race.md`'s rules
  hold (`tests/test_store_migration_race.py` green).
- `fleet spawn coder --persona skeptic` against the fake tmux: the window
  command carries `launch coder … --persona skeptic`, the row records it, the
  receipt line ends `· persona skeptic`; `--persona nope` refuses before any
  window exists; `[fleet.roles.coder].persona = "minimalist"` is used when the
  flag is absent and a flag beats it; a persona whose `roles` exclude the
  spawned role adds a receipt note.
- `aisquare fleet ls` shows `· skeptic` on the row; `--json` has `persona`.
- `docs/fleet.md`: the `fleet spawn` reference gains `--persona`, the config
  block gains the key; `tests/test_fleet_docs_are_true.py` and
  `test_documented_commands.py` green.

**P3 — the Spawn dialog**

- Activating `＋ spawn agent` opens `SpawnDialog` for that project (no toast).
- Label prefills `coder-<n>`; picking a task re-prefills `coder-<short id>`;
  typing `Bad Label!` disables *Spawn* and shows the rule; 🎲 yields
  `coder-<adjective>-<animal>` matching `fleet_service.LABEL`.
- In a non-git project the worktree switch is disabled and says why.
- *Spawn* calls the recorder exactly once with the chosen values and `None` for
  untouched fields; extra agent args are `shlex`-split.
- A recorder raising `FleetError("…")` keeps the dialog open with the message;
  *Spawn* re-enables. A recorder returning a `SpawnReceipt` with two notes
  dismisses the dialog, produces the `✓ spawned …` toast plus two warning
  toasts, refreshes, and selects the new agent.
- `Esc` dismisses with `None` and calls nothing. No test reaches tmux
  (`no_real_tmux` teardown clean).
- `docs/fleet.md`'s UI section describes the dialog in the present tense only
  for what P3 ships (no persona field yet).

**P4 — persona in the UI**

- The dialog's Persona Select lists `(none)` plus the catalogue; choosing
  `coder` preselects `[fleet.roles.coder].persona` when set; the description
  updates under the field; the recorder receives `persona="skeptic"`.
- Settings tab: a Persona Select per role, saved through `save_config` (the one
  writer), re-read after save; an unknown configured name still shows (the
  `(custom)` pattern from `permission_options`).
- Sidebar agent rows show a dim `· skeptic` badge when the row's `FleetAgent`
  (or its session) carries a persona.
- `docs/personas.md` gains the UI paragraph; `docs/fleet.md` the dialog's field.

---

## 8. Guards this work must keep green, and what each means here

| Guard | What it means for this work |
| --- | --- |
| `tests/test_spawn_seams.py` | no new `subprocess`/`exec` call sites. `persona edit` goes through the registered `core/editor.py::edit_text` seam. |
| `tests/test_documented_commands.py` | every `aisquare …` line in a shell fence must exist; this plan uses `text` fences until the commands land. |
| `tests/test_stubs.py` + `tests/cli_tree.py` | every new leaf is enumerated; it is implemented, not stubbed, and reports canonically. |
| `tests/test_no_traceback_in_a_configured_home.py` | every new command runs in a configured home without raising — including `persona edit` with no `$EDITOR` and `persona show` of a name that does not exist. |
| `tests/test_json_stdout_is_machine_readable.py` | under `--json`, stdout is JSON or empty for every new command. |
| `tests/test_import_cost_of_the_integration.py` | no heavy import at module level in `core/personas.py` (it is imported by the hook path). |
| `tests/test_config_unknown_keys.py`, `test_config_durable_replace.py` | `FleetRoleSettings.persona` round-trips; an older build that lacks the field keeps the key (`_keep_unknown`). |
| `tests/test_config_writes_stay_in_the_cli.py` | the Settings tab writes through `save_config` from the UI layer as it does today; persona *files* are not config and are written by `services/personas.py`. |
| `tests/test_store_migration_race.py`, `tests/test_damaged_store_recovery.py` | v15 follows the race rules; a damaged store still degrades quietly. |
| `tests/test_the_roster_covers_every_identity_we_emit.py` | unchanged — a persona is not a role and mints no identity. |
| `tests/test_fleet_docs_are_true.py` | every present-tense claim added to `docs/fleet.md` is mechanically true. |
| `tests/test_ui_shell.py`, `tests/test_terminal_pane.py` | the modal does not intercept keys the pane owns; opening and closing the dialog leaves the shell's bindings as they were. |
| `make check` (ruff format + lint, mypy `--strict`, pytest) | at every PR, in the worktree's `.venv`. |

---

## 9. Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| **PR #136** (`codex/native-personas-workflow`, open, base `main`) rewrites `cli/launch.py`, `services/fleet.py`, `services/team.py`, `core/harness.py` | high | merge conflicts for P2 | P2 keeps its changes additive and local (one new option, one new column pair, one new block in `_render_board`); whoever lands second rebases. The owner tells Surjoyday this plan exists. |
| **PR #169 (#144)** adds `fleet_agent.account_slot` and `launch_spec` as store **v15–v18** on its branch | high | migration-number collision | v15 here is provisional; the second to land renumbers (the v13/v14 precedent recorded in `core/store.py`). When #144 lands, `persona` joins `LaunchSpec` so a restart replays it — a one-line follow-up named in the Decisions log. |
| The tmux server that runs this fleet carries `AISQUARE_TEAM_HUB=…/aisquare-ci` in its **global environment**, so any agent spawned on it reads the aisquare-ci board | certain today | coders claim the wrong board's tasks | operational, not code: `tmux -L asqui set-environment -gu AISQUARE_TEAM_HUB` before the first spawn (a window inherits the server's environment, not the spawner's — `services/fleet.py`'s own comment). Recorded here so the next fleet does not rediscover it. |
| Hook context is weaker than the system prompt; a persona may not hold for a long session | medium | persona fades | acceptable for v1 (the role cycle governs from the same place and demonstrably holds); `/clear` re-briefs; promotion to `--append-system-prompt-file` for the default binary is a contained follow-up (§3.2). |
| Textual modal focus vs a live `TerminalPane` | low | keys lost | the modal owns focus while open; `tests/test_ui_shell.py` pins the shell's bindings before/after. |
| Two coders touch `docs/fleet.md` and `CHANGELOG.md` | certain | trivial conflicts | each PR adds its own paragraph/bullet; rebase on `rc/hackathon-v1` before review. |

---

## 10. Decisions log

| Date | Decision | Who |
| --- | --- | --- |
| 2026-09-15 | Plan authored; `rc/hackathon-v1` cut from `release/2026-09-12` @ `5ec92b5` and pushed. Defaults chosen and open to the owner: Markdown + flat YAML front matter (§3.3); delivery via the session-start briefing (§3.2); layers project > user > bundled (§3.4); body cap 4,000 chars; four bundled starters named in §5; no `UI:` prefix on the dialog tasks because a browser ui-tester cannot verify a Textual dialog — the tester verifies it headless. Deferred: `extends`, per-persona version field, URL install, per-turn reinforcement. | manager `8e92f6af` |
