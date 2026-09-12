**AISquare CLI personalities: research and build plan**

The [final build plan](/Users/surjoyday_kt100/Developer/aisquare-cli/docs/plans/cli-personas-build-plan.md) is now the controlling design, including native work improvements, slash commands and local persona authoring. This document is historical supporting research; its scope, command examples and implementation priorities yield to that plan.

Latest scope clarification: the user's main route is `asq` → Start manager. The first release must therefore show a shared narration panel beside/below Manager as well as Board. Direct native terminals retain their original replies. Working-rule changes such as Ponytail are separate from the display-only feature. The simple plan documents route behavior and incoming role PRs.

Research date: September 11, 2026. Local code examined at commit 29e1c3e476d6d7bc3947a290a482485a14f856e2. This is a proposal, not an implemented feature. Commands, file additions, schema fields, estimates, and acceptance targets below are proposed unless explicitly identified as existing.

Read the [expanded implementation survey and revised build plan](/Users/surjoyday_kt100/Developer/aisquare-cli/docs/plans/cli-personas-implementation-survey.md) first. It adds source-level studies of Pi, term-pet, PeonPing/OpenPeon, SillyTavern, OpenCode, Crush, termvis, AIChat, ShellGPT, Mods, Tracery and Fluent. It supersedes this document's initial comparison of approaches: an independent narrator is a viable intermediate step between deterministic event phrases and full dialogue integration. This document retains the detailed repository map and deterministic foundation's implementation contract.

**1. Recommended product and scope**

Build downloadable, switchable presentation packs that give AISquare's manager, planner, coder, runner/tester, reviewer, and validator recognizable voices. Narrate actual assignment, claim, review, failure, blocking, and completion events in the existing terminal interface. Preserve task facts and the working agents' instructions.

The kickoff's lane 5 describes downloadable characters, restricts personality to the response layer, and sets a two-persona, real-CLI demonstration as the completion criterion. The user's extension is a different voice for each role. Two selectable packs, each with role-specific voices, reconcile those ideas. Demonstrate the same role and event under both packs so switchability is unambiguous. The source brief is [AISquare-Hackathon-No1-Kickoff.pdf](/Users/surjoyday_kt100/Downloads/AISquare-Hackathon-No1-Kickoff.pdf).

Recommended first release: two packs, an Off option, installation from a local JSON file or explicit HTTPS URL, a picker with previews, role-aware narration in the existing board, original evidence in the detail panel, and isolation tests. No new runtime library is necessary for that scope.

This release gives the CLI a personality through its workflow presentation. It does not restyle every sentence inside the native Claude terminal. Full conversational restyling is a separate, larger integration described below. That distinction should appear in the demo and feature description.

The expanded research supports adding a separately configured narrator to this foundation for richer role-specific commentary, without replacing native terminals. It consumes approved event facts and returns presentation only; it must never write narration into agent prompts, board notes or memory. Its provider integration, freshness checks and quality evaluation have a separate scope and estimate.

**2. What the repository already provides**

| Existing capability | Verified location | How the feature reuses it |
| --- | --- | --- |
| Role responsibilities and numbered-role normalization | [harness.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/core/harness.py:694) | Preserve work cycles; normalize coder1 to coder for voice lookup while retaining its actual identity. |
| Seven accepted role names, including tester/runner | [launch.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/cli/launch.py:42) | Cover current roles without creating a second orchestration system. |
| Tasks, actor IDs, events and addressed roles | [models.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/models.py:469) | Use existing facts as the source of narration. |
| Agent-to-task and spawning-actor associations | [models.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/models.py:666) | Corroborate an assignment; do not infer one from a task's suggested role. |
| Rich activity-feed rendering | [watch.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/cli/watch.py:88) | Integrate the shared persona renderer here. |
| Shared interactive BoardPanel | [board.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/cli/ui/board.py:324) | One integration reaches the fleet Board tab and standalone board view. |
| Bounded retained events and stable event IDs | [board.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/cli/ui/board.py:140) | Re-render the retained 2,000-row feed without losing state. |
| Theme modal and keyboard patterns | [theme.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/cli/ui/theme.py:25) | Reuse modal/picker conventions; keep voice selection independent of colors. |
| Typed config and atomic config persistence | [config.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/core/config.py:285) | Add one presentation section and use the existing write/error paths. |
| Real agent terminals | [terminal.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/cli/ui/terminal.py:1) | Keep terminal capture, keys, permissions and session handling intact. |

The manager already plans and coordinates; a separate planner is supported for standalone orchestration. Tester and runner share their working cycle. The reviewer examines a PR, whereas the validator gates the assembled deliverable. Persona metadata must not collapse these responsibilities or change their authority.

One subtle boundary matters: plain board text and injected agent context share rendering code in [team.py](/Users/surjoyday_kt100/Developer/aisquare-cli/src/aisquare/services/team.py:1721). Agent briefings also tell agents to read the board. Changing that shared renderer would allow personality back into reasoning. The safe first integration is the human-oriented activity-feed renderer, not the common team/context formatter.

**3. Research: persona and character projects**

The following projects supply useful patterns. Their mechanisms are not interchangeable with the brief's response-only requirement. Repository statements are identified as such; no external project's performance benchmark was reproduced.

| Project | Verified idea | Decision for AISquare |
| --- | --- | --- |
| [Ponytail](https://github.com/DietrichGebert/ponytail) | Recognizable senior-developer character, selectable modes, reuse-first working rules. | Borrow character clarity, visible selection and minimal-build discipline. Do not inject its behavioral rules into the runtime persona feature. |
| [Caveman skill](https://github.com/JuliusBrussee/caveman/blob/main/skills/caveman/SKILL.md) | Terse communication instructions, preservation of technical literals, clarity fallback. | Borrow exact-content preservation and output examples. Its suppression of progress narration conflicts with this feature. |
| [Agency Agents](https://github.com/msitarzewski/agency-agents) | Role cards combine identity, communication, mission and procedures. | Borrow distinct role presentation and discoverability. Preserve AISquare's existing role definitions. |
| [Aii Personas](https://github.com/aiiware/aii-personas) | README describes install, use, list, status and disabling; packs combine style, skills, hooks and theme. | Good pack-management UX reference. Define a smaller, data-only format. Individual manifest implementation was not audited. |
| [hersona](https://github.com/shiro-0x/hersona) | Composable persona attributes, presets, validation and exports into agent instructions. | Borrow small reusable speech traits and calibration examples. Its prompt exports do not provide isolation. |
| [PERSONA.md](https://github.com/personaxis/persona.md) | A broad persona specification includes identity, cognition, memory and other behavioral dimensions. | Useful comparison; substantially wider than presentation-only needs. Do not adopt the entire specification for this feature. |

Ponytail is the repository matching the user's example. Its [actual skill](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md) affects decisions about scope, reuse, dependencies and implementation. Its [activation hook](https://github.com/DietrichGebert/ponytail/blob/main/hooks/ponytail-activate.js) supplies instructions to the agent, and its [subagent hook](https://github.com/DietrichGebert/ponytail/blob/main/hooks/ponytail-subagent.js) extends that behavior to child agents. This is why adopting its mechanism would change the worker's reasoning context. Using existing code and standard libraries is still good engineering discipline for implementing AISquare's feature.

The official [Claude Code output-style documentation](https://code.claude.com/docs/en/output-styles) also describes an instruction-based mechanism. Styles are supplied with model requests. The keep-coding-instructions setting preserves coding guidance but does not isolate style from reasoning. Ordinary native subagents do not inherit the main conversation's style; AISquare's separately launched agents are separate sessions in any case. The documented current selector is /config; the old /output-style command has been removed. Native styles would be an easier conversational route only if the strict isolation requirement were intentionally relaxed.

License/maintenance observations: Ponytail and Agency Agents identify MIT licenses in their [respective](https://github.com/DietrichGebert/ponytail/blob/main/LICENSE) [license files](https://github.com/msitarzewski/agency-agents/blob/main/LICENSE). Caveman has [directory-specific licensing](https://github.com/JuliusBrussee/caveman/blob/main/LICENSING.md): its skill surfaces are MIT, while several runtime components are BSL-1.1. hersona documents MIT code and CC0 templates. Aii's README labels its project MIT, but direct retrieval of its individual license/manifest files was incomplete. No copied third-party persona text or runtime is required by this plan. Pin and review the exact artifact before copying code in implementation.

Ponytail's observed package manifest was 4.9.0, with recent repository activity; Agency Agents also showed recent additions and converter-test work. These are maintenance signals, not quality guarantees. Aii's visible history was small, so treat its README as design inspiration. No star count, savings percentage or author safety claim is used as a selection criterion.

**4. Research: terminal libraries and tools**

| Project | Applicable capability | Integration decision |
| --- | --- | --- |
| [Textual](https://github.com/Textualize/textual) | Python widgets, modals, reactive state, workers and headless tests. MIT. | Reuse. The project already depends on Textual 8.2.x. |
| [Rich](https://github.com/Textualize/rich) | Styled text, tables and terminal rendering. MIT. | Reuse for persona lines and plain fallback. |
| [Typer](https://github.com/fastapi/typer) | Python command groups and typed options. MIT. | Reuse for persona management commands. |
| [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | Rich input editing, completion and terminal applications. BSD-3-Clause. | Useful for a future REPL; redundant for the current picker. |
| [Bubble Tea](https://github.com/charmbracelet/bubbletea) | Go terminal framework with explicit model/update/view flow. MIT. | Borrow interaction ideas; avoid a second frontend/runtime. |
| [Lip Gloss](https://github.com/charmbracelet/lipgloss) | Go terminal styling and layout. MIT. | Visual reference; use equivalent Textual/Rich facilities. |
| [Gum](https://github.com/charmbracelet/gum) | Shell-oriented prompts, selection and styled output. MIT. | Optional scripts, not an AISquare runtime dependency. |
| [Ink](https://github.com/vadimdemedes/ink) | React/Node terminal components. MIT. | Useful design reference; adoption would introduce a frontend rewrite. |
| [Jinja](https://github.com/pallets/jinja) | General-purpose templating. | Unnecessary power for bounded event phrases; use the standard library. |
| [psygnal](https://github.com/pyapp-kit/psygnal) | Python callback/event system. | Unnecessary alongside AISquare's event store and Textual messages. |

Read-only environment inspection found Textual 8.2.8, Rich 15.0.0 and Typer 0.27.0. The repository declares Textual >=8.2,<9, Rich >=13.7 and Typer >=0.26.8. Keep that compatibility range; a UI framework upgrade is unrelated scope.

The implementation can use Textual's [reactive state](https://textual.textualize.io/guide/reactivity/) for the active selection and its [ModalScreen patterns](https://textual.textualize.io/guide/screens/) for the picker. Textual 8.2.8 has [OptionList.replace_option_prompt](https://github.com/Textualize/textual/blob/v8.2.8/src/textual/widgets/_option_list.py), allowing event rows to change in place. Use [thread workers](https://textual.textualize.io/guide/workers/) for a blocking download; return completion via messages or call_from_thread, since ordinary widget mutation is not thread-safe.

**5. Architecture and the meaning of response-only**

Recommended data flow:

    Existing prompts, role instructions, tools and orchestration
                              |
                  Existing canonical state/events
                         /               \
            Agent context, JSON       Read-only presentation adapter
            and original evidence                 |
                                       Selected persona + role voice
                                                  |
                                         Human-facing feed

There is no automatic return path from persona-rendered text to prompts, task contracts, board notes, tool results, agent stdin, memory or protocol output. The renderer cannot issue commands or change state. It should consume a snapshot of facts and return display content.

This supports a precise claim: persona selection is not included in the working agent's inputs, and switching changes only presentation. It does not promise identical independent LLM runs, identical scheduling, or identical future user actions. A human may choose a different next instruction after reading a different presentation. Tests should establish the software boundary rather than claim access to or control over internal model reasoning.

Four possible approaches:

| Approach | Effort | Isolation | Recommendation |
| --- | --- | --- | --- |
| Put character instructions into worker prompts or native output styles | Small initial change | Persona participates in model input | Does not meet the strict interpretation of the brief. |
| Render real workflow events through deterministic templates | Small-to-medium | Clear downstream boundary | Build first. Covers narration, handoffs and status presentation. |
| Run an independent narrator on approved event snapshots | Medium, requires provider and freshness work | Possible when narration has no return path into workers | Optional richer commentary using the same pack/role system; investigate with a bounded spike. |
| Obtain typed dialogue from an SDK/protocol and restyle it in a client | Large | Possible if canonical history remains separate | Future full-conversation version. |

**6. Persona identity, role voices and factual examples**

Ship two original packs, provisionally named Studio and Mission Control, with Off as the compatibility baseline. Names are working examples, not a requirement to use a particular theme.

| Role | Studio voice | Mission Control voice |
| --- | --- | --- |
| Manager | Organized coordinator | Calm mission captain |
| Planner | Structured architect | Navigator |
| Coder | Practical builder | Systems engineer |
| Runner/tester | Curious investigator | Test officer |
| Reviewer | Constructive maintainer | Inspector |
| Validator | Concise final assessor | Readiness officer |

Voice is selected by the base role; the actual role label, agent label and session identity remain visible. Manager is not normalized to planner. Runner and tester may share voice content, while keeping the label the session actually uses. Numbered seats inherit their base role voice. Unknown/custom roles use a generic pack fallback and retain their real name. Per-role pack overrides can be added after the basic global selector works; they are not needed to prove the concept.

Examples below assume actual events exist:

| Fact | Studio wording | Mission Control wording |
| --- | --- | --- |
| Coder claimed task T42 | "I've picked up T42." | "T42 is on my engineering bench." |
| Coder submitted work for checking | "My work is ready for verification." | "Engineering handoff ready for checks." |
| Tester reopened a task | "This needs another pass." | "Reopening this item for another pass." |
| Reviewer posted a result note | "Here's my review." | "Inspection report is ready." |

Render the canonical event kind, task identity and original reason/evidence beside or below these phrases. A generic result note must not become "approved". A completed task is not automatically a passing final validator gate. The pack controls expression; the application controls factual status.

**7. Event model and truthful handoffs**

Start with a small frozen presentation record around existing objects. Proposed fields: stable source ID, source kind, event time, project ID, actor session ID, actor role, actor label, task ID, target role, and original text. Optional fields are absent when unknown. For board events use the existing event ID/sequence; do not mint a new stored event merely to narrate an old one.

| Source | Safe interpretation | Unsupported inference to avoid |
| --- | --- | --- |
| task_added | Task created with a suggested role | A specific coder accepted it. |
| task_claimed | The recorded actor claimed the task | Another role assigned it. |
| task_review | Work submitted for verification | Reviewer approved it or tests passed. |
| task_reopened | Task reopened; show original reason | A specific test count or cause absent from the record. |
| task_blocked | Task blocked; show reason | Invented progress percentage or completion estimate. |
| task_done | That task marked done | Whole deliverable passed validation. |
| result/question/note | That actor reported/asked/noted the original content | A PASS/FAIL verdict guessed from arbitrary prose. |
| FleetAgent with spawned_by and task_id | Recorded actor started this agent for this task | An agent with an absent actor was started by the manager. |
| waiting/attention/exited state | Observed session state | Idle means finished, or exit zero means acceptance criteria passed. |

AISquare does not currently produce a general agent-spawn board event in the examined spawn path. An initial implementation can display assignment information from existing FleetAgent records as a UI-only activity item or agent header. Its stable identity can be derived from the agent record ID. Do not add persona notes to the shared board just to generate dialogue: those notes would reach agents. A stored spawn record proves the launch was recorded, not that the agent completed initialization or accepted a task; claims and lifecycle state provide that additional evidence.

Do not silently parse review severity, test totals or gate status out of free-form text. Preserve the raw result in the first release. A future typed event extension would require its own schema/protocol work and independent compatibility review.

**8. Pack format, selection and distribution**

Use one JSON document per pack. JSON works with existing Python/Pydantic dependencies and is simple to distribute. A proposed minimal shape is:

    {
      "schema_version": 1,
      "id": "mission-control",
      "version": "1.0.0",
      "name": "Mission Control",
      "description": "A calm crew with a voice for each role.",
      "author": "AISquare",
      "license": "MIT",
      "fallback": {"note": ["Update from $actor_label."]},
      "roles": {
        "coder": {
          "display_name": "Systems engineer",
          "phrases": {
            "task_claimed": ["$task_ref is on my engineering bench."],
            "task_review": ["Engineering handoff ready for checks."]
          }
        }
      }
    }

This abbreviated example illustrates shape; the shipped packs would cover all roles. Required fields and semantic constraints need to be specified in the implementation schema. The schema must exclude prompts, hooks, executable code, tool definitions, model selection, permission rules and file includes.

Validate strictly, reject unknown schema versions and unknown fields, bound file/template/list sizes, reject terminal control sequences, and restrict style tokens if added. Pydantic supports [strict validation](https://docs.pydantic.dev/latest/concepts/strict_mode/) and [forbidden extra fields](https://docs.pydantic.dev/latest/api/config/). Use those capabilities on the pack models rather than changing the existing app-wide config compatibility policy.

Use Python 3.11's [string.Template](https://docs.python.org/3.11/library/string.html#template-strings) with an allowlist of placeholder names. Validate templates and use substitute; safe_substitute can conceal malformed or missing placeholders. Unknown placeholders invalidate the pack. A known placeholder whose value is absent from this particular event makes that phrase ineligible: try a less specific phrase, then the generic phrase, then the existing plain renderer. Never fill a missing actor or task with invented information. Supply only strings derived from approved display fields. Avoid attribute lookups, nested evaluation, arbitrary format expressions, Jinja execution, shell expansion and recursively interpreting substituted text.

The application must always render factual status and evidence outside the customizable phrase. Schema validity cannot prove that arbitrary prose is truthful: a third-party author could write a misleading constant sentence. Curated bundled packs need editorial review and golden examples; downloaded packs need visible provenance and preview, and should never replace the canonical evidence. A checksum validates bytes against a reference, not the truth of the text or the author's identity by itself.

Proposed storage:

    src/aisquare/personas/studio.json
    src/aisquare/personas/mission-control.json
    ~/.aisquare/personas/<pack-id>/<version>/pack.json
    ~/.aisquare/personas/<pack-id>/<version>/source.json

Resolve the home through the existing AISQUARE_HOME helper; do not hardcode the default. Include bundled JSON in the built wheel and read it through [importlib.resources](https://docs.python.org/3/library/importlib.resources.html). Keep download provenance/version/checksum beside installed data. No database migration or marketplace server is needed for this release.

Constrain IDs to a bounded lowercase slug, for example [a-z][a-z0-9-]{0,47}, and first-version pack versions to three bounded numeric components such as 1.0.0. Reject separators, absolute paths, dot segments and unsupported version syntax before constructing a path. Verify the resolved destination remains inside the expected pack directory, including existing symlink components; the installer must not follow an escape or overwrite a symlink target. Include these cases in install tests. Broader version syntax can wait until it is needed.

Proposed CLI surface, not currently implemented:

    aisquare persona list
    aisquare persona show mission-control
    aisquare persona preview mission-control
    aisquare persona validate ./my-pack.json
    aisquare persona install ./my-pack.json
    aisquare persona install <https-url-to-pack.json>
    aisquare persona use mission-control
    aisquare persona use mission-control@1.0.0
    aisquare persona off
    aisquare --json persona list

Use one persistent selection in the existing config: presentation.persona = "mission-control@1.0.0", with "off" as the default and disabled representation. Begin with a global preference; add project and role overrides only if users need them. This avoids another precedence ladder. An unversioned use command succeeds when one version is available; if several are installed, list them and require an explicit version. Persist the resolved version so a later installation does not silently change the active voice. Treat identical bundled/installed ID-and-version content as the same pack; reject different content with the same identity rather than letting an installed file shadow a bundled pack.

Installation sequence: fetch explicit source with a timeout and a proposed 256 KiB byte limit; parse and validate; compute digest; stage in the destination filesystem; atomically publish; report the installed ID/version. Do not activate as a side effect of install. Reject conflicting same-ID/same-version content rather than overwriting silently. If download or validation fails, preserve the existing active pack. URL support should validate HTTPS redirects too; do not pass auth headers to an unrelated redirect target. Implement with the standard library, not shell commands. A curated catalog can later map stable IDs to versioned assets; GitHub's [release-asset API](https://docs.github.com/en/rest/releases/assets) already provides download URLs and metadata.

At startup and during ordinary rendering, installed packs work offline. Missing or damaged selections fall back to Off with one understandable notice. Do not automatically update packs or download them while a task is running.

**9. DRY implementation design**

Reuse responsibilities already established in the codebase. Add a small number of focused modules; the paths below are proposed additions, not existing files.

| Proposed addition/change | Responsibility |
| --- | --- |
| core/personas.py | Pack schema, validation and immutable pack definitions. No terminal output or agent instructions. |
| services/personas.py | List/load/install packs and update the presentation preference through existing config persistence. No argument parsing or printing. |
| cli/persona.py | Thin Typer command group, errors and machine-readable management output. |
| cli/persona_render.py | Read-only event adapter and one pure renderer used by all human narration surfaces. |
| cli/ui/personas.py | Modal picker and preview, using the shared renderer. |
| personas/*.json | Two original data-only packs. |
| core/config.py and core/paths.py | Presentation settings and pack paths using existing helpers. |
| cli/app.py | Register the management command group. |
| cli/watch.py and cli/ui/board.py | Call the renderer, re-render cached rows on selection changes, keep raw details. |
| cli/ui/app.py | Own active selection and open the picker without intercepting terminal input. |

Keep the model small: one pack model, one voice lookup, one event adapter, one renderer, and data files. Avoid classes such as ManagerPersona/CoderPersona that duplicate event logic. Avoid a plugin framework, second event bus, new database, daemon, alternate terminal framework, or generic download abstraction with only one caller.

Voice lookup order: exact role's configured phrase, normalized base role's phrase, generic pack phrase, then existing plain renderer. Shared runner/tester content is resolved in one place. Keep roles and session identity separate from persona display titles. A selected voice is never an agent executable/profile setting.

If variety is needed, choose one of a small curated set with a stable digest of pack ID/version, source event ID and event kind. Do not use Python's randomized process hash or choose fresh wording every refresh. Cache validated packs, not mutable copies of agent state. Profile only after observing a problem; the initial renderer should be ordinary bounded string substitution.

Use Rich.Text with separate text/style spans so externally authored strings remain literal. Preserve error text, commands and code in their original detail view. A visual theme and a persona are independent selections. [Rich's text API](https://rich.readthedocs.io/en/stable/text.html) already provides the rendering primitive.

**10. Interactive behavior**

The Board tab and aisquare board -w are the first surfaces. Put an obvious current-persona badge and an accessible picker there. The picker previews the same sample facts under each pack; label previews as examples, never as a live run. Arrow keys browse, Enter applies, Escape cancels. Commit the selection once, then persist it; browsing should not repeatedly write config.

Use a modal or an existing command-palette entry so the shortcut does not steal letters typed into Claude. Preserve F12's current behavior. Handle write failure explicitly: either retain the old selection, or clearly show a session-only choice; do not claim a preference was saved when it was not.

When switching, re-render retained rows by stable ID. Preserve the selected event, scroll position, event cursor, autoscroll setting and unread updates. If text-selection mode is active, keep its frozen snapshot unchanged so drag selection and copying remain stable; apply the latest persona and queued events when that mode exits. The badge can indicate the pending selection. Do not duplicate old events or reset _last_seq. A live switch should work in both the fleet Board tab and standalone board interface.

Show meaningful transitions and original details immediately. Avoid fake typing delays, repeated idle phrases, decorative percentages, or celebration while blocked. Use readable role names even in monochrome. Color can reinforce state, but it must not be its sole indicator. Keep Off independent of no-color and quiet: personality, color, and verbosity are different preferences. Machine output and noninteractive command receipts stay canonical.

A compact shared activity panel beside/below the manager pane is now part of the first release, because the user primarily stays in Manager. Preserve usable terminal space and focus; allow collapse on small terminals. It must consume the same facts and renderer as Board. Full animation, audio, avatars, a character marketplace and arbitrary per-agent combinations can follow after the basic flow is useful.

**11. Implementation sequence and reviewable deliverables**

| Phase | Concrete work | Exit criterion |
| --- | --- | --- |
| A: baseline and vertical slice | Capture representative event fixtures; add two in-memory render choices around the existing feed. | One genuine stored event renders differently in both voices; original event and hook payload are unchanged. |
| B: pack contract | Add strict models, template validation, fallback and bundled JSON; load resources from the package. | Both packs validate; invalid packs cannot replace the fallback; built wheel contains resources. |
| C: real CLI selection | Add list/show/preview/use/off, persistent versioned selection and JSON management responses. | Restart preserves selection; Off restores existing output; ordinary JSON remains stable. |
| D: live interaction | Picker, active badge, row updates, selection/scroll preservation, custom-role fallback. | Keyboard and mouse switch the live board without disrupting agent input or event delivery. |
| E: downloadable packs | Local and HTTPS install, bounds, provenance, staging and failure behavior. | A pack installs from an actual hosted file and works after disconnecting the network. |
| F: factual narration | Cover core transitions and evidence; optionally display corroborated spawn associations. | No invented actor, recipient, approval, test count or completion claim. |
| G: verification and demo | Isolation, failure, UI, packaging and repository checks; record a real workflow. | The acceptance checklist below passes with reproducible evidence. |

These phases can be combined into four reviewable changes: core/renderer and fixtures; command/config/installation; UI integration; verification/content/demo. Do not split into dozens of abstractions simply to make the changes parallel.

For two people, one can own pack models, commands and installation; the other can own UI integration and original voice copy after agreeing the record/renderer interface. Both review the isolation boundary. Event facts and rendering responsibilities must have one owner to prevent diverging semantics.

Effort estimates are engineering estimates, not measured promises. The earlier 12–20 focused-hour estimate covered the Board-based hackathon implementation with download, tests and a real demo. It does not include the newly required Manager-visible panel or reconciliation with incoming role changes. Re-estimate after that route's first working slice. A cold environment or changes to native agent dialogue add further uncertainty.

If the build window is literally two hours, reduce the attempt to two packs, core event rendering, a minimal selector, a narrow validated HTTPS JSON install and one live flow. Treat that as a stretch timebox with preparation, not a commitment that the complete plan fits. Defer catalog browsing, per-role overrides, full conversation rewriting and animation first; retain actual switchability, factual grounding and the response-only boundary.

**12. Verification: prove the boundary and the user experience**

The central test is not just that two strings look different. Freeze the same canonical events and inputs, render them under Off and both packs, and assert that factual identity/state/evidence and agent-facing data do not change.

| Test group | Required evidence |
| --- | --- |
| Pack contract | Missing/unknown fields, invalid types, unknown schema, oversized files, malformed placeholders, unknown variables, absent actor/task fields, control sequences and custom roles handled correctly. |
| Factual integrity | Same IDs, actors, role names, statuses, original text and evidence; review submission never displayed as approval; task completion never displayed as a final gate pass. |
| Isolation | Same SessionStart and prompt-delta payloads with time/IDs controlled; same role cycles, launch arguments/environment and tool-facing JSON; no persona text written to board notes. |
| Noninterference | The deterministic renderer never calls a model, shell, network client or store mutation; audit imports and use spies that record attempted calls even if errors are swallowed. An optional narrator is a separate background service with additional isolation/freshness tests from the expanded survey. |
| Native terminal | A fixed terminal frame and key sequence are unaffected by selecting a pack; use fake or isolated tmux, never the developer's active fleet. |
| Commands/config | Selection persistence, invalid IDs, Off, --json, quiet/non-TTY paths and failed atomic writes. |
| UI | Keyboard/mouse selection, Escape, focus isolation, same selected event after switching, incoming events, retained scroll/autoscroll, narrow terminal, frozen selection mode. |
| Download | Local fixture HTTP transport/mocked HTTPS paths, timeout, invalid bytes, size limit, redirect rules, path traversal and symlink escapes, conflicting versions, bad checksum reference, offline reuse. |
| Packaging | Build wheel, inspect packaged resources, install in a clean environment, exercise persona list/preview using that installation. |

Meaningful controls should show the guards can fail: deliberately route a persona marker into an injected delta in a test double and confirm the isolation test catches it; deliberately change a protected event field and confirm the factual test detects it. Restore the control immediately. These are targeted proofs of the feature's claims, not a test for every helper function.

Use existing [Typer CliRunner](https://typer.tiangolo.com/tutorial/testing/) and [Textual run_test/Pilot](https://textual.textualize.io/guide/testing/). The repository already uses those tools in its watch, shell and terminal tests. Keep tests of real rendered widget content, not only the input strings passed to widgets. Add a small set of exported SVGs for review. [pytest-textual-snapshot](https://github.com/Textualize/pytest-textual-snapshot) is optional and development-only; its compatibility must be checked before adding it, and it was not installed during this research.

Run targeted tests while implementing, then the existing make check gate and wheel/package smoke checks before delivery. Cover the repository's Python 3.11–3.13 matrix. Do not launch real model tasks in automated unit tests. Measure cold startup and a retained-feed persona switch separately if performance is uncertain; do not infer latency improvements from author marketing figures.

**13. Acceptance criteria and two-minute demo**

The first release is complete when:

- Two original persona packs can be selected and switched on the real AISquare CLI, including the same role shown in both voices.
- Every built-in role has a distinct voice; runner/tester equivalence and numbered/custom roles behave consistently.
- At least one pack is actually installed from a hosted data file, with offline use demonstrated afterward.
- Real claim, review, reopen/block and completion events retain correct actor, task, original evidence and state.
- Narration is visible while talking to the manager; the Manager panel and Board share their facts and renderer.
- Switching preserves ongoing agents, event delivery, focus, scroll and selection.
- Off and invalid-pack fallback are usable, and machine/agent-facing outputs pass isolation checks.
- The wheel contains the bundled packs and the repository's required checks pass.

Suggested recording: 0:00–0:15 state the problem and show the existing board; 0:15–0:35 select Studio and show a real task handoff; 0:35–1:00 show a failed check/reopen with the exact reason; 1:00–1:20 switch the retained events to Mission Control; 1:20–1:40 show pack installation and Off; 1:40–2:00 show the unchanged canonical evidence and explain the boundary. A real coding task may take longer than the video; identify any edit or replay honestly.

Use [VHS](https://github.com/charmbracelet/vhs) for reproducible scripted recordings if its ttyd/ffmpeg dependencies are already available. It is MIT and is a development tool, not a runtime requirement. [asciinema](https://github.com/asciinema/asciinema) can record and replay actual terminal output; its recorder identifies GPL-3.0-or-later licensing. Recording is separate from uploading. A replay proves presentation behavior for recorded events; a live run proves the feature is connected to the real CLI. Both are useful and should be labeled accurately.

**14. Later: personalities for full agent conversations**

If the requirement expands to all natural-language responses, AISquare needs typed dialogue rendering or a supported display-only hook in the native agent runtime. Pi's released Markdown transformer supplies a concrete example, covered in the expanded survey; it does not retrofit Claude's native pane. A read-only transcript companion is another intermediate investigation, subject to reliable supported transcript access. Do not parse ANSI screen captures and replace arbitrary text: redraws, code, approvals and commands make that brittle.

Two viable investigation paths are direct [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) integration or an [Agent Client Protocol client](https://agentclientprotocol.com/protocol/v1/prompt-turn). The SDK exposes text/tool blocks; ACP separates agent messages, plans, tool states and permission requests. ACP's [standard transport](https://agentclientprotocol.com/protocol/v1/transports) uses JSON-RPC over subprocess streams, so persona prose must never be injected into protocol stdout.

For ACP, the existing [claude-agent-acp adapter](https://github.com/agentclientprotocol/claude-agent-acp) is the strongest DRY candidate for translating Claude's runtime. It identifies Apache-2.0 licensing. Pin and validate a released version; some child-session features involve negotiated or draft capabilities. Choose direct SDK or ACP as the initial integration path, not both simultaneously.

The [SDK streaming guide](https://code.claude.com/docs/en/agent-sdk/streaming-output) distinguishes partial stream events from complete messages. Avoid double-rendering a block when both arrive. Current docs describe main-session token deltas and complete-message attribution for nested subagents, so do not assume uniform streaming across all agent types. Separate AISquare processes can be attributed from their known role/session identity.

An [AG-UI event model](https://docs.ag-ui.com/concepts/events) is also a useful frontend-event reference. Its [MIT-licensed repository](https://github.com/ag-ui-protocol/ag-ui) does not supply personality content or eliminate the need for an agent adapter. Adopt it only if a future browser/multi-client frontend makes a shared protocol worthwhile.

This larger project must address input, approvals, cancellation, terminal tools, attachments, authentication, provider terms, project configuration loading, session resumption, worktrees and existing hooks. Preserve canonical provider history for continuation; persona-rendered text is a separate human display artifact. SDK adoption is not automatically equivalent to the user's current interactive Claude session.

A downstream LLM rewriter of complete replies is optional, later work. It can produce richer prose but adds latency, cost, nondeterminism and factual drift. Rewriting only allowlisted natural-language blocks and preserving code/commands/numbers/negations helps, but automated checks cannot guarantee semantic equivalence for arbitrary text. Retain original output and fall back when checks fail. This is distinct from the expanded survey's independent event narrator, which can supplement the current board without owning full conversation rendering. Deterministic event templates remain the fallback.

Budget a separate compatibility spike before estimating full-dialogue delivery. Do not promise it within the event-feed MVP schedule.

**15. Findings, uncertainties and immediate next build step**

Research covered current repository code and primary upstream repositories/documentation. It did not install Ponytail or other persona runtimes, reproduce their benchmarks, alter AISquare's runtime, exercise a new feature, or prove that arbitrary downloaded text is semantically honest. Libraries were selected for fit with the current Python implementation, not popularity.

The remaining product uncertainty is whether the organizers consider workflow narration alone sufficient for their response-layer wording. The documented brief does not define that boundary precisely. The plan makes the narrower scope explicit and supplies a larger technical route if full dialogue is required; it does not claim organizer approval.

The first implementation step is one reviewable vertical slice: take a real task_claimed event, preserve its original record, render it in two original voices in the existing BoardPanel, and prove its agent-facing representation is unchanged. That validates the key architecture before investing in more persona content or distribution features.
