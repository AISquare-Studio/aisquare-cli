# rc/hackathon-v1 — what is folded in, and why it matters

> Prepared 2026-09-15 by the fleet manager for the owner's review of the
> `rc/hackathon-v1` → `main` pull request. One row per feature. Every row was
> built on the fleet overnight, verified by the runner against its plan
> acceptance (`docs/plans/spawn-personas.md` §7) in its own worktree, reviewed
> read-only by the manager and against the workspace 9-dimension framework,
> and gated by CI. The validator's gate on the assembled head was
> **PASS-WITH-FIXES**, and every finding was closed on the branch. Decisions,
> timeline and evidence: `docs/plans/hackathon-overnight-log.md`. Demo:
> `docs/plans/hackathon-demo.md`.

## Features

| Feature | Impact | Outcome | How | PRs |
| --- | --- | --- | --- | --- |
| **A persona is a Claude Code skill** — `<name>/SKILL.md`, three layers (bundled · user · project) | Interchange for free: any skill someone already likes is a persona the moment it is imported, and any persona is `/name` in Claude Code the moment it is exported. No new format to learn. | `aisquare persona list\|show\|new\|edit\|rm\|validate\|import\|export`; four bundled starters (skeptic, mentor, minimalist, careful); project personas travel with the repo under `.aisquare/personas/`. | `core/personas.py` parses the frontmatter with PyYAML (`safe_load`, lazily imported), preserves bytes, keeps provenance in a `.persona.json` sidecar; `services/personas.py` owns every write; `export --skill` copies into Claude's skill directories. | P1 #181 · P16 #195 |
| **Spawn an agent *as* a persona** — `fleet spawn --persona`, `launch --persona`, `[fleet.roles.<role>].persona` | The operator chooses how an agent works, per spawn or per role, and sees it on the board. | The persona's body is injected once at session start, right after the role's cycle and lane rule, with a fixed guard sentence; without a persona the injected bytes are identical to before. `fleet ls`, the board row and the sidebar show `· <persona>`. | `AISQUARE_PERSONA` exported by `launch`; the SessionStart hook records it on the session row and renders the block; store v15 adds `team_session.persona` and `fleet_agent.persona`; precedence flag > config > none. | P2 #183 |
| **Attach a persona to a *running* agent** — `persona attach <name> --to <label>` | No restart needed to change how an agent works; the change survives `/clear`. | The briefing is delivered now through the existing `fleet tell` (typed to a waiting agent, filed as a note for a busy one) and recorded on the agent's row; the hook falls back to the row when the env var is absent. | `services.fleet.attach_persona`; hook resolution env > fleet row > session row; one board event. Since P20 the fleet row's persona (the latest recorded intent) beats `AISQUARE_PERSONA`, so an attach survives `/clear` even for an agent spawned `--persona`; an unreadable row fails open with a logged warning and one briefing line naming only the exception class. | P8 #186, P20 #197 |
| **Smart import** — recognised skills copy verbatim; anything else is turned into a skill by an LLM | "Import takes anything": a plain prompt, a JSON/YAML persona from another tool, a Cursor rule, a URL. | A structured draft (name, description, ≤4,000-char body, notes) is validated like any skill, shown, and confirmed before it is saved; drafts are kept on refusal; provenance names the engine and model. | Engine ladder `manager` (headless `claude -p` under the manager role's binding, probe-style isolation, no `--bare` so OAuth accounts pay) → `api` (official `anthropic` SDK, optional extra `aisquare-cli[llm]`, structured output) → refuse with both fixes; `[persona.import]` config; one registered spawn seam; nothing imports the engines on a hook path. | P5 #185 · P15 #193 |
| **The Spawn dialog** — `＋ spawn agent` opens a real form | The fleet UI can spawn without the terminal (Phase 7 of the fleet plan). | Role, label (prefilled, 🎲), task, worktree, permission mode, account, binary, extra args, first prompt; refusals stay in the dialog; success opens the agent's live pane. | `cli/ui/spawn.py` over the unchanged `fleet_service.spawn` signature; thread worker; presets applied at compose. | P3 #180 (merged) · P4 #187 |
| **Persona-first UI** — Personas tab, target picker, two-step attach | The owner's flow: pick a persona, then pick who runs it — a running agent, a bound teammate, or an account — in two steps; new binds and accounts created in place. | Personas tab (search, catalogue, preview of exactly what will be injected, Import with progress + confirm-draft modal, in-UI editor, Export, Remove); the picker lists Agents · Binds · Accounts ordered by intent and never acts itself; a bind/account opens the Spawn dialog preset; **Pick…** in the dialog is the same picker. | `cli/ui/views/personas_tab.py`, `cli/ui/persona_dialogs.py`, `cli/ui/attach.py`; `+ New bind` saves through `services.settings.bind_role`; Settings gets a per-role default persona; sidebar badge. | P6 #184 · P7 #188 |
| **Demo runbook** | The owner can trust the demo before the deploy. | Twelve CLI steps with real pasted output (extracted and replayed verbatim by the runner), nine `asq` steps each tied to a passing headless test, known limits stated. | `docs/plans/hackathon-demo.md`; CHANGELOG's forward-looking sentences rewritten to what merged. | P14 #194 |

## Hygiene folded in

| Fix | Why it is in this train | PR |
| --- | --- | --- |
| Doc-guard sweep skips the fleet's worktrees | `make check` from a root checkout that hosts agent worktrees used to fail on copies of the docs. | P9 #182 |
| tmux dead-status flake polls for the status | One predicate; the runner verified the mechanism, not just today's green. | P10 #190 |
| The five install-script tests honour a WSL2 host | This machine's gate is green for the first time (0 failed); CI still executes all 87 tests in the file. | P11 #189 |
| `probe_model` treats a non-object reply as inconclusive | `aisquare team spawn` crashed with a traceback on a not-logged-in account; now an honest "not logged in" reason. | P12 #191 |
| Public `services.fleet.role_ok` | The seat rule the UI relies on has a public name (a wrapper, so it cannot drift). | P13 #192 |
| `import --list` decides "imported" by provenance | A never-imported skill sharing a bundled name showed `imported`; validator finding, checked against the real collision on the owner's machine. | P16 #195 |

## What the reviewer needs to know

- **Dependency added:** `pyyaml>=6.0` (core; imported lazily inside the parser) and `types-PyYAML` (dev). Optional extra `llm` = `anthropic>=1`. Nothing else new at import time: `python -X importtime -c "import aisquare.cli.app"` shows no `yaml`, `anthropic` or `persona_import`.
- **Store migration:** v14 → v15 adds two nullable columns (`team_session.persona`, `fleet_agent.persona`), following `docs/store-migration-race.md`. PR #144's unmerged stack numbers its own migrations v15–v18 on its branch; whichever lands second renumbers (the v13/v14 precedent in `core/store.py`).
- **Deliberately different from the persona layer removed in PR #136** (narration panel, voice/style packs): this is a spawn-time operating prompt delivered once, recorded on the board, never per turn.
- **One channel:** the persona rides the same session-start briefing the role cycle uses; no `--append-system-prompt`, no per-turn injection.
- **LLM import cannot be exercised in tests** (no live engines); its argv, env stripping, seam and outcomes are pinned by fakes, and the validator read the code paths.
- **Merges into rc were performed under the owner's overnight authorisation** with a merge gate of runner verdict (fresh worktree, own venv, evidence on the board) + peer review by the other coder against the workspace 9-dimension framework (verdict + comment id cited in every merge commit body) + CI green on the merged head + the manager's read-only review. A reopened PR merged only after its original reviewer confirmed the 🔴s closed on the fixed head and the runner re-verified. Every merge is a merge commit; nothing was squashed or force-pushed. The full sequence is in `docs/plans/hackathon-overnight-log.md`.
- **Known follow-up:** P17 switches the picker's seat check to the public `role_ok` (one line, `cli/ui/attach.py`); its PR opens the moment #188 lands in this branch (#192, which it also needs, is in).
- **Not touched:** `main`, CI configuration, servers, accounts, the tmux server.
