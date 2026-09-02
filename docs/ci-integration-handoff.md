# CLI ↔ CI server integration — handoff

**For:** the engineer building the Collective Intelligence server (`aisquare-ci`).
**From:** the `aisquare-cli` side. **Date:** 2026-08-28.
**Read against:** `aisquare-cli` branch `feat/collective-intelligence` — the former #60–#64 stack consolidated and merged with `main` @ `905c68b` (0.5.0), open as a draft PR into `main`; `aisquare-ci` `main` @ `fff5646`; the plan
*CI Programme — Three-Component Split* (2026-08-24).

This document is the seam. It says what the CLI will send and expect, what it needs from the
server before a single real turn can flow, and which decisions neither side can make alone.
Where the two repositories disagree today, it says which one is right and why. Canon
(`aisquare-ci/docs/canon/`) outranks this file; where they differ, canon wins and the
disagreement is a bug here.

---

## 1. Where each side actually is

### Server (`aisquare-ci`)

Built (per `README.md` and `docs/handoff-2026-08-25.md`): Slices 0–7 and 9 — substrate,
contracts (67 schemas), immutable runs, signal ledger + normalizer, deterministic R1 builder,
exact-reference reader, complete ledger and restore. Not started: 8, 10, 11.

**Update 2026-09-02:** R1 is deployed at `https://ci-api.aisquare.studio` (`main` `4cb104b`, staging,
us-east-1). `POST /v1/hook` and `POST /v1/mcp/collective_intelligence_recall` are built and live; the
descriptor route still serves the constant `direct_api` list; the trusted session mapping is still a
stub. `docs/ci-live-wiring-handoff.md` is the CLI-side plan against the live server. The table below is
as of `fff5646`, with the rows that changed marked.

What matters for the CLI:

| Surface | State | Where |
|---|---|---|
| Hook contract v2 schemas + fixtures | **exist, frozen** | `contracts/jsonschema/delivery/hook-request.experimental-v2.schema.json`, `hook-response.experimental-v2.schema.json`, `contracts/fixtures/{valid,invalid}/hook-*.json` |
| Delivery descriptor schema + `GET /v1/experiment/runs/{run_id}` | **exists, live** — the handler still returns a constant `direct_api` delivery list for every run (`app/api/runs.py`, `DIRECT_API_DELIVERY`), `client_safety_ms` 60 000, `expires_at` now + 1 h. *Still true at `4cb104b`* | `client-delivery-descriptor.v1` |
| MCP tool schemas (`collective_intelligence_recall`) | **exist** | `mcp-tool-input.v1`, `mcp-tool-output.v1` |
| Capability manifest schema | exists, **no route serves it** | `delivery-capability-manifest.v1` |
| `POST /v1/hook` | ~~not built~~ **built and live** (`app/api/delivery.py`, bound to `hook-request/response.experimental-v2`; any experiment token, a recorded stand-in for a developer-role principal) — *changed 2026-09-02* | `app/api/delivery.py` |
| MCP server-side handler | ~~not built~~ **built and live as an HTTP route**: `POST /v1/mcp/collective_intelligence_recall` takes `mcp-tool-input.v1`, returns `mcp-tool-output.v1` directly — *changed 2026-09-02* | `app/api/delivery.py` |
| Trusted session mapping (`ses_…` → principal / projects / studios) | **stub returning empty lists** — every real session resolves to zero scope; only admitted `workspace_wide` items can serve | `app/api/queries.py::trusted_session_mapping`; handoff "Residuals" |
| Tokens | five experiment tokens in Secrets Manager `aisquare-ci/stg/api-token` (us-east-1); use `CITEST_READER_TOKEN`; `Authorization: Bearer` — *changed 2026-09-02* | `app/api/auth.py`, `docs/deploy/ci-client-integration.md` |
| `ExplainabilityTraceBatchSource` (pull CLI sessions' spans from Explainability) | **not built**; only `TraceFixtureSource` | `app/sources/__init__.py` |
| `run_kind: live \| replay` | **absent** from `hook-request.v2`, `normalized-signal.v2` and the ledger | plan §1.5 asks for it now |
| `status: degraded` | unreachable from the reader | flagged in the schemas' own descriptions |
| Health | `GET /live`, `GET /ready` — public, no auth | `app/api/health.py` |

### CLI (`aisquare-cli`)

On `main` (0.5.0): the Explainability integration — every launched session is traced through the
hosted proxy (`ANTHROPIC_BASE_URL` + `X-Agent-Name` / `X-Pipeline-Id`), the client lane spools
prompts and board events to the gateway, and `record_join` pairs the Claude Code `session_id`
with the pipeline id. Hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`,
`Notification`) are installed by `aisquare agents connect` and removed by `disconnect`.

On `feat/collective-intelligence` (PR #72, draft), **as of 2026-08-28 the CLI side speaks contract
v2 and is built as far as it can be without the server.** Built and tested against the vendored
schemas and a local v2 stub:

| Surface | State on the branch |
|---|---|
| Hook contract v2 models | `services/ci_contract.py` — closed, frozen, strict; every pattern and cross-field rule; the seven server schemas + fixtures vendored byte-for-byte from `fff5646` under `tests/fixtures/ci_contract/v2/`, validated with `jsonschema`; a drift test compares against a sibling `aisquare-ci` checkout |
| Descriptor client | `services/ci_descriptor.py` — `GET /v1/experiment/runs/{run}`, cached until `expires_at`, refuses skew / expiry / 401 / 404 with distinct detail, never raises |
| Transport | `services/ci_client.py` — `urllib`, one attempt, **wall-clock deadline** from `client_safety_ms` (thread + join, chunked reads, late arrival is a breach), body cap, no client cache |
| Hooks | `session_start` / `prompt_submit` only when the descriptor lists them; outcome of every call recorded; `trace_id` sent = `trace_id` recorded |
| Snapshot | `services/ci_snapshot.py` — `git stash create` → `refs/aisquare/wip/<trace_id>`, object id on the wire, `untracked_excluded` on the row; `project_ref` from `origin` with credentials stripped |
| Injection | `core/injection.py` — `aisquare-ci-frame/1`: caveat before and after, delimited region the payload cannot close, control characters stripped, 16 KB cap, both sizes recorded |
| Metric row | `metric` table v11 rewritten in place: the §5 join keys, `status` + `action` beside a closed `client_reason` vocabulary in three groups, `run_kind` (local), `opaque_config_id`, `redaction_level`, `frame_version`, `instruction_version`; no `arm`, no `run` table. **v12 (2026-09-02)** adds `delivery_source` (`descriptor \| override`) as a healing migration — `CREATE TABLE IF NOT EXISTS` then `ALTER TABLE`, because v11 has reached developer machines, some with the table deleted by hand |
| Join record | `insights.record_turn` spools a `ci_turn` record through the client lane; the sweeper emits it as a `ci.turn` span in the session's Run |
| Pull | `services/ci_recall.py` — `collective_intelligence_recall` registered in `aisquare serve` when the descriptor lists `mcp_pull`; standing instruction (`aisquare-ci-instruction/1`) at `SessionStart`; ~~forwards as `agent_request` via the hook route (J7 default)~~ **forwards to `POST /v1/mcp/collective_intelligence_recall` as `mcp-tool-input.v1` with `run_id` always filled, `token_budget`/`reason` carried; parses the bare briefing — *changed 2026-09-02*** |
| `doctor` | switch → URL (scheme required, credentials never echoed) → token → run → `GET /ready` → descriptor fetch without caching, each its own line; every probe bounded. **2026-09-02:** a refusal quotes the server's `error.v1` code and sentence (fix chosen from the status, not the words); a further line warns whenever `AISQUARE_CI_DELIVERY_OVERRIDE` is set — active, ignored, or malformed |
| Staging override | `services/ci_override.py` — **dated interim (live-wiring handoff §2 B), owner's call whether it stays:** `AISQUARE_CI_DELIVERY_OVERRIDE=hook_push:session_start,prompt_submit;mcp_pull` stands in for the delivery list only when the fetched descriptor is `direct_api`-only; every row and join record says `delivery_source: override`; never cached. Removed once `DIRECT_API_DELIVERY` publishes real modes |
| Refusals | a non-200 with an `error.v1` body puts `code` on the row's `error_codes` and the clipped `message` in the detail — verified live against the staging 401 (2026-09-02) |
| Installed hooks | `SessionStart` / `UserPromptSubmit` carry `timeout: 120` so Claude Code's 60 s default cannot discard a hook still inside the ceiling (J4) |
| Stub | `tests/stub_ci_server.py` speaks v2 (`/ready`, descriptor route, programmable `/v1/hook` with delay and drip, and the pull route `/v1/mcp/collective_intelligence_recall` with its own programmable answer); runnable by hand for a real-session smoke |

Not built, by design: client metrics from spans (tokens, active wall time, idle exclusion —
columns stay `NULL`, reported as "not measured"); `aisquare replay` (Slice 13). §3 below is the
wire as implemented; §4 is the v1→v2 field map that was applied; §6 the decisions still open,
each coded as a default assumption listed in `docs/ci-contract.md`.

## 2. Ownership, restated for this integration

From `04-build-plan.md` §8 and `00-owner-decisions.md`:

| Server owns | CLI owns | Joint approval |
|---|---|---|
| hook + pull-provider endpoints; MCP tool schema and **server handler**; sealed run config + neutral descriptor; canonical schemas; ledger facts + exports; dummy harness | hook invocation + prompt insertion; tool exposure + agent discoverability; fetching/honouring **only** the descriptor; snapshot + replay; agent execution; client tokens/turns/active wall time; real runner; task/test/code-quality + judge scoring | contract version · IDs · clocks · retry semantics · snapshot identity · trusted auth claims · failure semantics · fixtures · headroom criteria · ledger joins |

The CLI never holds an architecture name or arm label. Anything below that would require it to is
a bug in this document.

---

## 3. The wire, as the CLI will implement it

Authority for every field is the server's schema file. The CLI will **vendor the six delivery
fixtures byte-for-byte** (A-08) and fail its own suite when they drift.

### 3.1 Descriptor — `GET /v1/experiment/runs/{run_id}` (`client-delivery-descriptor.v1`)

Fetched at `SessionStart`, cached until `expires_at`, refetched on expiry. Everything the CLI does
for a run is driven by it:

| Field | CLI use |
|---|---|
| `contract_version` (const 2) | refuse to call the hook if it is not the version this build speaks |
| `run_id` | echoed on every hook request and MCP call |
| `opaque_config_id` | recorded on every local metric row; never interpreted |
| `delivery[]` | `hook_push.triggers` decides which hooks call the server; `hook_push.endpoint` is the path POSTed to (server-relative, joined to the configured base URL); `mcp_pull.tool` decides whether the recall tool is exposed and the standing instruction injected |
| `client_safety_ms` | the CLI's hang ceiling for that run — **replaces the stack's hard-coded 10 s backstop** |
| `retry_policy: none` | one attempt per developer event, ever |
| `expires_at` | cache lifetime |

**How `run_id` reaches the CLI:** proposed `AISQUARE_CI_RUN=run_…` alongside `AISQUARE_CI=1`,
`AISQUARE_CI_URL=<base>`, `AISQUARE_CI_KEY=<experiment token>` (env only, never `config.toml`).
The experiment runner sets it for replay; for live cohort sessions the controller publishes one
run per cohort and the developer exports it. No `run_id` ⇒ no descriptor ⇒ no calls, recorded as
`not_configured`. (Closes v1 bilateral #1.)

### 3.2 Push — `POST {endpoint}` (`hook-request.experimental-v2`)

What the CLI sends, and where each value comes from:

| Field | Value | Source | Notes |
|---|---|---|---|
| `contract` | `2` | constant | body only; the v1 `X-CI-Contract` header goes away |
| `trigger` | `session_start` \| `prompt_submit` (\| `agent_request`, see §3.4) | which hook fired | only triggers the descriptor lists |
| `run_id` | `run_…` | descriptor | |
| `session_id` | `ses_` + Claude Code `session_id` | hook payload | Claude Code ids are UUIDs (36 chars); prefixed they fit `^ses_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`. **Needs A-02 sign-off.** Same value the CLI already uses for the board row and the Explainability join |
| `trace_id` | `trc_` + ULID, **one per turn** | CLI mints | matches A-02 "ULID-shaped"; same id is written to the local metric row and to the client-lane join record (§5) — the v1 code currently mints two different ids, which is a known defect |
| `project_ref` | `<origin owner/repo>@<branch or worktree>` | git | ≤500 chars, selector only, grants nothing |
| `snapshot_ref` | 40-hex **object id**: `git stash create` on a dirty tree, `HEAD` on a clean one; `null` at `session_start` if not yet resolved | git | the schema rejects ref *names*, so `refs/aisquare/wip/<trace_id>` is only where the CLI keeps the object alive for replay (C6/C7); the id is what travels. Satisfies A-03 |
| `prompt` | the raw prompt | hook payload | `null` on `session_start` (required by schema); ≤100 000 chars; CLI never sends `prompt_submit` for a blank prompt. **Redaction is open** (§6) |
| `client_safety_ms` | from descriptor | | |
| `client_observed_at` | UTC RFC 3339 with `Z` | client clock | never differenced against server time by either side |

Nothing else. No `project_id`, `budget_ms`, `arm`, `tool`, `flags`, workspace or studio ids.

### 3.3 Response handling (`hook-response.experimental-v2`)

| Field | CLI behaviour |
|---|---|
| `contract` ≠ 2 | client reason `contract_mismatch`; nothing injected; row recorded |
| `status` | recorded verbatim. `unavailable` is **never** folded into the CI-off baseline (canon §10) |
| `action: inject` | inject `briefing.rendered_context`; `action: noop` → inject nothing |
| `briefing.query_id`, `briefing_id`, `config_fingerprint`, `input_checkpoint`, `resolved_scope_version`, `token_count`, `cache.status`, `items[].item_id/item_version` | recorded on the local metric row — these are the join keys (A-09) |
| `server_ms`, `deadline.{server_ms,client_safety_ms,breached}` | recorded; `network_ms = round_trip_ms − server_ms` computed client-side |
| `errors[]` | `code`s recorded; free text kept for logs only |

Client-side failure reasons (`transport_error`, `deadline_exceeded`, `http_error`,
`malformed_body`, `contract_mismatch`, `schema_mismatch`, plus the never-asked states `disabled`,
`not_configured`, `push_not_in_descriptor`, `no_prompt`) are a **separate axis** from the server's
`status` and are recorded beside it. A turn with a client-side failure is treated like
`unavailable`: excluded by reason code, never counted as baseline. This is the plan's C10 ("record
as `unavailable` with a reason — never silently `noop`") in v2 vocabulary.

**Injection framing.** `rendered_context` is byte-identical across arms by construction (render
invariance). The CLI wraps it in a *constant, versioned* frame ("retrieved by aisquare — you did
not fetch this; open the cited source before relying on it") and appends it **after** any team
delta, closest to the prompt. The frame text and version are recorded on the row and are an
experimental variable (plan C5). Whether the frame is applied at all is a joint call (§6).

### 3.4 Pull — `collective_intelligence_recall` (`mcp-tool-input.v1` → `mcp-tool-output.v1`)

The CLI already runs an MCP server (`aisquare serve --stdio|--port`, `services/mcp_server.py`)
that Claude Code connects to. The natural home for the tool is there: the CLI registers
`collective_intelligence_recall(prompt, session_id, run_id?, token_budget?, reason?)` when the
descriptor lists `mcp_pull`, injects the versioned standing instruction ("consult CI before
exploring") at `SessionStart`, and forwards the call to the server.

**What the forward hits is undecided** (A-07). Two options:

1. **Hook route with `trigger: agent_request`.** Already in v2; requires `prompt`; returns the same
   `briefing` shape (`mcp-tool-output.v1`). Server needs nothing new beyond `/v1/hook`. The CLI
   returns `briefing` to the agent as the tool result. Cheapest; keeps "one service function".
2. **A server-side MCP transport** the CLI's tool proxies to. Matches "connection identity
   determines the principal" literally, but the CLI is already the connection Claude Code holds.

The CLI side recommends (1) and will build it unless the server rules otherwise.

`token_budget` and `reason` pass through untouched; `session_id` and `run_id` are the same values as
§3.2. The tool never accepts scope-shaped arguments — the schema closes them and the CLI will not
add any.

### 3.5 Observation — traces

Tool activity does **not** go through the hook. Sessions are already traced into Explainability by
the proxy lane on `main`; the server ingests them through `ExplainabilityTraceBatchSource` (its
build item). What the CLI adds so the two sides can be joined is §5.

---

## 4. Field map, v1 stack → v2

For the v2 re-cut on the CLI branch; the server needs none of it.

| v1 (stack) | v2 | Disposition |
|---|---|---|
| `X-CI-Contract: 1` header | `contract: 2` in body | drop header |
| `trigger: tool_intercept` | — | remove; observation is async ingest |
| `project_id` | `project_ref` (free text) | rename + change value |
| `budget_ms` (advisory 400) | `client_safety_ms` (from descriptor, 60 000) | replace |
| `arm` | — | **remove; blinding** |
| `tool {name,args}` | — | remove |
| — | `run_id` required | from descriptor |
| — | `client_observed_at` | add |
| `action: allow \| substitute` | not v2 actions | remove; client failures become client reasons + `unavailable`-class rows |
| `context` (markdown) | `briefing.rendered_context` | server-rendered |
| `provenance[{node_id,source}]` | `briefing.items[].evidence_ids` (+ `structured_facts`) | rename |
| `flags_applied` | — | remove |
| `cache_hint {ttl_s,key}` + `services/ci_cache.py` | — (server caches; `briefing.cache` reports it) | **delete the client response cache**; keep only descriptor caching until `expires_at`. Closes v1 bilateral #2 and #5 as moot |
| `server_ms` | `server_ms` + `deadline{}` | keep, add |
| — | `status`, `config_fingerprint`, `errors[]`, `briefing.*` ids | add to `metric` row |
| `metric.arm`, `metric.flags_hash`, table `run(arm, flags_hash)` | — | **remove** (client never sees arm); add `opaque_config_id`, `run_kind`, `status`, `query_id`, `briefing_id`, `config_fingerprint`, `deadline_breached`, `token_count`, `cache_status`, `frame_version` |
| `experiment.push` / `experiment.pull` config flags | `descriptor.delivery[]` | remove — the descriptor decides delivery; the CLI keeps only the master kill switch |
| 10 s `CLIENT_BACKSTOP_SECONDS` | `client_safety_ms` from descriptor, enforced as a real wall-clock deadline (not a per-socket-op timeout) | replace |
| `doctor` TCP connect | `GET /ready` + a descriptor fetch | replace. Closes v1 bilateral #6 |

---

## 5. Ledger joins (A-09) — what the CLI will write so rows can be paired

Per turn, locally (`metric` row) and shipped through the existing client lane to Explainability:

```
{ session_id: "ses_…", pipeline_id: "<X-Pipeline-Id>", trace_id: "trc_…",
  run_id, run_kind: "live|replay", opaque_config_id,
  query_id, briefing_id, config_fingerprint, status, client_reason,
  client_observed_at, round_trip_ms, server_ms, deadline_breached,
  injected_chars, frame_version, tokens_in?, tokens_out?, tool_calls? }
```

- `session_id ↔ pipeline_id` is what `record_join` already writes locally
  (`services/explainability.py`), so the OTel spans (W3C trace ids, unprefixed) and the hook
  `trc_` ids meet through the pipeline id, not by sharing an id space — exactly the join the
  server schema says is "left to the adapter".
- The server never infers a missing client row as zero; the CLI never fabricates a count. Token
  and tool columns stay `null` until they come from real evidence (Explainability spans).
- Idle exclusion (plan C9: sub-agent wait subtracted from active wall time) will be derived from
  the same spans (`Tool:Agent` fan-out is countable per `docs/explainability-tracing-boundary.md`);
  it is client-owned and not yet built.

---

## 6. Decisions to settle together (with a proposed answer each)

Mapping the CLI's old bilateral list onto canon's A-series. "Proposed" is the CLI side's
suggestion, not a ruling.

| # | Canon id | Question | Proposed |
|---|---|---|---|
| J1 | A-01 | Hook v2 shape | accepted as frozen in `contracts/jsonschema/delivery/`; the CLI vendors the fixtures |
| J2 | A-02 | IDs | `ses_` + Claude Code UUID; `trc_` + ULID per turn (CLI-minted); `run_` and `qry_` server-minted; the CLI never mints a run or query id |
| J3 | A-03 | Snapshot identity | 40-hex object id from `git stash create` / `HEAD`; the CLI keeps the object under `refs/aisquare/wip/<trace_id>`; dirty-tree hash is implied by the stash commit |
| J4 | A-04 / O-17 | Clocks and the developer's wait | `client_safety_ms` comes from the descriptor (60 000 today). The CLI's installed `UserPromptSubmit` hook `timeout` must exceed it, and the developer waits synchronously for up to that long on a slow server. The CLI side's earlier argument stands — a tight client clamp turns latency into silently absent data — but 60 s in front of a developer is a product decision to make explicitly, not inherit |
| J5 | A-05 | Retries | none, ever (`retry_policy: none`); each attempt is its own row on both sides |
| J6 | A-06 | Auth + user attribution | today: 4 fixture tokens, workspace-level. Needed for per-developer attribution (plan §1.5): one experiment token per developer, or a token + a server-side `principal` mapping the CLI can name. The CLI will send whatever the token carries and nothing more |
| J7 | A-07 | MCP exposure | **settled 2026-09-02:** the server's `POST /v1/mcp/collective_intelligence_recall` (`mcp-tool-input.v1` → bare `mcp-tool-output.v1`); tool registered in the CLI's MCP server under the canonical name; `run_id` always filled from the descriptor (the server refuses its absence, 422) |
| J8 | A-08 | Fixtures | the CLI copies the six delivery fixtures + `error.v1` examples verbatim and pins them with a drift test; any change is a coordinated commit in both repos |
| J9 | A-09 | Ledger join | §5 record; join on `(run_id, session_id, trace_id, query_id)`; pipeline id bridges to spans |
| J10 | A-13 | Config surface | `AISQUARE_CI`, `AISQUARE_CI_URL`, `AISQUARE_CI_KEY`, `AISQUARE_CI_RUN`; nothing about arms or architectures anywhere in the CLI |
| J11 | — (new) | **Trusted session mapping** | the CLI cannot make a `ses_` resolve to projects/studios; the server's mapping is empty today. Proposal: the token identifies principal + workspace; `project_ref` selects; projects/studios resolve from grants the controller seeds per run. Until this exists every live query returns `empty` |
| J12 | — (new) | **`run_kind: live \| replay`** | add to `hook-request.v2` (or descriptor), to `normalized-signal.v2` and the ledger; the graph builder ingests `live` only. The CLI will send it from the first v2 build; without a field it can only be a `project_ref` convention, which is not good enough |
| J13 | — (new) | Prompt redaction | the CLI has `RedactionSettings.level` (off/standard/strict) for what it stores locally and ships to Explainability; propose the same level applies to `prompt` on the hook, recorded as `redaction_level` on the client row so server text can be reconciled |
| J14 | — (new) | Injection frame | constant, versioned CLI frame around `rendered_context` vs verbatim injection. Proposed: frame on, version recorded, because a bare server block reads as established fact |
| J15 | — (new) | Error codes the hook can emit | publish the `error.v1` `code` values `/v1/hook` will use (at least contract mismatch, deadline exceeded, dependency unavailable, scope unresolved) so the CLI can map them deterministically |
| J16 | — (new) | Health for `doctor` | `GET /ready` (public) is enough; a capability-manifest route would let `doctor` also confirm the contract version before the first call |

---

## 7. Sequence to the first joined turn

Order matters; each step is small.

**Server**
1. `POST /v1/hook` bound to `hook-request/response.experimental-v2`, calling the same service
   function as `POST /v1/experiment/queries`; experiment-token auth; contract mismatch →
   `unavailable/noop` with versions recorded (canon §10).
2. Descriptor publishes the run's real `delivery[]` (hook_push triggers, mcp_pull) instead of the
   constant `direct_api`; a way to create a run with those modes on the dev server.
3. A minimal trusted session mapping (J11) — even "token → workspace, `project_ref` → project"
   — so a real session can be served something other than `empty`.
4. `run_kind` (J12) and the hook's `error.v1` codes (J15).
5. Later: `ExplainabilityTraceBatchSource`; per-developer tokens (J6); `degraded` reachability.

**CLI** (re-cut of the v1 code on `feat/collective-intelligence` against v2; order preserves the "baseline first" argument)
1. Contract v2 models + vendored fixtures + total parser + client-reason ladder.
2. Descriptor client (`AISQUARE_CI_RUN` → cached descriptor) and transport with a real wall-clock
   deadline taken from the descriptor; no retries; no response cache.
3. `metric` schema with the v2 columns (§4), migration written once; rows from day one.
4. Hooks driven by the descriptor: `session_start` / `prompt_submit` only when listed; snapshot
   capture (`git stash create`); injection of `rendered_context` in the versioned frame; join
   record (§5) through the client lane.
5. `doctor` via `GET /ready` + descriptor fetch; `[experiment]` extra; docs.
6. Then: MCP tool + standing instruction (C5); client metrics from spans (C8/C9); replay
   primitive `aisquare replay <trace_id> --run <run_id>` (C7, Slice 13).

**Joint smoke (dev environment only — `VIBHUNAVU_ZEPHY`-class hosts produce no measurements)**
- One `prompt_submit` round trip: server ledger row and CLI metric row share
  `(run_id, session_id, trace_id, query_id)`; the fixture drift tests are green in both repos on
  the same fixture bytes.
- One deliberately mismatched request (`contract: 1`) → `unavailable/noop`, recorded on both sides
  as a mismatch, not as baseline.

---

## 8. What is measurable when (unchanged from the plan, restated so nobody quotes early)

Nothing in the right-hand column of the plan's §4 — voluntary MCP use, push-vs-pull usefulness,
turns, tokens, active wall time, exploration avoided, rework, final-code quality — exists until
Slice 13, and unavailable fields are stored `not_measured`, never zero. The CLI's per-turn rows
before the endpoint is live are the **baseline**, and are recorded as `disabled` /
`not_configured`, which are distinct from `unavailable` on purpose.

No comparative claim — "CI helped N %" — until the discrimination self-test exists and the
baseline variance band is published.
