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
> **Revised the same day** on the owner's redirect: a persona **is a Claude Code
> skill** (`<name>/SKILL.md`), so one directory is interchangeable between
> `aisquare persona` and Claude's `/name`; and `persona import` is *smart* — a
> recognised skill imports as bytes, anything else is turned into one by an
> LLM, using the fleet's own Claude first and an API key second (§3.9).
>
> This is a living document: update it in the same PR as the code it describes
> and keep the *Decisions log* (§10) current. Owner: Anmol. Manager session
> `8e92f6af` (fleet `solar-sparrow`).

---

## 0. The ask, restated as acceptance criteria

From the owner's brief ("fix our spawn dialog — spawn a teammate with a
persona; a persona is the personality prompt, configurable per config, easy to
share, with its own way of plugging in and updating"; then: "keep personas as
the Claude skill md format so it is interchangeable as a skill and a persona;
allow a smart LLM import that takes any existing skill, persona or md and makes
it a skill we use as a persona; recognised formats import directly, anything
else falls back to the LLM; use the manager first, then an API key if
available"). Each line is something the finished feature must do.

1. In `asq`, the `＋ spawn agent` row under a project opens a **Spawn dialog**
   instead of today's toast ("the spawn dialog is not built yet" —
   `src/aisquare/cli/ui/app.py`, `on_spawn_agent`). This is Phase 7 of
   `docs/plans/fleet-tui.md` §9, specified in its §4.1 and §5.7. Fields: role,
   label (prefilled, 🎲), task, **persona**, worktree, permission mode, account,
   binary, extra agent args, first prompt. *Spawn* / *Cancel*. A refusal from the
   service stays in the dialog with its reason; success toasts the receipt and
   its notes and opens the new agent's live pane.
2. **A persona is a Claude Code skill**: a directory `<name>/` holding a
   `SKILL.md` (YAML frontmatter + a Markdown body) and any supporting files.
   The same directory works as `/name` in Claude Code and as `--persona name`
   in aisquare, byte for byte. Nothing aisquare-specific is *required* in it.
3. Personas live in **three layers**: bundled (ship with the CLI), user
   (`$AISQUARE_HOME/personas/`), project (`<repo>/.aisquare/personas/`, so a team
   shares them through git). Project beats user beats bundled.
4. `aisquare persona list|show|new|edit|rm|validate|import|export` manage them,
   and every reporting verb honours `--json`.
5. **Import takes anything.** A skill directory, a `SKILL.md`, an agent file, a
   Cursor rule, a plain `.md`/`.txt`, a JSON or YAML persona from some other
   tool, `-` for stdin, an `https://` URL, or the name of a skill already in
   Claude's skill directories. When the source is a recognised skill (frontmatter
   with a `description`, a non-empty body) it is copied verbatim; otherwise an
   LLM rewrites it into a skill, the result is validated like any other, shown,
   and confirmed before it is saved. Engines, in order: the fleet's own Claude
   Code run headless under the **manager** role's binding, then the Anthropic
   **API** when credentials are available, then a refusal that says how to get
   either.
6. **Export goes back out** as a skill: to stdout, to a directory, or straight
   into Claude's personal or project skills directory so it is `/name` there.
7. `aisquare fleet spawn <role> --persona <name>` and `aisquare launch <role>
   --persona <name>` spawn with one; `[fleet.roles.<role>].persona` in
   `config.toml` gives a role a default. Precedence: per-spawn flag > config >
   none (fleet-tui §3.10).
8. The persona reaches the agent **once, at session start**, as its own fenced,
   labelled block inside the `<aisquare-team>` briefing, *after* the role's
   standing cycle and lane rule, followed by a fixed sentence that says it never
   overrides the role cycle, the lane rule, a task's contract or evidence. With
   no persona the injected bytes are **identical to today's**.
9. The board row, `aisquare fleet ls` and the sidebar all say which persona an
   agent runs.
10. `make check` green at every PR; every existing guard (§8) stays green.

**Non-goals for v1, stated so nobody re-derives them:**

- Re-injecting the persona on every prompt. Measured on the removed feature
  (§3.1): ~175 tokens per turn. Session start only; `/clear` re-briefs anyway.
- Making a persona *act* as a skill inside the spawned agent (materialising it
  into the agent's `.claude/skills/`). That writes into a coder's worktree. Export
  does it on request, at a path the operator names.
- Model, effort, binary, permission mode or tools *decided by* a persona. A
  skill's frontmatter may carry `model`/`allowed-tools` for Claude's use; aisquare
  reads none of them at spawn. One home per concept — the rule that deleted
  `bins` in #56 and keeps model/effort out of the Settings tab.
- A narration panel, "voice" packs or response *styles* — the persona surface
  removed in #136 on 2026-09-14 (§3.1).
- Running the LLM import inside the fleet UI. v1 imports from the CLI; the
  dialog says so (§4). Confirmation of LLM output is a terminal interaction.
- Persona inheritance, versioning fields, and converting a directory of skills
  in one go.
- A browser-verified UI task. This is a terminal UI; the tester verifies it
  headless with Textual's pilot, exactly as `tests/test_ui_project.py` does.

---

## 1. What already exists, and what this reuses

| Need | Already there |
| --- | --- |
| A place the agent reads standing instructions | the `SessionStart` hook → `services.team.hook_session_start` → `_render_board` → `harness.role_cycle` (the role's cycle and lane rule). One channel. |
| A way to tell the agent process *who it is* | `AISQUARE_ROLE`, exported by `src/aisquare/cli/launch.py`, read by `core.orchestrator.env_role`. The persona takes the same shape: `AISQUARE_PERSONA`. |
| The spawn itself | `services.fleet.spawn(project, role, *, label, task_id, worktree, permission_mode, binary, prompt, agent_args, spawned_by, account)` — every dialog field but persona exists today. |
| Running headless Claude Code safely | `core.harness.probe_model` + `_probe_env`: `claude -p … --output-format json --settings {} --strict-mcp-config --max-turns 1`, cwd = the aisquare home (never the repo), environment stripped of role, team, model overrides and tracing identity, registered in `core/spawn.py` `SEAMS` as *excluded, strips identity*. The import engine is the same shape with a different prompt. |
| Where Claude's own skills live | `core.agents._claude_home()` resolves the config dir (`CLAUDE_CONFIG_DIR`, else `~/.claude`); skills sit at `<config dir>/skills/<name>/SKILL.md` (personal) and `<repo>/.claude/skills/<name>/SKILL.md` (project). |
| Running a spawn off the UI thread | `views/project.py` `_start_manager` → `run_worker(thread=True, exit_on_error=False)` → `on_worker_state_changed`. Copy the pattern. |
| A modal | `ModalScreen` is already used twice in `cli/ui/app.py` (`HelpScreen`, `ThemePicker`). |
| Permission-mode choices | `views/settings.py` `PERMISSION_MODES` and `permission_options`. |
| Label rules and 🎲 | `services.fleet.LABEL`, `next_label`; `core/codenames` word lists (fleet-tui §5.7 names the 🎲 form `<role>-<adjective>-<animal>`). |
| The per-role config rung | `core.config.FleetRoleSettings` (`permission_mode`, `worktree`, `extra_args`) — gains `persona`. |
| Editing a text file in `$EDITOR` | `core.editor.edit_text` (already a registered spawn seam). |
| Stripping control characters from text we inject | `core.injection.sanitise_text`. |
| Store migrations | `core/store.py` `_MIGRATIONS` (v14 on this base); `docs/store-migration-race.md` for the rules. |
| A project-level dotdir | `.aisquare` is already a root marker (`core/workspace.py` `_ROOT_MARKERS`), so `<repo>/.aisquare/personas/` needs no new convention. |
| Typed models everywhere | pydantic (already a dependency) — the persona, the import draft and the API's structured output all validate through it. |

---

## 2. Architecture in one picture

```text
  SOURCES                          IMPORT (cli/persona.py · services/personas.py · services/persona_import.py — new)
  skill dir / SKILL.md ─┐
  agent .md / .mdc      ├─ recognised? ──yes──▶ copy verbatim ──▶ <layer>/personas/<name>/SKILL.md (+ files, + .persona.json)
  plain .md/.txt/.json  │                                             ▲
  yaml / stdin / https  ┘        no ──▶ LLM engines: manager (claude -p, headless, manager binding)
  Claude skill by name                          → api (anthropic SDK, optional extra)
                                                → refuse with the fix ──▶ draft → validate → show → confirm → save

  LAYERS (core/personas.py — new)         project  <repo>/.aisquare/personas/<name>/SKILL.md   wins
        parse (PyYAML) → Persona          user     $AISQUARE_HOME/personas/<name>/SKILL.md
        catalogue / resolve(name)         bundled  src/aisquare/personas/<name>/SKILL.md      read-only
              │
              ├──▶ aisquare fleet spawn <role> --persona NAME ──▶ aisquare launch <role> --persona NAME
              │        (services/fleet.py: validate, record        (cli/launch.py: validate, then
              │         fleet_agent.persona, receipt line)           AISQUARE_PERSONA=NAME in the env)
              │                                                              │  the agent process
              │                                              SessionStart hook → hook_session_start
              │                                                 records team_session.persona
              └──── briefing(persona) ─────────────────────────▶ <aisquare-team> … role cycle … lane rule …
                                                                  <aisquare-persona name= layer=> body </aisquare-persona>
                                                                  + the fixed guard sentence

  EXPORT  persona export NAME [--to DIR | --skill --user|--project]  ──▶ <config dir>/skills/NAME/  or  <repo>/.claude/skills/NAME/
  board row · fleet ls · sidebar badge  ◀── team_session.persona / fleet_agent.persona
  Spawn dialog (cli/ui/spawn.py, new) ──▶ fleet_service.spawn(…, persona=…)
```

The only process any of this starts is the import's headless Claude (§3.9),
and the only network it opens is the import's `https://` fetch and the API
engine — both behind an explicit `persona import`, never on a hook.

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
on the board, shareable as a directory that is also a Claude skill. It changes
how an agent *works*, not how a panel narrates. Three lessons from #136 are kept
on purpose:

- **One delivery channel.** `4cdab46` removed the `--append-system-prompt` path
  "so the two channels cannot fight". We have exactly one (§3.2).
- **Never per turn.** ~175 tokens a turn was measured and is why styles were off
  by default. Session start only.
- **Isolation is a test, not a promise.** Every factual surface — the board,
  `task`, `log`, `--json` — is byte-identical with and without a persona, and a
  test pins it (§7, `tests/test_persona_briefing.py`).

### 3.2 Delivery: the session-start briefing, not `--append-system-prompt`

The persona's SKILL.md body is rendered into the same `<aisquare-team>` block
that already carries the role's standing cycle — right after the lane rule,
before the closing tag. Reasons, in order of weight:

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
4. **Nothing on argv.** A persona body can be thousands of characters; argv is
   visible in `ps` and in every receipt.

Rejected: `--append-system-prompt-file`. Stronger placement in the system prompt
proper, but Claude-only, a second channel, invisible to the hook, and frozen in a
way that does not survive `/clear`. If adherence ever measures short, the
hook's block can be promoted for the default binary as a follow-up; the file
format and the board do not change.

### 3.3 Format: a persona IS a Claude Code skill

Decided by the owner on 2026-09-15, replacing the first cut's own `.md`
format. A skill is a directory `<name>/` with a `SKILL.md` — YAML frontmatter
between `---` lines, then a Markdown body — plus optional supporting files.
Claude Code loads them from `<config dir>/skills/` (personal) and
`<repo>/.claude/skills/` (project); the **directory name is the command**
(`/name`) and the frontmatter `name` is only a display label. Every frontmatter
field is optional to Claude Code; the open Agent Skills spec (agentskills.io)
requires `name` and `description`. Verified 2026-09-15 against
`code.claude.com/docs/en/skills` and against the 200 SKILL.md files on this
machine (every one carries `name` and `description`; the long tail carries
`allowed-tools`, `user-invocable`, `hooks`, `metadata`, …).

What that buys: **no aisquare format to learn or convert.** A persona someone
wrote for the fleet is `/skeptic` in their Claude Code the moment it is exported;
a skill someone already likes is a persona the moment it is imported; the
bundled personas are also four ready-made skills. The price is a real YAML
parser: Claude Code's frontmatter is full YAML — block scalars for long
descriptions, `metadata` maps, lists — and a "flat subset" parser would refuse
valid skills, which is the one thing an interchange format may not do. So:

- **PyYAML** becomes a core dependency (`pyyaml>=6.0`; `types-PyYAML` in the
  dev extra for mypy `--strict`). Read with `yaml.safe_load` only, on a
  frontmatter capped at 16 KB, **imported lazily inside the parser** so the
  hook path and `aisquare status` pay nothing until a persona is actually read
  (`tests/test_import_cost_of_the_integration.py` doctrine). The first plan's
  no-new-dependency stance is recorded as the alternative and why it lost.
- Everything in the frontmatter is **preserved verbatim**: import copies bytes,
  export writes the same bytes. aisquare never rewrites a file it did not
  author. Round-trip fidelity is a test (§7).

### 3.4 Layers, and Claude's own skill directories

```text
<repo>/.aisquare/personas/<name>/SKILL.md      project — shared with the repo, wins
$AISQUARE_HOME/personas/<name>/SKILL.md        user    — this machine, every project
src/aisquare/personas/<name>/SKILL.md          bundled — ships with the CLI, read-only
```

Same name in two layers: the higher wins and `persona list` prints `(shadows
user)` / `(shadows bundled)` on the winner. Bundled personas cannot be edited or
removed in place; `persona export skeptic --to …`, `persona import`, or
`persona new` into a layer is how one is copied and changed. `persona edit` on a
bundled name refuses and names those commands.

Claude's skill directories are **not** a fourth layer. A machine with 200 skills
would list 200 personas in the Spawn dialog, most of them workflows, none of
them personalities. They are **import sources**: `persona import --list` shows
every skill in `<config dir>/skills/` and `<repo>/.claude/skills/` with its
description and whether it is already a persona; `persona import <skill-name>`
copies one in; `persona export <name> --skill --user|--project` copies one out.
Explicit both ways, so the persona catalogue stays curated by the operator.

### 3.5 What a persona carries

```markdown
---
name: skeptic
description: Evidence-first. Distrusts green checkmarks; reproduces before believing.
metadata:
  persona-roles: tester, reviewer
  persona-tags: verification
---
You are a skeptic. A passing test is a claim, not a fact …
```

For aisquare to accept a directory as a persona (the "recognised" test of §3.9):

- a `SKILL.md` whose frontmatter parses as a YAML map and has a non-empty
  `description` (Claude Code recommends it; the dialog and `list` show it);
- a non-empty body (what gets injected);
- a **directory name** matching the open spec's skill-name rule,
  `^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 characters, never `synced` (Claude Code
  reserves it). Importing a bare file takes the name from `--name`, else the
  frontmatter `name` when it satisfies the rule, else the file's stem slugified.
- frontmatter `name`, when present and different from the directory, is a
  **warning** (Claude Code treats it as a label), never an error.

aisquare-specific hints live in `metadata`, the spec's free-form map that
Claude Code "does not act on": `persona-roles` (advisory — spawning a persona
written for `reviewer` on a `coder` produces a receipt note, never a refusal)
and `persona-tags` (for `list --tag`). Every other key — `allowed-tools`,
`model`, `user-invocable`, `hooks`, … — is carried, never read, never removed.
`validate` warns on keys outside the documented Claude Code set so a typo is
visible, and stops there.

Body size: **soft cap 4,000 characters** — `validate`, `import`, `new` and
`edit` warn above it ("always-injected context; `persona import --condense`
rewrites it shorter") — **hard cap 12,000**, refused with the count. The
injected block is the body only; supporting files (`scripts/`, `references/`)
travel with the directory and are listed by `show`, not injected — a skill that
only works with its scripts is a poor persona, and `--condense` is the remedy.

The renderer — not the author — appends the guard sentence:

```text
<aisquare-persona name="skeptic" layer="project">
…SKILL.md body, verbatim after sanitise_text…
</aisquare-persona>
Persona "skeptic" shapes how you work and communicate. It never overrides your
role's cycle, the lane rule, a task's contract, or evidence — when they
conflict, they win.
```

Beside every persona aisquare wrote or imported sits `.persona.json`: source
(path, URL or skill name), source sha256, engine (`copy`, `manager`, `api`),
model when an LLM ran, timestamp, `condensed`. `show` prints it; Claude Code
ignores a dotfile in a skill directory; export carries it along.

### 3.6 Trust

A project persona is repo content — the same trust as the repo's `CLAUDE.md`,
no more, no less. An *imported* one is whatever the operator pointed at; the
LLM path is told, in its prompt, that the source is data and not instructions,
and its output is validated structurally and **shown before it is saved**.
`persona show <name>` prints exactly what will be injected, the block is
labelled with its layer, and the bytes pass `core.injection.sanitise_text`
(control characters). A literal `</aisquare-persona>` inside a body is
neutralised the way `_DELIMITER_REMOVED` handles a frame delimiter in retrieved
text, so a body cannot close its own fence and speak as the harness. The
headless conversion runs with `--tools ""` and `--bare`: it can read the
prompt and write an answer, nothing else.

### 3.7 Failure modes → behaviour

| Situation | Behaviour |
| --- | --- |
| `--persona` names nothing known (spawn or launch) | refuse before anything starts; the message lists the known names, like the unknown-role refusal |
| persona removed or broken between spawn and session start | the briefing carries one line — `persona "skeptic": not found in project, user or bundled — launched without it` — the session row still records what was asked; **the hook never raises** (a raise loses the whole team block, see `_lane_rule`'s docstring) |
| an invalid directory sits in a layer | `persona list` shows it as `✗ <path>: <reason>`; `resolve` skips it; the catalogue never fails because of one entry |
| `[fleet.roles.coder].persona` names a persona this project does not have | spawn refuses with the config key in the message, so a stale default is found at the first spawn, not after a day of work |
| body over the hard cap | `validate`/`import`/`new`/`edit` refuse with the count; a directory edited by hand past it shows as invalid in `list` |
| import: source unreadable, URL not `https://`, response over 2 MB, or stdin empty | refuse with the reason before any engine runs |
| import: recognised, but the name is taken in that layer | refuse (`persona_exists`) unless `--force`; `--name` picks another |
| import: LLM output fails validation | one retry with the validator's message appended to the prompt; then the draft is saved to `$AISQUARE_HOME/personas/.drafts/<name>/SKILL.md`, its path printed, exit 1 `import_invalid` — nothing an engine produced is lost |
| import: `--json` or non-interactive without `--yes` | the draft is saved as above, exit 1 `needs_confirmation`, so a script can inspect and finish with `persona import <draft path>` |
| import: no engine can run | exit 1 `no_import_engine`, naming both fixes: start or bind a manager (`claude` on `PATH`, signed in) or install `aisquare-cli[llm]` and provide credentials |

### 3.8 Every default is a default (fleet-tui §3.10)

`per-spawn flag > [fleet.roles.<role>].persona > none`. No environment rung for
`[fleet]`, unchanged. `AISQUARE_PERSONA` is what `launch` *exports* and what the
hook *reads* — it is not a config input — so a hand-typed
`AISQUARE_PERSONA=skeptic aisquare launch coder` also works, with no fleet at all.

### 3.9 Import and export — the smart part

**The recognised test** (no LLM, no network, bytes copied): the source is a
directory with a `SKILL.md`, or a single Markdown file, whose frontmatter is a
YAML map with a non-empty `description`, with a non-empty body (§3.5). That
covers a Claude skill, a `.claude/agents/*.md` agent (has `name` and
`description`), a Cursor `.mdc` rule (has `description`), and the first plan's
own flat format. `--llm` forces the LLM path anyway (to reshape), `--no-llm`
forbids it (scripts that must never spend a token).

**The LLM path** runs when the recognised test fails — no frontmatter, a JSON
or YAML persona from another tool, a plain prompt in a `.txt`, a page fetched
from a URL — or when `--llm`/`--condense` asks for it. The engine is asked, in
one turn, for a skill: a `name` (slug), a one-line `description`, a body of at
most 4,000 characters that keeps the source's *intent* as an operating
personality, and `notes` on what it dropped. The output is **structured**
(§5: `PersonaDraft`), validated by the same code as the fast path, rendered to
a SKILL.md with `metadata.persona-source`, shown (frontmatter, first twelve body
lines, character count, engine and model) and confirmed — `y/N` on a TTY,
`--yes` to skip, otherwise §3.7's draft-and-exit. Provenance goes to
`.persona.json`.

**Engine ladder — `auto` (default), or forced with `--engine`:**

1. **`manager` — the fleet's own Claude Code, headless, under the manager
   role's binding.** `harness.resolve_binary("manager")` for the executable,
   `harness.resolve_profile("manager")` for its env (so the account and config
   dir the fleet's manager runs on are what pays), `harness.resolve_model(
   "manager", probe=False)` for the model (`fable → opus → sonnet`), then the
   `probe_model` shape: `claude -p --model … --bare --tools "" --max-turns 1
   --output-format json --json-schema <PersonaDraft schema>
   --no-session-persistence --settings {} --strict-mcp-config`, the source on
   **stdin** (Claude Code reads piped stdin in print mode; cap 200 KB), cwd the
   aisquare home, environment from a `_probe_env`-style stripper (no role, no
   team, no model overrides, no tracing identity) plus the manager profile's
   own variables, timeout 180 s. Registered in `core/spawn.py` `SEAMS` as
   *excluded, strips identity*, beside `probe_model`, with the same reason: a
   real model process that is not a session. Unavailable when the binary is
   not on `PATH`, not signed in, or exits non-zero — each a one-line reason in
   the receipt, then the ladder continues.

   *Why not the live manager pane?* There is no RPC into a running Claude Code
   session (verified: cross-session messaging is asynchronous plain text; the
   only other channel is typing into its terminal). Typing a conversion job into
   the manager's pane is a steering channel, not a call: it may land mid-task,
   its result has to be polled off disk, and none of it can be tested without
   tmux. Headless Claude under the manager's *binding* is the same account,
   the same ladder, one process, one answer. "Manager" here means whose Claude
   runs, not which window.

2. **`api` — the Anthropic API through the official `anthropic` SDK**, an
   optional extra `aisquare-cli[llm]` (`anthropic>=1`), imported inside the
   engine function only. A zero-argument client, so credentials resolve the
   SDK's way: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth
   login` profile — aisquare stores no key. Model `claude-opus-5` by default
   (`[persona.import] api_model`, or `--model`), `output_config.effort:
   "medium"`, `max_tokens` 16 000, non-streaming, **structured output via
   `client.messages.parse()` against `PersonaDraft`** so nothing is scraped
   out of prose. SDK missing → "install aisquare-cli[llm]"; no credentials →
   the SDK's `AuthenticationError`, reported as such; both let the ladder end
   with §3.7's `no_import_engine`. Usage tokens are printed — an import costs
   money and says so.

3. **Refuse**, naming both fixes.

`[persona.import]` in `config.toml`: `engine = "auto" | "manager" | "api" |
"off"`, `api_model = "claude-opus-5"`. `off` makes every non-recognised import
fail fast — for a machine that must never spend a token by accident.

**Sources**, resolved in this order: `-` (stdin) · an existing path (directory
or file) · an `https://` URL (stdlib fetch inside the function, 20 s, 2 MB,
`http://` refused) · the name of a skill in `<config dir>/skills/` or
`<repo>/.claude/skills/`. `persona import --list` prints those skills with their
descriptions and an `imported` mark.

**Export**: `persona export <name>` prints the SKILL.md to stdout; `--to DIR`
writes the whole directory (SKILL.md, supporting files, `.persona.json`) as
`DIR/<name>/`; `--skill --user` writes it under `<config dir>/skills/<name>/`
(`core.agents._claude_home()`, so a `CLAUDE_CONFIG_DIR` install exports into
*its* skills); `--skill --project` under `<repo>/.claude/skills/<name>/`. An
existing target refuses without `--force`. After `--skill`, the persona is
`/name` in Claude Code — the interchange the owner asked for, in one command.

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
| Persona *(P4)* | `Select` | the role's `[fleet.roles.<role>].persona`, else `(none)` | `core.personas.catalogue(project.root)`; the description shows under the field; follows the role until touched; a footer line reads `more: aisquare persona import <file, url or skill>` |
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

The contract below is what lets two coders work in parallel: P2, P4 and P5 code
against these names while P1 builds them.

```python
# src/aisquare/core/personas.py (new) — pure: directories in, models out. No store, no tmux, no network.
SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")   # 1–64 chars, never "synced"
BODY_SOFT_CAP = 4_000
BODY_HARD_CAP = 12_000
FRONTMATTER_MAX_BYTES = 16_384
Layer = Literal["project", "user", "bundled"]

class Persona(BaseModel):
    name: str                      # the directory name
    description: str               # frontmatter["description"], stripped
    frontmatter: dict[str, Any]    # everything, verbatim (yaml.safe_load)
    body: str                      # after the closing ---, stripped
    layer: Layer
    path: Path                     # the directory
    files: list[str] = []          # supporting files, relative, excluding SKILL.md and .persona.json
    roles: list[str] = []          # from metadata.persona-roles, advisory
    tags: list[str] = []           # from metadata.persona-tags
    provenance: Provenance | None  # .persona.json when present

class Provenance(BaseModel):
    source: str; source_sha256: str; engine: Literal["copy", "manager", "api"]
    model: str | None = None; imported_at: datetime; condensed: bool = False

class PersonaError(ValueError):
    """str(exc) names the path, the line (when there is one) and the rule."""

def split_frontmatter(text: str) -> tuple[str, str]                 # (yaml text, body) or PersonaError
def parse_skill(text: str, *, name: str, path: Path, layer: Layer) -> Persona   # the recognised test lives here
def load(directory: Path, *, layer: Layer) -> Persona
def layer_dirs(root: Path | None) -> list[tuple[Layer, Path]]       # precedence order; project only with a root
def catalogue(root: Path | None = None) -> tuple[list[Persona], list[tuple[Path, str]]]
    # loadable personas, first layer wins per name  ·  (path, reason) for every directory that did not load
def resolve(name: str, root: Path | None = None) -> Persona           # PersonaError lists the known names
def briefing(persona: Persona) -> list[str]                           # the lines the hook appends; sanitised; guard last
def render(name: str, description: str, body: str, *, metadata: dict[str, str]) -> str   # a canonical SKILL.md (new, LLM drafts)
def warnings(persona: Persona) -> list[str]                           # soft cap, unknown keys, label≠dir
```

```python
# src/aisquare/services/personas.py (new) — the file operations behind the CLI; every write goes through here
def import_source(source: str, *, layer: Layer, root: Path | None, name: str | None, force: bool,
                  llm: Literal["auto", "always", "never"], condense: bool, engine: str | None,
                  model: str | None, confirm: Callable[[PersonaDraftView], bool]) -> ImportResult
def importable_skills(root: Path | None) -> list[SkillRef]           # <config dir>/skills + <repo>/.claude/skills
def new(name: str, *, layer: Layer, root: Path | None) -> Path        # scaffold, then edit
def edit(name: str, *, root: Path | None) -> Persona | None            # $EDITOR via core.editor.edit_text; invalid result keeps the old file
def remove(name: str, *, layer: Layer, root: Path | None) -> Path      # bundled: PersonaError
def export(name: str, *, root: Path | None, to: Path | None, skill: Literal["user", "project"] | None, force: bool) -> Path | str
def validate(path: Path) -> tuple[Persona, list[str]]                  # persona + warnings
```

```python
# src/aisquare/services/persona_import.py (new) — the LLM engines; nothing here is imported by a hook
class PersonaDraft(BaseModel):          # the structured output both engines must produce
    name: str; description: str; body: str; notes: list[str] = []

class EngineUnavailable(RuntimeError): ...   # one-line reason; the ladder continues
def draft_with_manager(source_text: str, *, condense: bool, timeout: float = 180.0) -> PersonaDraft   # claude -p, §3.9
def draft_with_api(source_text: str, *, condense: bool, model: str) -> PersonaDraft                    # anthropic SDK, §3.9
def draft(source_text: str, *, engine: Literal["auto", "manager", "api", "off"], condense: bool, model: str) -> tuple[PersonaDraft, str, str | None]
    # (draft, engine that ran, model) — or ImportRefused with every reason the ladder collected
```

```python
# elsewhere
core.orchestrator.env_persona() -> str | None                   # AISQUARE_PERSONA, like env_role
core.config.FleetRoleSettings.persona: str | None = None        # the config rung
core.config.PersonaImportSettings(engine="auto", api_model="claude-opus-5")   # [persona.import]
models.TeamSession.persona: str | None = None                   # recorded at session start
models.FleetAgent.persona: str | None = None                    # recorded at spawn
services.fleet.spawn(..., persona: str | None = None)           # None → role config → none
```

Files, by task (§6):

| Task | New | Changed |
| --- | --- | --- |
| **P1** persona core | `src/aisquare/core/personas.py`, `src/aisquare/services/personas.py`, `src/aisquare/cli/persona.py`, `src/aisquare/personas/{skeptic,mentor,minimalist,careful}/SKILL.md`, `docs/personas.md`, `tests/test_personas.py`, `tests/test_persona_cli.py` | `pyproject.toml` (`pyyaml`, `types-PyYAML`), `src/aisquare/cli/app.py` (register the group), `tests/test_stubs.py` (IMPLEMENTED), `CHANGELOG.md` |
| **P2** persona wiring | `tests/test_persona_briefing.py` | `cli/launch.py`, `core/orchestrator.py`, `services/team.py`, `core/store.py` (v15), `models.py`, `core/config.py`, `services/fleet.py`, `cli/fleet.py`, `docs/fleet.md`, `tests/test_fleet_service.py`, `tests/test_fleet_cli.py`, `tests/test_launch.py`, `tests/test_store.py`, `CHANGELOG.md` |
| **P3** Spawn dialog | `src/aisquare/cli/ui/spawn.py`, `tests/test_ui_spawn.py` | `cli/ui/app.py` (`on_spawn_agent`), `docs/fleet.md` (the dialog paragraph), `CHANGELOG.md` |
| **P5** smart import | `src/aisquare/services/persona_import.py`, `tests/test_persona_import.py` | `services/personas.py` (`import_source` gains the LLM branch), `cli/persona.py` (`--llm/--no-llm`, `--condense`, `--engine`, `--model`, `--yes`), `core/config.py` (`[persona.import]`), `core/spawn.py` (the new seam), `pyproject.toml` (`llm` extra), `docs/personas.md`, `CHANGELOG.md` |
| **P4** persona in the UI | — | `cli/ui/spawn.py` (the Select + footer), `cli/ui/views/settings.py` (per-role default), `cli/ui/sidebar.py` (badge), `tests/test_ui_spawn.py`, `tests/test_ui_project.py`, `docs/fleet.md`, `docs/personas.md` |

The bundled set, four operating personalities as skill directories (bodies
≤ 1,200 characters each, owner reviews the wording in P1's PR): **skeptic** —
evidence first, reproduces before believing, names what it did not check;
**mentor** — explains the why before the what, surfaces the alternatives it
weighed; **minimalist** — the smallest change that meets the contract, no
speculative abstraction, says what it left out; **careful** — prefers reversible
steps, states assumptions before acting, asks one precise question when blocked
rather than guessing. Each is also a valid Claude skill: `persona export
skeptic --skill --user` and it is `/skeptic`.

Config, the whole of it:

```toml
[fleet.roles.coder]
permission_mode = "auto"
worktree = true
persona = "minimalist"        # (new) default for every coder spawn; a flag still wins

[persona.import]              # (new)
engine = "auto"               # auto | manager | api | off
api_model = "claude-opus-5"   # the api engine's model; the manager engine rides the manager ladder
```

---

## 6. Delivery — five tasks, two coders, one tester

Board: the **aisquare-cli** board (project `prj_61a0b873…`, codename
`solar-sparrow`). Tasks are titled `[HACK][P<n>][coder] …`; `--needs` carries the
ordering. Both coders start at once.

| # | Task | Coder | Needs | Size |
| --- | --- | --- | --- | --- |
| **P1** | Persona core: personas are skill directories, PyYAML parser, three layers, `catalogue`/`resolve`/`briefing`, four bundled skills, the `aisquare persona` group with the **recognised-path** `import`, `import --list`, `export --skill`, `docs/personas.md` | `coder-persona-core` | — | M |
| **P3** | The Spawn dialog over today's `spawn()` — every field except persona; `on_spawn_agent` opens it | `coder-spawn-dialog` | — | M |
| **P2** | Persona wiring: `launch --persona` + `AISQUARE_PERSONA`, the hook records and renders once, store v15, `fleet spawn --persona`, `[fleet.roles.<role>].persona`, board / `fleet ls` show it | `coder-persona-core` | P1 | M |
| **P5** | Smart import: the `manager` and `api` engines, structured drafts, validate → show → confirm, drafts on refusal, provenance, `[persona.import]`, the `llm` extra, the seam ruling | `coder-spawn-dialog` | P1 | M |
| **P4** | Persona in the UI: the dialog's Select (default from role config) with the import hint, per-role default in Settings, sidebar badge | `coder-spawn-dialog` | P2, P3 | S |

Board ids are recorded in §10 when the tasks are (re)issued.

Tester (`tester-hackathon`): spawned when the first task reaches review; its
prompt names the worktree and branch to check, because nothing moves a tester
into a coder's tree (`docs/fleet.md`, the roles table). Reviewer once a PR
exists; validator once all five are done.

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
  layer, path, files` per entry plus an `invalid` list.
- `aisquare persona show skeptic` prints the exact block `briefing()` renders,
  guard sentence last, then the provenance and supporting files; `--json`
  carries `frontmatter`, `body`, `briefing`, `provenance`.
- Import, recognised path: `aisquare persona import ./some-skill --user` copies
  the directory byte-for-byte into `$AISQUARE_HOME/personas/some-skill/` and
  writes `.persona.json` (`engine: copy`, sha256); a second run refuses
  (`persona_exists`) without `--force`; `--name other` renames the directory
  only; `import ./agent.md` (a `.claude/agents`-shaped file) lands as
  `<stem>/SKILL.md`; `import -` reads stdin; `import ./notes.txt` (no
  frontmatter) exits 1 `not_recognised` **in P1** with the message naming the
  LLM path P5 adds — never a traceback.
- `aisquare persona import --list` prints every skill under `<config dir>/skills`
  and `<repo>/.claude/skills` with description and an `imported` mark; `--json`
  parses. `import <skill-name>` copies one in.
- `aisquare persona export skeptic` prints the SKILL.md; `export skeptic --to
  DIR` writes `DIR/skeptic/` (SKILL.md + `.persona.json`); `export skeptic
  --skill --user` writes `<config dir>/skills/skeptic/SKILL.md` (with
  `CLAUDE_CONFIG_DIR` pointed at a temp dir in the test); an existing target
  refuses without `--force`.
- **Round trip**: a skill directory with a multi-line `description: >`,
  `metadata`, `allowed-tools` and a `references/` file, imported then exported,
  is byte-identical (SKILL.md and every supporting file).
- `aisquare persona new pair --project` inside a repo creates
  `<repo>/.aisquare/personas/pair/SKILL.md` from the template and opens
  `$EDITOR` (tests use `EDITOR=true`).
- `aisquare persona edit skeptic` refuses: bundled, names the copy commands.
  `edit` on a user persona with an editor that returns invalid text leaves the
  old file byte-identical and exits 1 with the rule.
- `aisquare persona validate <dir>`: exit 0 with warnings for unknown keys, a
  label `name` that differs from the directory, and a body over 4,000; exit 1
  naming the line for unparseable YAML, a frontmatter over 16 KB, a missing or
  empty `description`, an empty body, a body over 12,000 (with the count), a
  directory name that fails the skill-name rule or is `synced`.
- `aisquare persona rm x --user` removes it; `rm skeptic` refuses (bundled).
- Precedence: a project `skeptic/` wins over bundled and `list` says
  `(shadows bundled)`; an unreadable directory in a layer appears under
  `invalid` and does not stop the others from loading.
- `python -X importtime -c "import aisquare.cli.app"` shows no `yaml` module:
  PyYAML is imported inside the parser only.
- Guards: `test_stubs`, the configured-home sweep, the JSON-stdout sweep,
  `test_import_cost_of_the_integration` and `test_spawn_seams` (no new process
  in P1) are green with the new group.

**P5 — smart import**

- With a fake runner in place of the subprocess (the `tests/test_harness.py`
  probe pattern): `aisquare persona import ./notes.txt --user --yes` builds the
  argv `claude -p --model <manager ladder pick> --bare --tools "" --max-turns 1
  --output-format json --json-schema … --no-session-persistence --settings {}
  --strict-mcp-config`, feeds the source on stdin, runs with cwd = the aisquare
  home and an env that has no `AISQUARE_ROLE`, `AISQUARE_FLEET_AGENT`,
  `ANTHROPIC_BASE_URL` or `ANTHROPIC_CUSTOM_HEADERS` and has `AISQUARE_TEAM=0`
  **and** the manager profile's own variables; the fake's JSON answer becomes
  `<name>/SKILL.md` with `metadata.persona-source` and a `.persona.json`
  (`engine: manager`, the model). The binary is the manager's binding
  (`AISQUARE_BIN_MANAGER=other-claude` → argv[0] is `other-claude`).
- Fake runner exits non-zero or times out → the receipt names the reason and
  the ladder tries `api`; with `anthropic` uninstalled the refusal names
  `aisquare-cli[llm]`; with a fake `anthropic` module whose `messages.parse`
  returns a `PersonaDraft`, the import succeeds with `engine: api`, the model
  from `--model`/config, and the usage tokens printed. `--engine manager` never
  touches `api` and vice versa. `[persona.import].engine = "off"` refuses the
  LLM path with `import_engine_off`.
- A draft failing validation (body over 12,000, bad name) is retried once with
  the validator's message in the prompt; a second failure saves the draft under
  `$AISQUARE_HOME/personas/.drafts/<name>/SKILL.md`, prints the path, exits 1
  `import_invalid`.
- Without `--yes`: on a TTY the confirmation shows frontmatter, the first twelve
  body lines, the character count, engine and model, and `n` saves nothing; with
  `--json` or a non-TTY the draft is saved and the exit is 1 `needs_confirmation`
  with the path in the payload; `persona import <that path>` then finishes on
  the recognised path.
- `--condense` on a recognised 9,000-character skill runs the LLM path and the
  result's body is ≤ 4,000 with `condensed: true` in `.persona.json`.
- `import https://…` fetches with the stdlib inside the function (fake
  `urlopen`), refuses `http://`, refuses a body over 2 MB; the prompt frames the
  source as data.
- `core/spawn.py` `SEAMS` gains
  `aisquare/services/persona_import.py::draft_with_manager` as *excluded,
  strips identity*; `tests/test_spawn_seams.py` green; `tests/
  test_no_network_on_the_primary_path.py` green (no hook imports
  `persona_import`); `python -X importtime -c "import aisquare.cli.app"` shows
  neither `anthropic` nor `urllib.request`.
- `docs/personas.md` gains the import section with the engine ladder, cost
  note and the config keys; `test_documented_commands` green.

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
  flag is absent and a flag beats it; a persona whose `persona-roles` exclude
  the spawned role adds a receipt note.
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
  updates under the field; the footer names `aisquare persona import`; the
  recorder receives `persona="skeptic"`.
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
| `tests/test_spawn_seams.py` | exactly one new call site, `services/persona_import.py::draft_with_manager`, ruled *excluded, strips identity* with its reason; `persona edit` goes through the registered `core/editor.py::edit_text` seam; nothing else starts a process. |
| `tests/test_documented_commands.py` | every `aisquare …` line in a shell fence must exist; this plan uses `text` fences until the commands land. |
| `tests/test_stubs.py` + `tests/cli_tree.py` | every new leaf is enumerated and listed in `IMPLEMENTED`; it reports canonically. |
| `tests/test_no_traceback_in_a_configured_home.py` | every new command runs in a configured home without raising — including `persona edit` with no `$EDITOR`, `persona show` of a name that does not exist, and `persona import` of a missing path. |
| `tests/test_json_stdout_is_machine_readable.py` | under `--json`, stdout is JSON or empty for every new command, including the `needs_confirmation` exit. |
| `tests/test_import_cost_of_the_integration.py` and its doctrine | `yaml`, `anthropic` and `urllib.request` are imported inside functions only; `aisquare status` and every hook import none of them. |
| `tests/test_no_network_on_the_primary_path.py` | no hook path reaches `persona_import`; the only sockets are behind `persona import`. |
| `tests/test_config_unknown_keys.py`, `test_config_durable_replace.py` | `FleetRoleSettings.persona` and `[persona.import]` round-trip; an older build that lacks them keeps the keys (`_keep_unknown`). |
| `tests/test_config_writes_stay_in_the_cli.py` | the Settings tab writes through `save_config` from the UI layer as it does today; persona *directories* are not config and are written by `services/personas.py`. |
| `tests/test_store_migration_race.py`, `tests/test_damaged_store_recovery.py` | v15 follows the race rules; a damaged store still degrades quietly. |
| `tests/test_the_roster_covers_every_identity_we_emit.py` | unchanged — a persona is not a role and mints no identity; the import's headless run strips identity like the probe. |
| `tests/test_fleet_docs_are_true.py` | every present-tense claim added to `docs/fleet.md` is mechanically true. |
| `tests/test_ui_shell.py`, `tests/test_terminal_pane.py` | the modal does not intercept keys the pane owns; opening and closing the dialog leaves the shell's bindings as they were. |
| `tests/test_packaging.py` | the wheel carries `src/aisquare/personas/*/SKILL.md` (hatch includes package files; assert it). |
| `make check` (ruff format + lint, mypy `--strict`, pytest) | at every PR, in the worktree's `.venv`; `types-PyYAML` keeps mypy strict. |

---

## 9. Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| **PR #136** (`codex/native-personas-workflow`, open, base `main`) rewrites `cli/launch.py`, `services/fleet.py`, `services/team.py`, `core/harness.py` | high | merge conflicts for P2 | P2 keeps its changes additive and local (one new option, one new column pair, one new block in `_render_board`); whoever lands second rebases. The owner tells Surjoyday this plan exists. |
| **PR #169 (#144)** adds `fleet_agent.account_slot` and `launch_spec` as store **v15–v18** on its branch | high | migration-number collision | v15 here is provisional; the second to land renumbers (the v13/v14 precedent recorded in `core/store.py`). When #144 lands, `persona` joins `LaunchSpec` so a restart replays it — a one-line follow-up named in the Decisions log. |
| The tmux server that runs this fleet carries `AISQUARE_TEAM_HUB=…/aisquare-ci` in its **global environment**, so any agent spawned on it reads the aisquare-ci board | certain today | coders claim the wrong board's tasks | operational, not code: `tmux -L asqui set-environment -gu AISQUARE_TEAM_HUB` before the first spawn (a window inherits the server's environment, not the spawner's — `services/fleet.py`'s own comment). Recorded here so the next fleet does not rediscover it. |
| **PyYAML** is the first non-typing dependency added since Textual | certain | install footprint, a dependency to track | pure-Python wheels on every platform; imported lazily; `safe_load` only; the owner accepted it for interchange (§3.3). Alternative if reversed: the first plan's flat parser plus the LLM path for everything it cannot read. |
| The LLM import produces a plausible persona that misreads the source | medium | a wrong personality, silently | never saved unseen: validate → show → confirm; `notes` from the engine say what was dropped; `.persona.json` names source and engine; `--no-llm` for scripts. |
| Headless flags drift: `--tools ""`, `--json-schema`, `--bare` are in 2.1.272's `--help` but the headless docs page lags | low | the manager engine breaks on an older or newer Claude Code | the engine checks the exit code and the JSON envelope, never assumes; a non-zero exit is a one-line reason and the ladder continues to `api`; `harness.probe_model` already carries the same exposure. |
| `CLAUDE_CONFIG_DIR` relocating the personal skills dir is not documented | low | `export --skill --user` writes where Claude does not read | `_claude_home()` is what the fleet already uses for hooks and settings and it works on this machine's `~/.claude4`; the path is printed on export so the operator sees it. |
| Hook context is weaker than the system prompt; a persona may not hold for a long session | medium | persona fades | acceptable for v1 (the role cycle governs from the same place and demonstrably holds); `/clear` re-briefs; promotion to `--append-system-prompt-file` for the default binary is a contained follow-up (§3.2). |
| Import spends money or quota | certain, by design | surprise cost | the receipt names engine and model; the API engine prints usage; `[persona.import].engine = "off"`; nothing runs an LLM without `import` being typed. |
| Two coders touch `docs/fleet.md`, `docs/personas.md` and `CHANGELOG.md` | certain | trivial conflicts | each PR adds its own paragraph/bullet; rebase on `rc/hackathon-v1` before review. |

---

## 10. Decisions log

| Date | Decision | Who |
| --- | --- | --- |
| 2026-09-15 | Plan authored; `rc/hackathon-v1` cut from `release/2026-09-12` @ `5ec92b5` and pushed. First-cut defaults: Markdown + flat YAML front matter, no new dependency; delivery via the session-start briefing (§3.2); layers project > user > bundled (§3.4); body cap 4,000; four bundled starters (§5); no `UI:` prefix on the dialog tasks because a browser ui-tester cannot verify a Textual dialog. Tasks issued: P1 `tsk_01m2hkec4s10veyxy1d2rc2jnm`, P3 `tsk_01m2hkecm1tgegphnqa9kt398s`, P2 `tsk_01m2hked3g7e26f4zs0x5ypyjw`, P4 `tsk_01m2hkedk67vpy4gnt3bar1hmm`. | manager `8e92f6af` |
| 2026-09-15 | **Owner redirect:** a persona is a Claude Code skill directory (§3.3), interchangeable both ways through `import`/`export --skill` (§3.9). PyYAML accepted as a core dependency for interchange fidelity. Smart import: recognised skills copy verbatim; everything else goes through an LLM — `manager` engine = headless Claude Code under the manager role's binding (not the live pane: no RPC exists), then `api` via the official SDK as an optional extra, then refuse. Body caps 4,000 soft / 12,000 hard. `roles`/`tags` moved under `metadata`. P1 re-specified, **P5** added (needs P1), P2/P4 re-issued for the dependency ids; P3 unchanged. Ids: P1 `tsk_01m2hmms453d9veehg44p5xzx0` · P5 `tsk_01m2hmmsmqp4yn0s57hksnz24t` (needs P1) · P2 `tsk_01m2hmmt4e06kr6z08m6pmdmqx` (needs P1) · P4 `tsk_01m2hmmtm4afjwprvz5tc26j5a` (needs P2, P3); P3 keeps `tsk_01m2hkecm1tgegphnqa9kt398s`; the first-cut P1/P2/P4 were dropped. Deferred: LLM import inside the UI, batch import, `extends`. Follow-up when #144 lands: `persona` joins `LaunchSpec`. | owner + manager `8e92f6af` |
