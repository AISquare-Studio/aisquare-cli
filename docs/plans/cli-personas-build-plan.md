# AI Square CLI — final build plan

Status: implemented on `codex/native-personas-workflow` and stress-tested on September 11, 2026: the WHOLE suite (not only the feature tests) passes, an adversarial multi-agent review produced 87 candidate findings of which 54 were independently confirmed and then fixed with regression tests (see `tests/test_native_hardening.py`, `tests/test_persona_isolation.py`, `tests/test_source_revision_hardening.py`, `tests/test_team_fleet_harness_assignment.py`), and the clean-wheel end-to-end check in [native end-to-end checks](../native-end-to-end.md) was repeated. Added September 12, 2026: the opt-in `persona voice on` so the agents themselves speak in the chosen cast (see docs/personas.md). Still NOT done, deliberately: required-verification item 10 (baseline comparison runs with measured usage), the optional AI character generator, automatic command interception, and restyling Claude's replies in sessions that are already running. No performance or token-saving claim is established. Use the shipped [persona controls](../personas.md), [native workflow](../native-workflow.md), and [command reports](../command-reports.md) guides for current commands and limits.

This is the controlling product and build plan. It supersedes implementation choices and command examples in the earlier [research](/Users/surjoyday_kt100/Developer/aisquare-cli/docs/plans/cli-personas-research.md) and [implementation survey](/Users/surjoyday_kt100/Developer/aisquare-cli/docs/plans/cli-personas-implementation-survey.md). Those remain supporting research, not additional plans to combine with this one.

## 1. The product

Make AI Square a team that is easier to understand and better prepared to do useful work. The person keeps the normal route: open `asq`, select a project, start its manager, and describe the job.

| Feature | What changes | What the user gets |
| --- | --- | --- |
| Personalities | Human-facing wording about recorded work | Distinct role voices, two starter packs, local creation, downloads and switching. |
| Working rules and shared requirements | Instructions and information given to agents | Clear expectations, suitable reuse, focused assignments, better handoffs and evidence before completion. |
| Short command reports | Selected command results given to agents | Less unnecessary reading, with failures and access to original results preserved. |

These have separate settings. The kickoff condition, “personality shapes only the response layer and never the reasoning,” applies to the first feature. The other two intentionally change worker inputs. A persona command never changes work rules or command-report settings.

We own the system inside AI Square. Users do not need Ponytail, Spec Kit or RTK plugins. Selected tools can run underneath our implementation. A skill supplies instructions; a browser or container tool supplies an actual capability.

## 2. One example from beginning to end

This is an illustrative application scenario. The reproducible end-to-end check uses a minimal Python login example; it does not claim to have tested this imaginary website. Assume a website already has a working sign-in service.

The user says: “Add a login page. Correct details should open the dashboard. Wrong details should show an error. It must work on a phone.”

1. The manager records three requirements: successful login, visible error, usable phone layout. It inspects the project and asks only for important answers still missing.
2. It creates linked tasks on the existing board. The coder receives its actual task, relevant requirements, code locations and working rules.
3. The coder reuses the sign-in service and submits changes for checking.
4. Runner/tester executes the agreed checks. UI tester checks the changed application in a real browser, including the phone view.
5. Suppose the error is clipped on a phone. That requirement remains unmet. The board records the finding, evidence and a specific correction or reopening.
6. The coder fixes it. Relevant checks run again against the changed build. Reviewer examines the proposed changes; validator checks requirements and evidence.
7. Manager reports what is ready and what remains unresolved. Passing checks does not automatically authorise merging or deployment.

The same recorded coder claim could appear as:

| Studio | Mission Control |
| --- | --- |
| Coder: “I've picked up this task.” | Coder: “This task is on my engineering bench.” |

Actual task ID, identity, status and evidence stay unchanged. Label this role narration, not a quotation from the original conversation. “Submitted for checking” must not become “approved.”

Not every task needs every role. The manager already plans; a separate planner is optional. Preserve existing runner/tester responsibilities. Docker is used only when the project's checks require it.

## 3. Where personality appears

Add one shared activity component to the Manager view, individual worker views, Board, and human-oriented live activity output. In Manager and worker views, put it beside/below the terminal. Show the active persona, real status, narration, and original-record inspection/copy controls. Make it collapsible on small screens without taking typing focus.

The original Claude terminal retains its original replies. Restyling every assistant response is later work requiring actual message data and a display we control, or a supported host display extension. Do not rewrite terminal screen bytes or advertise the activity panel as changing every reply.

## 4. Commands and where to type them

Use one family: `persona add`, not a separate `persona-add` command.

- **Normal terminal:** `asq persona ...`.
- **AI Square interface:** click Persona in Manager or a worker view, or use the configured escape key, normally F12, then F1 → Personas. This opens a labelled **AI Square commands** box with `/persona` completion.

The task continues while the box is open. Closing it restores focus and preserves the agent's draft. Persona commands execute locally and never enter the working agent's conversation.

Currently, `/persona` typed into the embedded Claude chat would go to Claude. This release does not intercept it. A future host-specific shortcut must pass the same isolation checks before we advertise that route.

Shell, slash box and picker share the same parsed actions and services. Slash input is parsed locally; it is never evaluated as a shell command.

### Shell commands

These commands are implemented. The optional AI generation action described later is not included.

```text
asq persona list
asq persona status
asq persona preview studio
asq persona use mission-control
asq persona use calm-dev --role coder
asq persona off
asq persona off --role reviewer
asq persona reset --role coder

asq persona add ./calm-dev.json
asq persona add --url https://example.org/personas/calm-dev.json
asq persona add --name calm-dev --text "Calm, friendly developer; brief updates and gentle humour."
asq persona add --name calm-dev --text-file ./character.txt
asq persona edit calm-dev
asq persona export calm-dev --output ./calm-dev.json
asq persona remove calm-dev
```

The download address is illustrative, not a working pack URL.

### Equivalent commands in the AI Square box

```text
/persona
/persona use mission-control
/persona use calm-dev --role coder
/persona add ./calm-dev.json
/persona add --name calm-dev --text "Calm, friendly developer; brief updates."
/persona off
```

The AI Square command box is part of the picker; bare `/persona` shows its status. `status` explains effective selections and scope. `add` imports/creates a pack without activating it. `use` activates a saved pack. Import failures leave the current pack intact. File paths and pasted JSON are supported; a native operating-system file chooser is not included.

### Local files versus plain descriptions

A **ready JSON file** contains message patterns. Import, validate and preview it locally without a model call.

A **plain description** expresses the desired character but does not contain every event message. `--text` and `--text-file` open an authoring screen based on a starter pack. Users edit sample wording locally; unchanged starter wording is clearly identified. Saving a description alone must not be presented as automatically generating a new voice.

**Generate from description** is an optional action in that screen. One separate AI request drafts patterns; the user previews and saves them. The saved pack then works locally without a model call for each update. No provider configured means manual creation/import still works. Non-interactive use requires a ready pack; return a usage error rather than wait for an editor.

For generation, show the configured provider receiving the description. Send only that text, schema and synthetic examples. Supply no tools, repository files, working-agent session or task history. Authentication is separately configured; credentials are not included in the prompt. Treat generated output as untrusted pack data and validate it. The original description never enters worker instructions. Selecting Generate initiates the request; importing a text file does not silently make one.

### Scope, switching and persistence

Default scope is the selected project in the interface, or the registered project containing the shell's working directory. `--project PROJECT` selects an explicit project. If neither resolves, require an explicit project or `--global` rather than guess.

- `use PACK` changes the project default and enables its personality display.
- `use PACK --role coder` saves a choice for coders within that project, including numbered coder seats. Keep actual identities visible. If the project is explicitly Off, keep it Off and say the choice is saved but inactive; only project-wide `use PACK` re-enables the display.
- `off` disables all personality narration for the project, retaining choices for later. Work continues.
- `off --role reviewer` gives that role plain output.
- `reset --role coder` removes that override and restores inheritance.
- `use PACK --global` sets the fallback for projects without an explicit default. Project choices win. Global role overrides are outside the first interface.
- `use PACK` preserves deliberate role overrides and lists them in its receipt. The picker offers an explicit Reset role overrides action.

Resolution: project-wide Off gate → project role override → project default → global default → Off. A role override can explicitly be Off. Within the selected pack, try the base role's event pattern, then the generic event pattern, then plain text. New workers inherit project/role settings. Persistent individual-agent overrides are later work; per-role choices satisfy the initial requirement.

Selections and local packs survive restart. Changes from another shell refresh open views. Redraw visible narration from unchanged records; do not choose random new phrases on every refresh. Keep scrolling, selection and active copying stable. Canonical task exports and JSON remain unstyled.

A direct `asq launch coder` session has no persistent AI Square command box. Another shell can change its project settings, and AI Square's board can display its events. Its original terminal does not automatically gain a slash command or restyled replies.

## 5. Pack design

Ship two original packs: Studio and Mission Control. Each covers manager, planner, coder, runner, tester, reviewer, validator and ui-tester, with a fallback for future roles. Two selectable casts satisfy the two-persona proposal while allowing different voices per role.

A pack contains schema version, ID/version, name, description, author/licence metadata, generic patterns, and optional patterns per role/event. Provide a few variations with a small allowlist of factual placeholders.

It contains no executable hooks, tools, permissions, model selection, shell commands, work policies or working-agent instructions. Use existing Python/Pydantic facilities and a bounded standard-library template formatter.

Validate sizes, IDs, versions and placeholders; reject unsupported fields, terminal control sequences and path escapes. Treat downloaded text as text, not executable markup. Missing facts cause a simpler phrase or plain fallback. The application displays actual task IDs, statuses, errors and evidence outside custom prose.

Schema validation cannot establish that arbitrary prose is truthful. Review bundled wording and preview custom packs against failures as well as successes. A custom caption never replaces the official result.

Use existing AI Square home/path helpers, for example `~/.aisquare/personas/<id>/<version>/pack.json`. Import a local copy; editing creates a new version rather than changing the shipped package. Record download provenance and checksum. Bound downloads by time/size and install atomically after validation. Reject path/symlink escapes. Installed packs work offline and do not auto-update during work.

Different content cannot overwrite an existing ID/version. Show conflicts and require a new identity/version. Removing a selected pack must explicitly resolve affected selections to plain output; do not leave dangling references. Raw output remains available even if a pack is missing or damaged.

## 6. Shared architecture and the response-only boundary

```text
User request
    ↓
Shared requirements + existing board
    ↓
Manager starts needed workers
    ↓
Workers receive tasks + applicable working rules
    ↓
Tools run; supported results may become short reports
    ↓
Original records, results and evidence
    ├──→ Other agents/validator receive factual information
    └──→ Human display selects role voice → personality narration
```

Persona selection has no automatic path back into worker prompts, launch arguments, task notes, tool results, inter-agent messages, memory or captured agent context, with ONE deliberate, operator-chosen exception added on September 12, 2026: `asq persona voice on` appends the selected pack's per-role speaking instruction to NEW agents' system prompts through Claude Code's `--append-system-prompt`. It is off by default, project-scoped, framed to govern only the wording of replies to the human, and covered by tests that show the start-up briefing and every board record stay byte-identical with it on. Do not modify shared board-summary formatters also used in worker briefings. Keep narration outside captured native terminals so hooks cannot feed it back to agents.

One pack loader, selection resolver, event adapter and renderer serve all roles/views. Shell, slash box and picker call shared services. We do not build separate managers or task boards for these improvements.

## 7. Native working improvements

### Shared expectations, inspired by Spec Kit

Add compact work briefs with stable requirement IDs, user corrections, assumptions, boundaries and expected checks. Link existing tasks and dependencies to them. Store structured records through the existing store; a readable document can be exported. Avoid a second independently editable task list.

Check for missing task coverage and contradictory instructions. Send workers the relevant portion with access to the full brief when needed. New user instructions update the brief and affected tasks; old documents never override corrections.

Validator checks requirements against real evidence. Findings reopen/create linked tasks with duplicate detection. Repeated inability to progress produces a clear blocked result rather than endless new tasks. Small edits get short briefs.

### Role-specific habits, inspired by Ponytail and Superpowers

| Role | Built-in working habits |
| --- | --- |
| Manager/planner | Clear expectations, focused tasks, no duplicate assignments or unnecessary workers, actual task handoff. |
| Coder | Inspect the affected flow; reuse suitable code; add necessary dependencies only; make a small correct maintainable change. |
| Runner/tester | Execute required checks, retain failure detail, report what was and was not tested. |
| UI tester | Check the changed build in a real browser, requested device sizes and error cases; report missing tools honestly. |
| Reviewer | Examine relevant changes and give specific evidence-backed findings. |
| Validator | Compare requested outcomes with fresh evidence; an agent saying done is insufficient. |

Load rules through the common session-start briefing path for manager-spawned workers and direct launches. Record each session's rule version. Changed rules apply to new sessions; do not pretend old instructions disappear or automatically restart active agents.

Keep existing working mode available for compatibility/comparison. Personality Off does not change it. Do not weaken existing testing obligations by copying another project's defaults.

### Shorter input, inspired by RTK and Aider

Start with a native wrapper, provisionally `asq exec -- COMMAND ...`, supporting a small tested set: Git status and pytest output. Execute the exact command once, preserve original stdout/stderr and exit status, and compact only recognised output. Do not silently add flags that change which tests run.

Retain relevant failure details, actionable warnings, check counts and a reference to saved output. Retrieval reads that saved result instead of rerunning a potentially mutating command. Document retention/size limits and mark truncation. Unknown formats use ordinary output. Preserve signals, exit codes and permission checks.

Initially, worker instructions explicitly use the wrapper for supported commands. Automatic interception needs a verified integration with the underlying agent. Installing a binary or shortening the outer display is insufficient. Native Read/Grep/Glob tools and machine-readable `aisquare --json` flows are outside initial coverage.

Extend the existing Repomix snapshot/index with task-relevant selection and cache invalidation. Use maps to find files, then read necessary originals. Do not create a second index or treat compressed maps as review evidence.

Optional RTK support can later use the same reporting/recovery contract. Prevent double filtering. Matching RTK's full command coverage is outside the first release.

## 8. Build versus reuse

| Reference/component | Decision |
| --- | --- |
| Ponytail | Author AI Square's own short reuse-first rules; no plugin dependency. |
| Spec Kit | Adapt requirements, coverage and remaining-work checks into the existing board; no Specify installation required. |
| Superpowers | Adopt selected debugging/evidence habits without adding a second coordination workflow. |
| RTK | Reference compaction/recovery; limited native implementation first; optional integration later. |
| Aider | Reference focused code maps; improve existing context selection. |
| Repomix | Reuse the existing integration. |
| Playwright/browser tools | Reuse supported browser capabilities; detect/setup when needed. |
| Docker/container tools | Use the project's setup where required; not compulsory for every runner task. |
| Textual, Rich, Typer, Pydantic | Reuse existing dependencies for screens, commands, rendering and validation. |

Pin/test reused versions and retain required attribution when copying/distributing code. One AI Square experience can use specialised tools underneath.

## 9. Build order and completion gates

| Stage | Build | Completion evidence |
| --- | --- | --- |
| 0. Baseline | Select target version; reconcile installed/check-out differences and incoming role/task-handoff changes. | A real create → launch → intended claim → review → check → result run, with version recorded. |
| 1. Personality | Typed display records, two packs, shared renderer, Manager/worker/Board activity. | Same real events in two voices and Off; failures and incomplete work retain their meanings. |
| 2. Controls | Shell family, Persona button, AI Square slash box, project/role selection, live switching. | Switch without restart, lost input, or persona commands reaching workers. |
| 3. Creation/distribution | Local import, editor/text-authoring, export, downloads and persistence. | Import/edit, assign to a role, restart, use offline, and reject bad packs without losing the active choice. |
| 4. Working improvements | Shared requirements, task links, versioned briefings, evidence and final coverage check. | An unmet requirement becomes a precise correction; new user instructions update the brief. |
| 5. Efficiency | Limited command wrapper, original recovery, focused code context and usage measurement. | Agent receives compact results and can retrieve originals; failure/exit information survives; unknown cases fall back. |
| 6. Release | Packaging, upgrade behaviour, route documentation and reproducible comparisons. | Clean installation works; required checks pass; demo/report separates measured results from estimates. |

Optional one-time AI authoring comes after the local editor works in Stage 3. Continuous AI narration and complete native-response restyling are later projects. Locally imported packs need neither.

For the hackathon, finish Stages 0–3 first: two downloadable switchable personas on the real CLI, per-role voices and local creation. Before declaring that milestone complete, build the package, install it in a clean supported environment, verify both bundled packs are present, and run a real download/import/switch smoke test. Stage 6 broadens compatibility and performance verification; it does not defer basic installation checks. Stages 4–6 deliver the broader productivity proposal. Do not present a personality-only demo as completion of the expanded system.

Earlier 12–20-hour estimates covered a smaller Board-only prototype. They no longer apply. Estimate each stage after the baseline is confirmed; interception, generation and full-reply display are separate effort drivers. Do not promise total token savings before comparison runs.

## 10. Developer implementation map

“New” means proposed, not present. Match repository conventions during implementation.

| Area | Location |
| --- | --- |
| Pack schema/display records | `src/aisquare/core/personas.py` — new |
| Starter packs | `src/aisquare/personas/*.json` — new |
| Install/version/selection services | `src/aisquare/services/personas.py` — new |
| Optional one-time generator | `src/aisquare/services/persona_authoring.py` — new, separate no-tools client |
| Shared actions/shell commands | `src/aisquare/cli/persona.py` — new; register in `cli/app.py` |
| Human-only renderer | `src/aisquare/cli/persona_render.py` — new |
| Picker/editor/command box/activity | New `cli/ui/personas.py` and `persona_activity.py` |
| Existing views | `cli/ui/views/project.py`, `views/agent.py`, `cli/ui/board.py`, `cli/watch.py` |
| Persistence/refresh | Existing `core/config.py`, path helpers and UI refresh paths |
| Working rules | Extend `core/harness.py` and shared session briefing in `services/team.py` |
| Requirements/evidence | Additive existing-store/model migrations; new `services/work_briefs.py` |
| Command execution/formats/recovery | New `services/command_reports.py`, small formatters and CLI wrapper |
| Code-context selection | Extend existing `core/snapshot.py` and `services/hooks.py` |

Keep persona imports out of agent-context formatting and working-rule loading. Reuse event persistence. Add factual fields only when needed for a real actor/task relationship, not to store narration.

## 11. Required verification

1. Replay identical events under both packs and Off. Compare worker launch data, injected context, task records, inter-agent messages and tool results: persona choice must not affect them.
2. Keep launch, claim, submitted, task done and final validation distinct. Worker exit or empty output does not establish success.
3. Exercise shell/slash/picker actions through shared services. No persona input enters the terminal. Normal keys/paste still work. Cover paths with spaces, quoting, cancellation and scope receipts.
4. Check role inheritance, numbered seats, ui-tester, unknown roles, project Off/reset and restart persistence. Two open views observe changes without losing input.
5. Check invalid packs/placeholders, terminal controls, path/symlink escapes, size limits, version conflicts, interrupted writes and missing packs. Built wheels contain starter data.
6. Verify offline authoring/import/export. Text descriptions do not become worker prompts. Optional generation cannot access tools or active sessions.
7. Verify intended task/rules reach manager-spawned and direct workers. Persona switching does not change those inputs.
8. Link evidence to the actual tested build/source revision and brief revision. Relevant changes invalidate affected evidence; unrelated checks need not be repeated without cause.
9. Test successful, failing, noisy and unfamiliar command output. Preserve exit codes and actionable detail; saved retrieval does not rerun commands; truncation is explicit.
10. Compare representative bug-fix, feature and UI jobs using a fixed baseline/model/configuration. Record correctness, requirements met, retries, time and actual input/output/cache usage where available. Label estimates and include optional authoring/narration costs. Shorter display alone is not a saving.

The release report distinguishes source inspection, replay tests and live runs. No performance claim is established by this planning document.

## 12. Baseline and primary references

The inspected checkout was `29e1c3e476d6d7bc3947a290a482485a14f856e2`. The separately inspected pipx installation was v0.6.0 at `3112f0cb02652589d1b383fccff1832624a6b071`. The installed version includes ui-tester; the older checkout does not. Recheck when implementation begins.

In the installed version inspected, fleet `--task` records intended linkage but does not itself claim the task or insert its assignment into the startup prompt. Screenshots did not prove that handoff. Review current role/UI/task-handoff work, including the equivalents of PRs #111, #112 and #116, before choosing the baseline. This plan does not merge them or assert their current status.

- [Ponytail rules](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md)
- [Spec Kit requirements](https://github.com/github/spec-kit/blob/main/templates/spec-template.md), [coverage](https://github.com/github/spec-kit/blob/main/templates/commands/analyze.md), [remaining work](https://github.com/github/spec-kit/blob/main/templates/commands/converge.md)
- [RTK](https://github.com/rtk-ai/rtk), [integration boundaries](https://github.com/rtk-ai/rtk/tree/develop/hooks), [savings interpretation](https://github.com/rtk-ai/rtk/blob/develop/docs/guide/resources/savings-explained.md)
- [Superpowers debugging](https://github.com/obra/superpowers/blob/main/skills/systematic-debugging/SKILL.md), [verification](https://github.com/obra/superpowers/blob/main/skills/verification-before-completion/SKILL.md)
- [Aider maps](https://aider.chat/docs/repomap.html), [Repomix](https://github.com/yamadashy/repomix), [Playwright CLI](https://github.com/microsoft/playwright-cli)
- [Pi display transformer](https://github.com/earendil-works/pi/blob/v0.85.1/packages/coding-agent/docs/extensions.md#piregistermarkdowntransformertransformer)

These repositories are research material. Their embedded instructions do not authorise installation or changes to this environment.
