# Streams, and learning from the conversation itself

**Status: proposal.** Nothing here is built. This page exists because the
first serious attempt to set aisquare up for one person's real workload found
two gaps in the model, and both were expensive to discover by reading code. The
findings are recorded before the fix so the fix is argued against the right
problem.

The workload that surfaced them: four streams of work on one machine —
**MetricStream** (an enterprise cell), **Platform** (the product it is a cell
of), **Adoption Manager** (a feature spanning three repos), and **SOC 2**
(a compliance programme with no code at all). Two of the streams are the *same
repositories cloned twice* — one clone pinned to enterprise branches, the other
on `develop` — running as two Docker stacks side by side.

---

## 1. What the model says today

aisquare knows two scopes:

| Scope | Identity | Injected when |
| --- | --- | --- |
| `user` | the person | always |
| `project` | `sha256(resolved root path)` — the git common dir, or the nearest marker dir, or cwd | cwd resolves to it, or it is pinned |

Injection is exactly `user + active project` (`core/injection.py`). Three
things around that are worth stating plainly, because each looks like more than
it is:

- **`project link <repo>` records a string and nothing reads it.** It appends to
  the `linked_repos` JSON column (`core/store.py`). Injection, snapshot and
  search never consult it. Linking two repositories does not share one entry
  between them.
- **`project switch` writes one global pin** to `~/.aisquare/state.json`, and
  `active_project` honours the pin *before* cwd (`core/workspace.py`). Pin the
  enterprise clone, `cd` into the `develop` clone, and the enterprise pool is
  what gets injected — with no visible signal.
- **A directory with no marker becomes its own project.** `find_project_root`
  falls back to `start` itself. Run `aisquare remember` from `$HOME` and the
  fact lands in a project keyed on `$HOME` that no session will ever resolve to
  deliberately.

None of these is a bug in the code as written. They are what happens when the
unit of work is *one checkout* and the real unit of work is not.

## 2. The four cases, and where each breaks

### 2a. Same repository, two clones, two stacks

`~/aisquare-workspace/AISquare-Studio-BE` (branch `metricstream`) and
`~/aisquare-workspace-platform/AISquare-Studio-BE` (branch `develop`) are two
projects, because identity is the path. **That part is right** — the two clones
have genuinely different facts (hard-coded CORS vs an env hook; a database that
must be preserved vs one that is restored from dumps). The break is the other
direction: everything that is true of *both* (the port map, "BE serves both
platforms", the migrate-on-start hazard) has nowhere to live except the user
pool, where it is a personal preference in name only.

### 2b. One stream requires another

MetricStream *is a deployment of* Platform. Features land in the platform BE
first, flagged off, then merge to the `metricstream` branch. A MetricStream
session needs the platform conventions. Today the only way to express that is
to copy the entries — and copies drift.

### 2c. One repository, several streams

`AISquare-AIStudio-v3` in the enterprise clone carries the MetricStream
deployment *and* the Adoption Manager feature branch. Both sets of facts are
true of that checkout. A project pool can hold both, but nothing can say which
is which, and nothing lets the Adoption Manager facts follow the feature into
the other two repositories it touches.

### 2d. A stream with no code

SOC 2 lives in `~/SOC2/` (Markdown, decision records, a changelog), a Vanta
tenant, and an AWS account. `init` there works — a `.aisquare` marker makes a
project — but the standing rule that matters most ("every action gets a dated
entry in the changelog and the action-plan artifact") must fire when SOC 2 work
comes up *inside a platform repository* (an IAM change for the stack, say). A
project pool cannot do that; only the user pool can, and see 2a.

### 2e. Workspace roots snapshot to nothing

Both workspace roots are git repositories whose `.gitignore` lists the nested
clones. Repomix honours `.gitignore`. `init` at a workspace root packs the
planning documents and none of the code. This is not wrong — but it is not what
anyone running `init` there expects, and nothing says so.

## 3. Proposal: a scope between user and project

Add **stream**: a named body of work that owns a set of projects.

| Scope | Identity | Holds |
| --- | --- | --- |
| `user` | the person | genuinely personal preferences |
| `stream` | a name; a set of member projects; a set of required streams | conventions true across the stream's repositories |
| `project` | unchanged | facts true of one checkout |

Five rules, each answering one case above:

1. **A project may belong to many streams** (2c). Membership is a table, not a
   column.
2. **A stream may contain non-git roots** (2d). Streams do not require code.
3. **A stream may require other streams** (2b). `metricstream` requires
   `platform`. Injection follows the edge.
4. **Injection = user + active project + every stream the project belongs to +
   their required streams**, deduplicated, each entry labelled with the scope
   it arrived through, so `aisquare why` can answer *"via metricstream →
   platform"*.
5. **Resolution stays cwd-first, and nothing global overrides it.** The pin is
   removed. Forcing a stream — SOC 2 work from a platform repo — is a per-shell
   `AISQUARE_STREAM=soc2` or a per-command `--stream soc2`. A file that follows
   you silently into the next terminal is exactly the failure mode in §1.

With zero streams defined, behaviour is byte-identical to today. That is the
compatibility contract.

### What the surface looks like

```sh
aisquare stream new platform
aisquare stream new metricstream     --requires platform
aisquare stream new adoption-manager --requires platform
aisquare stream new soc2

aisquare stream add platform         ~/aisquare-workspace-platform/*/
aisquare stream add metricstream     ~/aisquare-workspace/AISquare-Studio-BE ~/aisquare-workspace/Metricstream-Studio
aisquare stream add adoption-manager ~/aisquare-workspace/AISquare-AIStudio-v3 ~/aisquare-workspace/aisquare-studio-unified
aisquare stream add soc2             ~/SOC2

aisquare remember "MS PRs target metricstream, so 'Closes #' is inert" --stream metricstream
aisquare remember "every action: dated CHANGELOG entry + artifact update"  --stream soc2
```

`cd ~/aisquare-workspace/AISquare-AIStudio-v3` then injects user + that
checkout + `metricstream` + `adoption-manager` + `platform`. `cd ~/SOC2`
injects user + `soc2`. Nothing pinned, nothing copied.

### What it replaces

| Today | Proposed |
| --- | --- |
| `project link <repo>` (inert) | `stream add <stream> <path>…` — membership injection actually reads |
| `project switch` (global pin, beats cwd) | removed; `--stream` / `AISQUARE_STREAM`, session-scoped |
| `pool ∈ {user, project}` | `pool` also accepts `stream:<name>`; `remember --stream` |
| project id = `sha256(path)` | unchanged; **add** `remote_url` and `branch` as metadata so two clones of one remote are visible siblings and `stream add` can offer the other clone |
| unmarked cwd → project = cwd | refuse to auto-create a project at `$HOME` or `/`; marker-dir projects require an explicit `init` |
| snapshot at a root whose `.gitignore` hides its clones | `init` says so and offers `--no-onboard`; a stream-level snapshot packs each member and writes one index (later) |

### Open question

Should the port map for two stacks live in `user` or in `platform`? This page
says `platform`, and lets `user` shrink to what is personal. Otherwise `user`
becomes the place everything cross-cutting is thrown, which is the state it is
in already.

---

## 4. The second gap: the conversation is where the context is

Setting the model aside, there is a plainer problem. aisquare captures **what
the person typed** (`UserPromptSubmit` → prompt text → `aisquare log`) and
**what the person told it** (`remember`). It does not capture **what was
learned** — the conclusions, decisions and gotchas that a session produces and
that the next session needs.

Concretely, on the machine that motivated this page:

| Source | Read by aisquare |
| --- | --- |
| prompt text, per turn | yes |
| the agent's replies, tool calls, decisions | **no** — `transcript_path` is stored on the session row (`services/hooks.py`) and never opened |
| sessions from before install (303 MB of `~/.claude/projects/*/*.jsonl`) | **no** |
| Claude Code's own auto-memory (`~/.claude/projects/*/memory/*.md`) | **no** |
| `CLAUDE.md` | **no** |

The memory files are the sharpest case. They are already one fact per file with
typed frontmatter (`type: user | feedback | project | reference`) and wiki
links — precisely aisquare's shape — and they are the only reason a fresh
session knows anything. They are also **loaded per directory**: memory written
from `$HOME` is not loaded when the agent is launched inside a sub-repository.
aisquare's cross-directory pools would fix that today, if aisquare could read
them.

### Three tiers, cheapest first

**Tier 1 — import auto-memory.** `aisquare import claude-memory` reads the
memory directories, maps `type: user` and `type: feedback` to the user pool and
`type: project` to the stream or project its path implies, and keeps the
frontmatter `name` as a tag so re-import is idempotent. No model call. This is
the install-day win and it can be done by hand with `context import` today.

**Tier 2 — backfill transcripts.** `aisquare import claude-code [--since
DATE]` walks `~/.claude/projects/<slug>/*.jsonl`. The slug *is* the cwd, so
every session maps to a project for free. Each session goes through redaction
(`core/redaction.py` already exists) and then one extraction call — *durable
facts, decisions, conventions, gotchas; not task chatter* — whose output lands
as **pending** entries. `aisquare context pending` lists them; approve or reject
one at a time or in bulk. Nothing reaches a pool unreviewed: one wrong
extraction injected into every future session costs more than a missing one.

**Tier 3 — distill on `Stop`.** The hook already fires at the end of every
turn with `transcript_path` in its payload. Spawn a detached drain — the same
pattern `services/distill.py` uses for team events: off the hot path, own
watermark, never fatal — that reads only the delta since the last watermark,
extracts, and queues pending entries. Same review gate. This is what makes the
memory compound between sessions instead of decaying.

### Two decisions to make before Tier 2

**Where the extractor runs.** A local Haiku call on the person's own key is the
obvious default. But the Explainability proxy already sits on every call the
agent makes and records model, prompt, response and tool use; when it is on,
the gateway holds the whole transcript already. Distillation could be a gateway
worker beside the RML extractor, with the CLI pulling results — one pipeline for
memory and explainability rather than two readers of one stream. This page does
not pick; it notes that the second option is where the product is pointing.

**Which store is canonical.** After Tier 1 the same facts exist in the agent's
memory directory and in `context.db`. Two writers drift. Proposal: aisquare is
canonical and *exports* a `MEMORY.md`-shaped view for the agent's native loader.
One source, two projections.

---

## 5. Phasing

| Phase | Scope | Compatibility |
| --- | --- | --- |
| **1 — streams: schema + injection** | `stream`, `project_stream`, `stream_requires` tables; `pool` accepts `stream:*`; injection walks the graph; `why` reports provenance | zero streams ⇒ identical to today |
| **2 — streams: surface** | `stream new/add/remove/list/show/requires`; `remember --stream`; `--stream` flag; `AISQUARE_STREAM`; deprecate `project switch` and `project link` with a pointer | pin honoured with a warning for one release, then removed |
| **3 — guards + snapshots** | refuse `$HOME`/`/` auto-projects; `remote_url`/`branch` metadata and sibling detection; `init` explains an ignore-hidden root; stream-level snapshot index | — |
| **4 — memory import** | Tier 1 (`import claude-memory`) and Tier 2 (`import claude-code`, pending queue, `context pending`) | additive |
| **5 — distill** | Tier 3 on `Stop`; extractor location decided; canonical-store export | additive; off by default |
| **6 — orchestration + explainability** | boards scoped to a stream; `agent_name_template` gains `{stream}` so Runs group by stream in the dashboard | additive |

Each phase is its own PR against its own issue, per CONTRIBUTING. This page is
the argument they share.
