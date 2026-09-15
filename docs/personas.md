# Personas

A persona is *how* an agent works — a skeptic that reproduces before it
believes, a minimalist that makes the smallest change — written down once and
given to an agent when it starts. In aisquare a persona **is a Claude Code
skill**: the same directory is `--persona skeptic` to the fleet and `/skeptic`
in Claude Code, byte for byte. There is no aisquare format to learn and nothing
to convert.

This guide covers what ships today: the persona catalogue, the
`aisquare persona` commands — importing anything, a skill or not — running an
agent as a persona, attaching one to a running agent, and the persona views in
`asq`: the Personas tab, the Spawn dialog's persona step, and the target
picker that joins them — attach a persona to a running agent or to a new one, in
two steps.

## What a persona is

A directory named for the persona, holding a `SKILL.md`: YAML frontmatter
between `---` lines, then a Markdown body. Supporting files may sit beside it.

```text
skeptic/
  SKILL.md
  references/checklist.md     (optional — travels with the persona, never injected)
  .persona.json               (written by aisquare on import or export — where it came from)
```

```markdown
---
name: skeptic
description: Evidence first. Treats a green check as a claim, reproduces before believing.
metadata:
  persona-roles: tester, reviewer
  persona-tags: verification
---
You are a skeptic. A passing test is a claim, not a fact …
```

What aisquare requires of a directory before it counts as a persona:

- a `SKILL.md` whose frontmatter is a YAML map with a non-empty `description`;
- a non-empty body — the body is what an agent is briefed with;
- a **directory name** Claude Code would run: lowercase letters, digits and
  single hyphens, 1 to 64 characters, and never `synced` (Claude Code keeps
  claude.ai's skills there).

The directory name is the persona's identity. A frontmatter `name` is a label;
one that differs from the directory is a warning, not an error. Everything else
in the frontmatter — `allowed-tools`, `model`, `hooks`, your own keys — is
carried untouched and never read. aisquare's own hints live under `metadata`,
the map Claude Code does not act on: `persona-roles` (advisory) and
`persona-tags` (for `persona list --tag`).

Sizes: the body is always-injected context, so above **4,000 characters** you
get a warning, and above **12,000** the persona is refused with the count. The
frontmatter is capped at 16 KB.

When a persona is used, the agent receives exactly what `persona show` prints:
the body inside an `<aisquare-persona>` block, then one sentence aisquare adds —
the persona shapes how the agent works and communicates, and never overrides its
role's cycle, the lane rule, a task's contract, or evidence. A body line that
carries an aisquare frame tag is replaced, so a persona cannot close its own
block.

## Where personas live

```text
<repo>/.aisquare/personas/<name>/SKILL.md     project — shared with the repo, wins
$AISQUARE_HOME/personas/<name>/SKILL.md       user    — this machine, every project
(inside the aisquare package)                 bundled — ships with the CLI, read-only
```

The project layer is the git repository around your working directory. When
two layers hold the same name the higher one wins, and `persona list` marks the
winner `(shadows bundled)`. A directory that does not load is listed as
`✗ <path>: <reason>` and never hides the others.

Claude Code's own skill directories — `<config dir>/skills/` and
`<repo>/.claude/skills/` — are **not** a layer: most skills are workflows, not
personalities. They are where `persona import` finds skills and where
`persona export --skill` puts personas.

## The bundled four

| Persona | How it works |
| --- | --- |
| `skeptic` | Evidence first; reproduces before believing; names what it did not verify. |
| `mentor` | Explains the why before the what; surfaces the alternatives it weighed. |
| `minimalist` | The smallest change that meets the contract; says what it left out. |
| `careful` | Reversible steps; states assumptions; asks one precise question when blocked. |

Bundled personas cannot be edited or removed in place. Copy one into a layer you
own and change the copy — the copy shadows the bundled one.

## Running an agent as a persona

```sh
aisquare launch coder --persona skeptic
aisquare fleet spawn coder --persona skeptic
```

`launch --persona` checks the name against this project's personas — an unknown
one is refused with the names that exist — and exports `AISQUARE_PERSONA` to the
agent. When the session starts, its hook records the persona on the board row
and adds exactly the block `persona show` prints to the team briefing, once,
after the role's cycle. Nothing is added per prompt, and the board shows only the
name: `persona:skeptic` on the session's line, `· skeptic` in `fleet ls`. A
persona removed or broken after launch costs one line in the briefing
(`… — launched without it`), never the session. Setting the variable by hand,
`AISQUARE_PERSONA=skeptic aisquare launch coder`, works too; it is checked when
the session starts rather than before.

`fleet spawn --persona` passes the name to `launch` inside the agent's window and
records it on the fleet row; without the flag the role's
`[fleet.roles.<role>].persona` applies (see `docs/fleet.md`). A persona whose
`persona-roles` leave the spawned role out is spawned anyway, with a `⚠` note on
the receipt.

To give an agent that is already running a persona:

```sh
aisquare persona attach skeptic --to coder-auth
```

`persona attach` records the persona on the agent's fleet row (and on its board
session, once it has joined) and delivers the briefing the way `fleet tell`
delivers anything: typed into the agent when it is waiting, filed as a board note
addressed to it when it is busy — the receipt says `typed` or `noted`. The board
gets one `persona_attached` line, with the name and never the body. Because the
session-start hook reads the fleet row first, the agent is briefed with the
persona again after a `/clear` or a restart. That includes an agent spawned with
`--persona`, whose `AISQUARE_PERSONA` holds only the value it was launched with.
The variable applies when the agent has no fleet row, or a row with no persona,
and either one wins over a persona a session recorded earlier. Attaching another
persona replaces it, and the agent is told which one it replaces.
In `asq`, the Spawn dialog (`＋ spawn agent` under a project) asks for the
persona right after who runs the agent — role, account, binary — with the role's
default preselected and the persona's description under the field; `(none)`
spawns without one even when the role has a default. The Settings tab sets each
role's default persona, and an agent running as a persona carries a dim
`· skeptic` on its sidebar row.

## In `asq`: the Personas tab

Open a project in `asq` and its **Personas** tab lists every persona that
project can use — its own layer (when the project is a git repository), yours,
and the bundled four — with the layer, the description, the roles, and a mark:
`⇧ shadows bundled` on a persona that hides a lower one, `✗ invalid` (greyed,
the reason in the description column) on a directory that does not load. The
search box narrows by name, description or tag; the `project` `user` `bundled`
checkboxes narrow by layer.

Beside the list, the preview is exactly what an agent is briefed with — the
same block `persona show` prints — then the directory, its supporting files,
where it came from (`.persona.json`) and its warnings.

For the selected persona:

- **Attach to existing** (`a`) and **Attach to new** (`n`) open the target
  picker — see *Attach in two steps* below.
- **Edit** (`e`) opens the whole `SKILL.md` in the tab. It is checked as you
  type — the first error, or the warnings — and *Save* is enabled only while it
  parses; it saves through the same writer as `persona edit`, so a refused edit
  leaves the file as it was. `Enter` on any row opens it too; a bundled persona
  opens read-only, with *Save as…* to copy it into your layer or the project's.
- **Export…** (`x`) writes it into Claude Code's personal skills, the
  project's `.claude/skills`, or a directory — after either skills target it is
  `/<name>` in Claude Code.
- **Remove** (`Del`) asks once, naming the directory it deletes.
- **Validate** (`v`) reports the rule a directory breaks, or its warnings.

Bundled rows cannot be edited or removed ("bundled — export to a layer first");
an invalid row cannot be attached or exported, and Edit is how you fix it.

Above the list, **+ Import…** (`i`) is `persona import` as a form: a source
(a skill directory, a `SKILL.md` or `.md` file, or a skill name — *Browse
skills* lists Claude Code's skills and marks the ones already imported), the
layer, a name, *Condense*, the engine and model for the LLM path, and *Force*.
Progress appears under the form while it runs; a draft an LLM wrote is shown in
full — engine, model, size, frontmatter, body, what it dropped — before *Save*
keeps it or *Discard* leaves it under `.drafts`. A refusal stays in the form
with its reason. **+ New** asks for a name, a layer and a description, creates
the scaffold, and opens it in the editor.

## Attach in two steps

In `asq`, a persona reaches an agent in two steps: pick the persona, then pick
who runs it.

1. **The persona.** In the project's **Personas** tab, select one and press
   **Attach to existing** (`a`) or **Attach to new** (`n`).
2. **Who runs it.** The target picker lists three sections, and the button you
   pressed decides their order — every section stays selectable either way:
   - **Agents** — this project's running agents, each with the persona it runs
     now. Choosing one asks once ("attach skeptic to coder-auth? It replaces
     mentor."), then attaches exactly as `persona attach` does; the toast says
     whether the briefing was `typed` into a waiting agent or `noted` on the
     board for a busy one.
   - **Binds** — the seats `aisquare team bind` pinned, with the binary and the
     account each one's environment points at. Choosing one opens the Spawn
     dialog with that seat as the role, its binary, and the persona filled in.
   - **Accounts** — the Claude Code account slots. Choosing one opens the Spawn
     dialog on that slot, with the persona.

   *Attach to existing* puts Agents first; *Attach to new* puts Binds, then
   Accounts, then Agents. The filter box narrows all three sections at once.

Two buttons under the list make a target on the spot. **+ New bind** is `team
bind` as a form — a seat (a role, a numbered seat such as `coder2`, or a name
already bound), a binary that must be on your `PATH`, an account whose
`CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR` it fills in, more `KEY=VALUE` lines
and extra args — and the list comes back with the new bind selected. **+ New
account** takes you to the Accounts page and starts its sign-in for a new slot;
come back to the Personas tab when it has signed in.

The same picker is the Spawn dialog's **Pick…**: from `＋ spawn agent`, Pick…
opens it with Binds first and fills the dialog's role, binary or account from
what you choose, keeping everything else you typed. **Import…** beside the
dialog's Persona field imports a persona and selects it.

## Commands

Every reporting command takes `--json` before the subcommand
(`aisquare --json persona list`); a refusal is then one JSON object with `error`
and `detail`.

### See what there is

```sh
aisquare persona list
aisquare persona list --tag verification
aisquare persona show skeptic
```

`show` prints the exact block an agent is briefed with, then where the persona
came from and its supporting files.

### Import a skill you already have

```sh
aisquare persona import --list
aisquare persona import ./skills/code-review --user
aisquare persona import code-review --project
aisquare persona import ./agents/reviewer.md --user --name careful-reviewer
```

A source is a skill directory, a single Markdown file with frontmatter (a
`.claude/agents/*.md` agent or a Cursor `.mdc` rule qualify), `-` for stdin, or
the name of a skill from `import --list`. The directory is copied byte for byte
— aisquare never rewrites a file it did not author — and a `.persona.json` beside
it records the source and its sha256. A bare file becomes `<name>/SKILL.md`,
named by `--name`, else its frontmatter `name`, else its file name.

`import --list` marks a skill `imported` when a user or project persona's
`.persona.json` names that skill as its source, whatever the persona was called.
A persona of the same name that did not come from it (a bundled one, say) is
not an import: the row ends `(name taken by bundled careful)`, which is why
importing that skill needs `--name`.

The default layer is `--user`. A name already taken in that layer is refused
unless you pass `--force`; `--name` picks another directory name.

### Import anything else — the LLM path

```sh
aisquare persona import ./notes/how-we-review.txt
aisquare persona import https://example.com/our-reviewer.md --user
aisquare persona import ./skills/long-skill --condense --yes
aisquare persona import ./prompt.txt --engine api --model claude-opus-5
aisquare persona import ./skills/code-review --no-llm
```

A source that is not a recognised skill — plain text, a JSON or YAML persona from
another tool, a page fetched over `https://` (20 s, 2 MB; `http://` is refused) —
is converted into a skill by an engine: a name, a one-line description, a body of
at most 4,000 characters that keeps the source's intent, and notes on what it
dropped. `--llm` sends a recognised skill through an engine anyway, to reshape
it; `--condense` asks for a shorter body — the remedy for a skill over the
4,000-character soft cap, or even over the 12,000 hard cap; `--no-llm` forbids the
path, for a script that must never spend a token. The source is given to the
engine as data, never as instructions.

The engines, tried in this order unless `--engine` picks one:

1. **manager** — the fleet's own Claude Code, headless, under the manager role's
   binding (`team bind manager`), so the manager's account pays. One turn, no
   tools, run from the aisquare home with the environment stripped of any role,
   team or tracing identity.
2. **api** — the Anthropic API through the official SDK, installed with
   `pip install 'aisquare-cli[llm]'`. Credentials are the SDK's own
   (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or `ant auth login`); aisquare
   stores no key. The token usage is printed.
3. Neither can run → `no_import_engine`, naming both fixes.

**An engine import costs money or quota**, and the output says which engine and
model ran. The draft is held to the same rules as a copied skill (one retry, told
what failed), written to `$AISQUARE_HOME/personas/.drafts/<name>/SKILL.md` before
anything else, and shown — frontmatter, the first twelve body lines, the character
count, engine, model and notes — for a `y/N`. `--yes` saves without asking. With
`--json`, or without a terminal, the draft is kept and the import exits
`needs_confirmation` with the draft's path; importing that path saves it. A draft
that fails validation twice is kept as well (`import_invalid`). A saved import
records engine, model and `condensed` in `.persona.json`.

```toml
[persona.import]
engine = "auto"               # auto | manager | api | off — off refuses every conversion
api_model = "claude-opus-5"   # the api engine's model; the manager rides the manager's ladder
```

If `config.toml` cannot be read, a conversion is refused with
`config_unreadable`, naming only the error's class. A broken file may be the
one that says `engine = "off"`, so its defaults are not assumed. Fix the file, or
pass `--engine` to choose an engine for that one import; the output then says
the config was not read.

### Export a persona

```sh
aisquare persona export skeptic
aisquare persona export skeptic --to ./persona-copies
aisquare persona export skeptic --skill --user
aisquare persona export skeptic --skill --project
```

With no destination the `SKILL.md` is printed. `--to DIR` writes the whole
directory as `DIR/<name>/`. `--skill --user` writes it into Claude Code's
personal skills (`$CLAUDE_CONFIG_DIR/skills`, else `~/.claude/skills`), and
`--skill --project` into the repository's `.claude/skills` — after either, the
persona is `/<name>` in Claude Code. An existing target is refused unless you
pass `--force`.

To change a bundled persona, copy it out and back in:

```sh
aisquare persona export skeptic --to ./persona-copies
aisquare persona import ./persona-copies/skeptic --user --force
aisquare persona edit skeptic
```

### Write your own

```sh
aisquare persona new pair-programmer --project
aisquare persona edit pair-programmer
aisquare persona validate .aisquare/personas/pair-programmer
aisquare persona rm pair-programmer --project
```

`new` scaffolds a valid `SKILL.md` and opens `$EDITOR` (without one, and without
a terminal, it tells you to run `persona edit`). `edit` opens the winning
persona's `SKILL.md`; text that breaks a rule is not saved — the old file stays
exactly as it was and the rule is printed. `validate` exits 0 with warnings
(an undocumented key, a label that differs from the directory, a body over
4,000 characters) and 1 with the rule — and the line, for YAML that does not
parse — for anything that stops a directory being a persona. `rm` without a
layer flag removes the persona from the layer it resolves from.
