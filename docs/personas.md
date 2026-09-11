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

Export a starting pack, edit its message patterns, then import your own version:

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
leave the active choice intact.

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
Off. Numbered seats such as `coder2` inherit `coder`. A role choice saved while
the project is Off stays inactive until a project-wide `use` turns narration on.
Switching the project cast preserves deliberate role overrides unless you ask
to reset them. Open views refresh after changes from another shell.

These are AI Square's own packs and code. No Ponytail, Spec Kit or RTK installation
is required. The separate [native workflow](native-workflow.md) changes how work
is prepared and checked; [command reports](command-reports.md) shorten supported
tool results.
