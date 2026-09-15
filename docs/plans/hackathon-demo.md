# Hackathon demo runbook — personas on the fleet

> **For the morning.** The exact demo path on the assembled `rc/hackathon-v1` train, in two
> tracks: **A** on the command line (every command below was run and its output is pasted
> verbatim) and **B** in `asq` (each screen described, each step named with the headless test
> that proves it). Written against #188's head (`74ce36f` — P7 on top of P8, P5, P2, P1, P4 and
> P6), so every command exists in that tree; after the morning merges it is the same on
> `rc/hackathon-v1`. Track A takes about five minutes and needs `tmux` (3.2+) but no Claude
> Code session. Transcript of the pass these outputs come from: `/tmp/p3dbg/p14-pass-build.txt`.

## Track A — the command line

Run the steps in order, in one shell, with the merged `aisquare` on your `PATH`. Lines that
depend on the moment (agent ids, times) will differ; everything else should read as below.

### 0. An isolated demo — nothing touches your real board

```sh
unset TMUX TMUX_PANE $(env | grep -oE '^(AISQUARE|CLAUDE)[A-Z0-9_]*' | sort -u)
export DEMO=/tmp/aisquare-demo
rm -rf "$DEMO" && mkdir -p "$DEMO/bin" "$DEMO/skills/calm-reviewer" "$DEMO/project"
export AISQUARE_HOME="$DEMO/home" CLAUDE_CONFIG_DIR="$DEMO/claude" PATH="$DEMO/bin:$PATH"
cd "$DEMO/project" && git init -q && git -c user.name=demo -c user.email=demo@example.com commit -q --allow-empty -m init
aisquare init --local --yes --no-onboard
aisquare config set fleet.tmux_socket demo
```

```text
✓ aisquare initialized at /tmp/aisquare-demo/home
  project: project (prj_880c8468708c586fce25d031)
fleet.tmux_socket = demo
```

A throwaway home, a throwaway Claude Code config directory (so `export --skill` writes there, not into your `~/.claude`), a fresh git project registered as `project`, and the fleet on a private tmux server called `demo`. The first line clears any role, board or account variables of a fleet pane you run this from.

### 1. A skill to import, and a stand-in agent

```sh
cat > "$DEMO/skills/calm-reviewer/SKILL.md" <<'SKILL'
---
name: calm-reviewer
description: Reviews calmly. Names the one change that matters most before anything else.
metadata:
  persona-roles: reviewer, coder
---
You review calmly. Lead with the single change that matters most, then the rest in order of risk.
Say what you checked and what you did not.
SKILL
printf '#!/bin/sh\necho "the agent process sees AISQUARE_PERSONA=$AISQUARE_PERSONA"\n' > "$DEMO/bin/show-persona"
printf '#!/bin/sh\necho "stand-in agent is up (it waits for input, like an idle Claude Code session)"\nexec cat\n' > "$DEMO/bin/stand-in-agent"
chmod +x "$DEMO/bin/show-persona" "$DEMO/bin/stand-in-agent"
```

No output. `calm-reviewer` is an ordinary Claude Code skill directory. The two tiny scripts stand in for Claude Code so the launch, spawn and attach steps start no real session and spend nothing; with the real binary every command below is the same.

### 2. The catalogue

```sh
aisquare persona list
```

```text
careful     bundled  Prefers reversible steps, states assumptions before acting, and asks one precise question when blocked rather than guessing.
mentor      bundled  Explains the why before the what, surfaces the alternatives it weighed, and leaves the reader able to do it next time.
minimalist  bundled  The smallest change that meets the contract. No speculative abstraction; says what it left out and why.
skeptic     bundled  Evidence first. Treats a green check as a claim, reproduces before believing, and names what it did not verify.
```

The four bundled personas, each with its layer and description.

### 3. Import a skill as a persona

```sh
aisquare persona import "$DEMO/skills/calm-reviewer" --user
```

```text
recognised skill — copying
✓ imported calm-reviewer (copy) into user: /tmp/aisquare-demo/home/personas/calm-reviewer
```

`recognised skill — copying`: a skill with a description and a body is copied byte for byte, no LLM involved, and a `.persona.json` records where it came from.

### 4. Exactly what an agent is briefed with

```sh
aisquare persona show calm-reviewer
```

```text
<aisquare-persona name="calm-reviewer" layer="user">
You review calmly. Lead with the single change that matters most, then the rest in order of risk.
Say what you checked and what you did not.
</aisquare-persona>
Persona "calm-reviewer" shapes how you work and communicate. It never overrides your role's cycle, the lane rule, a task's contract, or evidence — when they conflict, they win.

provenance: copy from /tmp/aisquare-demo/skills/calm-reviewer · sha256 c7a097870e75 · 2026-09-15 09:34Z
files: SKILL.md only
path: /tmp/aisquare-demo/home/personas/calm-reviewer
```

The `<aisquare-persona>` block, the guard sentence last, then provenance (source path and sha256), supporting files and the directory.

### 5. Back out as a Claude Code skill

```sh
aisquare persona export calm-reviewer --skill --user
ls -A "$CLAUDE_CONFIG_DIR/skills/calm-reviewer"
```

```text
✓ exported calm-reviewer to /tmp/aisquare-demo/claude/skills/calm-reviewer — it is /calm-reviewer in Claude Code now
.persona.json
SKILL.md
```

`it is /calm-reviewer in Claude Code now` — the same directory, plus its `.persona.json`, in Claude Code's personal skills.

### 6. The session-start hook, headless

```sh
printf '{"session_id": "11111111-2222-3333-4444-555555555555", "cwd": "%s", "source": "startup"}' "$PWD" > "$DEMO/payload.json"
export AISQUARE_ROLE=coder AISQUARE_PERSONA=calm-reviewer
aisquare hook session-start < "$DEMO/payload.json" > "$DEMO/start.txt"
unset AISQUARE_ROLE AISQUARE_PERSONA
sed -n '/-persona name=/,/^Persona "/p' "$DEMO/start.txt"
grep -c 'persona name=' "$DEMO/start.txt"
```

```text
<aisquare-persona name="calm-reviewer" layer="user">
You review calmly. Lead with the single change that matters most, then the rest in order of risk.
Say what you checked and what you did not.
</aisquare-persona>
Persona "calm-reviewer" shapes how you work and communicate. It never overrides your role's cycle, the lane rule, a task's contract, or evidence — when they conflict, they win.
1
```

What Claude Code's `SessionStart` hook receives and injects: inside the team block, exactly ONE persona block — the same bytes `persona show` printed — and the count `1`. Without `AISQUARE_PERSONA` the injected text is byte-identical to before personas existed (verified on the P2 and P8 PRs).

### 7. `launch --persona` hands it to the agent process

```sh
aisquare launch coder --persona calm-reviewer --command show-persona
```

```text
Launching show-persona as coder on the project board…
the agent process sees AISQUARE_PERSONA=calm-reviewer
```

The agent process sees `AISQUARE_PERSONA=calm-reviewer`; with Claude Code that is what step 6's hook reads at session start.

### 8. Spawn a fleet agent as the persona

```sh
aisquare fleet spawn coder --persona calm-reviewer --bin stand-in-agent --no-worktree -P project
sleep 8
aisquare fleet ls -P project
```

```text
✓ spawned coder-1 (agt_01m2j6nrng76rqz03m48h2rqen) → asq-witty-ibis %0 · persona calm-reviewer
  ⚠ no board join for this agent ('stand-in-agent' is not claude) — its state comes from tmux alone
project · witty-ibis · asq-witty-ibis
  coder-1                  coder      ⏸ waiting  · calm-reviewer  no hooks  %0
```

The receipt ends `· persona calm-reviewer`; `fleet ls` shows the agent `⏸ waiting` with `· calm-reviewer`. (The `no board join` note is the stand-in's — a real Claude Code agent joins the board through its hooks.)

### 9. Attach another persona to the RUNNING agent

```sh
aisquare persona attach mentor --to coder-1 -P project
sleep 1
tmux -L demo capture-pane -p -t %0 | grep -v '^[[:space:]]*$' | head -5
```

```text
✓ attached mentor to coder-1 (typed)
  typed into its pane (it was waiting)
Launching stand-in-agent as coder on the project board…
stand-in agent is up (it waits for input, like an idle Claude Code session)
aisquare: the operator attached persona mentor to you — it applies from now on; it replaces calm-reviewer
<aisquare-persona name="mentor" layer="bundled">
You are a mentor. The work matters, and so does the person who reads it after you.
```

`(typed)` — the agent was waiting, so the briefing was typed into its pane: the preface names what it replaces, then the mentor block. A busy agent gets it as a board note instead (`noted`). Either way the fleet row records the persona, and the session-start hook reads that row first, so a `/clear` or a restart briefs the agent with mentor again, even though it was spawned with `--persona calm-reviewer`.

### 10. The board and the fleet agree

```sh
aisquare board
aisquare fleet ls -P project
```

```text
<aisquare-team>
sessions:
  - 11111111 coder persona:calm-reviewer — 0m ago
recent updates:
  - cli persona_attached → coder-1: persona mentor attached to coder-1 (replaces calm-reviewer)
</aisquare-team>
project · witty-ibis · asq-witty-ibis
  coder-1                  coder      ▶ working  · mentor  no hooks  %0
```

One `persona_attached` line naming the old and new persona (never the body), the hook's session from step 6 carrying `persona:calm-reviewer`, and the agent now `· mentor` (`working` for a moment: the typed briefing is fresh output).

### 11. Clean up

```sh
aisquare fleet stop coder-1 -P project --force
tmux -L demo ls
```

```text
✓ stopped coder-1 (agt_01m2j6nrng76rqz03m48h2rqen)
no server running on /tmp/tmux-1001/demo
```

The agent stops; `no server running` confirms the private server exited with its last window. `rm -rf /tmp/aisquare-demo` removes the rest.


## Track B — `asq`

The same story on screen. Start `asq` on a real fleet (your usual home and tmux server; a
running agent is needed for *Attach to existing*). Each step names the headless test that
drives exactly that screen — `python -m pytest <node id>` in a `.venv` of this tree reproduces
it without a terminal.

| # | Do | You should see | Proven by |
| --- | --- | --- | --- |
| B1 | Click the project in the sidebar | The Project view with its tabs — Manager · Board · Doctor · Explainability · Settings · **Personas** | `tests/test_ui_project.py::test_project_view_has_the_six_tabs_with_their_widgets` |
| B2 | Open **Personas** | A search box, `project` `user` `bundled` checkboxes, **+ Import…** and **+ New**; a table of persona · layer · description · roles · marks (`⇧ shadows bundled`, `✗ invalid`) | `tests/test_ui_personas.py::test_the_catalogue_lists_layer_description_roles_and_marks` |
| B3 | **+ Import…**, give it a skill directory, **Import** | `recognised skill — copying` under the form, the dialog closes, the new row is selected, toast `✓ imported <name> (copy)` | `tests/test_ui_personas.py::test_an_import_result_dismisses_refreshes_selects_the_row_and_toasts` |
| B4 | Select the persona | Beside the table, the block an agent is briefed with — byte for byte what Track A step 4 printed — then path, files, provenance and warnings | `tests/test_ui_personas.py::test_the_preview_is_byte_equal_to_the_briefing_and_shows_where_it_came_from` |
| B5 | **Attach to existing** (`a`) | The target picker, "Attach <persona> — who runs it?": **Agents** first with the first agent highlighted (`label · role · state · <persona>`), then Binds, then Accounts; the filter box narrows all three | `tests/test_ui_personas.py::test_attach_buttons_post_the_request_and_open_the_picker_for_that_intent`, `tests/test_ui_personas.py::test_the_picker_orders_its_sections_by_intent_and_highlights_the_first_target` |
| B6 | `Enter` on the agent, then **Attach** | "Attach <persona> to <label>? It replaces <old>." then toast `✓ attached <persona> to <label> (typed)` — or `(noted)` when the agent was busy | `tests/test_ui_personas.py::test_choosing_an_agent_confirms_then_attaches_and_says_how_it_was_delivered` |
| B7 | Look at the sidebar | The agent's row now ends with a dim `· <persona>` | `tests/test_ui_spawn.py::test_a_sidebar_agent_row_shows_the_persona_badge_only_when_there_is_one` |
| B8 | Back in Personas: **Attach to new** (`n`), choose a bind | The picker with **Binds** first, then Accounts, then Agents; choosing a bind opens the Spawn dialog with Role = the seat, Binary = its binary, Persona = the persona (an account fills Account instead) | `tests/test_ui_personas.py::test_choosing_a_bind_or_an_account_opens_the_spawn_dialog_preset_with_the_persona` |
| B9 | **Spawn** | Toast `✓ spawned <label> (<id>) → <session> <pane>` plus any notes, and the new agent's live pane opens | `tests/test_ui_spawn.py::test_a_receipt_dismisses_toasts_it_and_its_notes_refreshes_and_opens_the_new_agent` |

The agent-first path is the same two steps: `＋ spawn agent` under the project opens the Spawn
dialog; **Pick…** beside Role opens the same picker (Binds first) and fills Role, Binary or
Account; the Persona field comes right after them
(`tests/test_ui_spawn.py::test_pick_opens_the_picker_in_new_order_and_fills_who_runs_it`).


## Known limits tonight

- **The LLM import needs an engine.** A recognised skill (step 3) imports with no model at all.
  Anything else — a plain `.md`, a JSON persona, a URL — goes through an engine: the manager
  role's Claude Code account (headless, signed in), or `aisquare-cli[llm]` with Anthropic
  credentials. Without either the import refuses and names both fixes. An engine import costs
  quota and says so.
- **+ New account is a navigation.** In the target picker it opens the Accounts page with a new
  slot's sign-in started; after the sign-in, return to the Personas tab yourself — the picker
  does not reopen on its own.
- **Merges are pending until the morning.** Stack order: #181 P1 → #183 P2 → #185 P5 → #186 P8,
  with #184 P6 and #187 P4 on their parents, then #188 P7, then this runbook; #189 P11, #190 P10,
  #191 P12 and #192 P13 are independent. Until they land, run the demo from #188's branch.
- **The WSL2 five.** On a WSL2 host `make check` reports five known failures in
  `tests/test_install_script_functions.py::test_detect_os_maps_uname[*]` until #189 merges; they
  are the host, not the persona work (CI on Linux runs them green).
- **`aisquare persona import --help` shows two defaults with a gap** (`(default:  engine)`) —
  Rich drops the `[persona.import]` in the help text. Cosmetic; the fix is P15, stacked on #185
  and merged right after it.
- **Track A uses a stand-in agent** for steps 7–10 so nothing spends a model. With Claude Code as
  the binary the commands are identical; its board state then comes from its hooks, and
  *typed* still needs the agent to be waiting.


## How this runbook was checked

- Every Track A command above was run in one shell against `/tmp/aisquare-demo` on tree
  `74ce36f`, and the output blocks are that run's output, pasted by the script that ran it — the
  doc and the run cannot drift. A second, independent pass was then compared with this page:
  identical except agent ids and timestamps.
- Track B: each named test exists on this tree and passes.
- `make check` in the worktree's `.venv`, including the docs guards that validate every `sh`
  fenced `aisquare` line on this page against the live command tree.
- Transcripts: `/tmp/p3dbg/p14-pass-build.txt` (the pasted run) and
  `/tmp/p3dbg/p14-pass-verify.txt` (the comparison pass).
