# Role personalities in AI Square CLI

Start AI Square as usual with `asq`, select your project, and start its manager.
The manager and its workers still do their normal jobs. A new **Role narration**
panel explains recorded activity in the character you choose.

Two original casts ship with the CLI: **Studio** and **Mission Control**. Each has
wording for manager, planner, coder, runner, tester, reviewer, validator and UI
tester. Unknown roles use a shared fallback. You can choose one cast for the
project and a different cast for particular roles.

## Try it

In a normal terminal, inside a registered project:

```sh
asq persona list
asq persona preview studio --role coder
asq persona use studio
asq persona use mission-control --role coder
asq persona status
asq persona off
```

In the AI Square interface, click **Persona…** under a manager or worker, or on
the Board. Alternatively, use the configured escape key (normally F12), then
F1 → **Personas**. The dialog has a picker and an **AI Square commands** box:

```text
/persona use studio
/persona use mission-control --role coder
/persona off
```

This box belongs to AI Square. Typing `/persona` directly into the embedded
Claude conversation still sends it to Claude. A direct `asq launch coder`
terminal does not gain AI Square's dialog; change settings in another terminal
and view the activity through the AI Square interface.

The task keeps running while the dialog is open. Closing the dialog returns
you to the agent without sending the persona command into its conversation.

## What changes, and what stays factual

When a coder actually claims a task, the display adds that cast's coder caption.
The underlying event still contains the real task ID, actor, time and status.
The panel labels captions as **role narration**; they are not quotations from
the worker. **Copy originals** copies the underlying records.

Persona settings affect this display only. They do not enter worker prompts,
task notes, command results, evidence, machine-readable JSON or Explainability
events. They do not rewrite Claude's original terminal replies. Changing a
persona cannot make a failed test pass or make a reviewer skip checks.

Custom prose can still be misleading, so it never replaces the official event.
Preview includes a failure example. Turning personalities off retains the
original records and all working improvements.

## Make your own character

Export a starting pack, edit its message patterns, then import your own version.
A pack has a `generic` section and an optional per-role section (`manager`,
`planner`, `coder`, `runner`, `tester`, `reviewer`, `validator`, `ui-tester`;
use the base role name, never a numbered seat such as `coder2`). Patterns are
keyed by the event kinds the team board actually records:

```text
note  result  question  attention  signal  activate  focus
task_added  task_claimed  task_released  task_review  task_reopened
task_done  task_blocked  task_dropped  agent_exited
brief_created  brief_updated  brief_linked  brief_evidence  default
```

A kind with no pattern falls back to `default`. Placeholders are limited to
`{role}`, `{task_id}`, `{session_id}` and `{event_kind}`; those values come from
the board and are flattened to one plain line before display. Whitespace-only
patterns and unknown kinds are rejected on import.

```sh
asq persona export studio --output ./my-team.json
# Edit id/name/version and the role messages in my-team.json.
asq persona add ./my-team.json
asq persona preview MY_PACK_ID --role reviewer
asq persona use MY_PACK_ID --role reviewer
```

You can also begin with a description:

```sh
asq persona add --name calm-dev --text "Calm, friendly, short updates"
asq persona add --name calm-dev --text-file ./character.txt
asq persona edit calm-dev
```

In a terminal, these open your local editor (`VISUAL` or `EDITOR`, with a local
fallback). In the AI Square dialog they open a JSON text editor. The description
starts a draft based on Studio; **you edit the actual phrases**. AI Square does
not claim a description automatically creates a new voice. Non-interactive
scripts must import ready JSON instead. There are no model calls for authoring,
switching or rendering.

For a pack someone has published, use `asq persona add --url HTTPS_URL`. Downloads
are bounded, validated and copied into AI Square's local storage with a checksum
and origin. Installed packs work offline and do not auto-update. The pack is
data: it cannot install code, change permissions or add agent instructions.

Different contents cannot overwrite an existing ID/version. Editing creates a
new version. Bundled files are not modified. Invalid files or failed downloads
leave the active choice intact, and a document that is not a pack is reported by
the fields that failed, never echoed into your terminal.

`asq persona remove ID@version` removes one version; `asq persona remove ID`
removes every installed version of that character. Any project or role choice
that pointed at a removed version becomes Off.

Exit codes: a malformed command line (missing pack, unknown flag, `--global`
combined with `--project`) exits 2; a real failure such as an unknown pack, a bad
file or a refused download exits 1. Under `--json` the payload carries
`"error": "usage"` or `"error": "persona_error"` with the reason in `detail`.

## Scope and persistence

Project selections survive restart. `--project PROJECT` selects another
registered project; `--global` sets the fallback for projects without a choice.
The interface uses the project you are viewing. Shell commands require a
registered project or an explicit scope.

```sh
asq persona use studio --global
asq persona off --role reviewer
asq persona reset --role coder
asq persona use mission-control --reset-roles
```

Resolution is: project Off → role override → project default → global default →
Off. Numbered seats such as `coder2` inherit `coder`; a declared role that is not
one of the eight (say `bot7`) keeps its own name and uses the generic patterns.
A role choice saved while the project is Off stays inactive until a project-wide
`use` turns narration on. Switching the project cast preserves deliberate role
overrides unless you ask to reset them. Open views refresh after changes from
another shell.

Choices live in `~/.aisquare/personas/selections.json`. If that file is ever
damaged, `status` and `use` name it and refuse; `asq persona off` or
`asq persona reset` rewrites it from empty choices so you are never locked out.

These are AI Square's own packs and code. No Ponytail, Spec Kit or RTK installation
is required. The separate [native workflow](native-workflow.md) changes how work
is prepared and checked; [command reports](command-reports.md) shorten supported
tool results.
