# Personas

A persona is *how* an agent works — a skeptic that reproduces before it
believes, a minimalist that makes the smallest change — written down once and
given to an agent when it starts. In aisquare a persona **is a Claude Code
skill**: the same directory is `--persona skeptic` to the fleet and `/skeptic`
in Claude Code, byte for byte. There is no aisquare format to learn and nothing
to convert.

This guide covers what ships today: the persona catalogue and the
`aisquare persona` commands. Spawning an agent *as* a persona, importing
something that is not already a skill, and the persona views in `asq` are
described in `docs/plans/spawn-personas.md` and land in later changes.

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
unless you pass `--force`; `--name` picks another directory name. A source that
is not a recognised skill — plain text, no frontmatter — is refused with
`not_recognised`; converting one needs the LLM import path, which is not in this
build yet.

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
