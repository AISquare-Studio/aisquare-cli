# CI hook contract — version 1

The wire protocol between the `aisquare` CLI and the CI experiment endpoint.
Both sides build against this document and against the golden fixtures in
[`tests/fixtures/ci_contract/`](../tests/fixtures/ci_contract), which are
executable: `tests/test_ci_contract.py` fails if this build and those fixtures
drift apart.

The CLI-side implementation is [`src/aisquare/services/ci_contract.py`](../src/aisquare/services/ci_contract.py).

> **Status.** The contract is frozen at version 1. The endpoint is not live
> yet; the client is written against these fixtures and a local stub. See
> [Bilateral decisions](#bilateral-decisions) for the questions that need an
> answer from the server side before integration.

## Endpoint

```http
POST {AISQUARE_CI_URL}/v1/hook
Authorization: Bearer {AISQUARE_CI_KEY}
X-CI-Contract: 1
Content-Type: application/json
```

### Header casing — read this before matching on it

The client sends the contract header over `urllib`, which **title-cases header
names**. `X-CI-Contract` goes out on the wire as:

```
X-Ci-Contract: 1
```

HTTP field names are case-insensitive and any correct server handles this, but
a server matching the literal string `X-CI-Contract` will see no contract
version at all — and then guess, which is the one thing this header exists to
prevent. Match case-insensitively. Pinned CLI-side by
`test_the_contract_header_arrives_case_normalised`.

## Request

```json
{
  "trigger": "session_start" | "prompt_submit" | "tool_intercept" | "agent_request",
  "session_id": "ses_01k…",
  "trace_id": "trc_01k…",
  "project_id": "prj_01k…",
  "budget_ms": 400,
  "run_id": "r-20260819-0134",
  "arm": "B",
  "snapshot_ref": "refs/aisquare/wip/trc_01k…",
  "prompt": "…",
  "tool": {"name": "Grep", "args": {}}
}
```

Nulls are sent explicitly rather than omitted. `prompt` is populated on
`prompt_submit` only, `tool` on `tool_intercept` only; `run_id`, `arm` and
`snapshot_ref` are null outside an experiment.

`trace_id` identifies **one turn**, not one session — a session spans many
turns, and keying on `session_id` would collapse them into a single trace and
make per-turn comparison impossible.

## Response

```json
{
  "contract": 1,
  "action": "inject" | "substitute" | "allow" | "noop",
  "context": "…markdown block…",
  "tool_result": "…",
  "provenance": [{"node_id": "…", "source": "…"}],
  "flags_applied": ["ci_retrieval", "graphify"],
  "server_ms": 118,
  "cache_hint": {"ttl_s": 900, "key": "…"}
}
```

`server_ms` is required for any response you want counted. Round-trip minus
`server_ms` is the network cost; without it the two fold together and a slow
link is indistinguishable from a slow server.

## Timing — deviates from the original spec

The §02 draft made `budget_ms` **a hard client-side deadline**: exceed it,
treat as allow. That is reversed here.

| | Draft §02 | Contract v1 |
| --- | --- | --- |
| `budget_ms` | hard client deadline | **advisory**, declared to the server |
| Client enforcement | 400 ms | **10 s backstop only** |
| Who owns shedding | client | **server** |

The reason is that a tight client clamp converts a latency problem into
silently absent data. At 400 ms, a server needing 300 ms over a 100 ms network
fails open on most calls — and a failed-open call records `action: "allow"`,
which is exactly what a healthy server returns when it has nothing to add. The
experiment would then measure nothing while appearing perfectly healthy.

So: **shed load server-side, where there is enough information to shed the
right thing.** The client keeps only a backstop against a hung endpoint holding
a developer's prompt hostage.

Ten seconds is not a latency target — it is a crash guard. It sits under Claude
Code's 30 s `UserPromptSubmit` cancellation so the hook always returns a
decision of its own; past 30 s the hook's output is discarded by the agent and
the call degrades with *no reason recorded at all*.

**`prompt_submit` is synchronous.** The developer has hit enter and is watching
a cursor. Every millisecond spent here is one they wait. Treat a high
`backstop_exceeded` rate as a server-side bug, never as an experimental result.

## Degradation

Any failure resolves to `action: "allow"` — the session continues untouched.
The CLI never raises on this path; there is no response it can receive that
does.

Because a degraded call and a deliberate `allow` are the same action, the CLI
records a **`degradation_reason`** beside every call. Aggregates that mix the
two are measuring plumbing, not retrieval.

| Reason | Cause |
| --- | --- |
| `none` | The server answered and this build understood it |
| `not_configured` | No endpoint configured — the default. No request made |
| `disabled` | Configured but switched off. No request made |
| `transport_error` | Connection refused, DNS, TLS, reset |
| `backstop_exceeded` | 10 s elapsed. A server-side bug |
| `http_error` | Status other than 200 |
| `malformed_body` | Not JSON, or JSON but not an object |
| `contract_mismatch` | `contract` names a revision this build does not speak |
| `unknown_action` | `action` absent or outside the four |
| `schema_mismatch` | Known action, but a field failed validation |

Checks run in that order deliberately. Contract is verified **before** action,
and action **before** full validation, so a skewed server reports the skew —
which an upgrade fixes — rather than a schema error, which implies a bug in one
of the two builds.

## Enablement — off by default

CI is opt-in and ships disabled.

| Knob | Default | Meaning |
| --- | --- | --- |
| `AISQUARE_CI` | *off* | Master switch. Must be explicitly enabled |
| `AISQUARE_CI_URL` | unset | Endpoint. Unset ⇒ `not_configured`, no request |
| `AISQUARE_CI_KEY` | unset | Bearer token |

With the master switch off — the state every existing user is in — the client
issues no request, adds no latency, and records `disabled`. That is what makes
this safe to land on `main` before the endpoint is live.

The `ci_push` / `ci_pull` flags described in T4 are **sub-flags of this master
switch**. The task list says both default true; that is true only once someone
has opted in.

## Phase 1 is push-only

The contract carries four triggers. Phase 1 implements **two**:

| Trigger | Phase 1 | Notes |
| --- | --- | --- |
| `session_start` | ✅ | Warms the cache |
| `prompt_submit` | ✅ | The augmentation path |
| `tool_intercept` | ❌ | Needs `PreToolUse` (T2, Phase 2) |
| `agent_request` | ❌ | No consumer specified anywhere yet |

This matters for reading early results: **injection makes the agent better
informed; it does not stop it grepping out of habit.** Exploration calls
avoided — the headline metric — needs interception, which is Phase 2. Do not
expect Phase 1 numbers to show it.

### On `substitute`

`substitute` is in the contract so the server can express the intent, but it
has no working delivery mechanism today. Claude Code's `PreToolUse` hook
**cannot fabricate a tool result**. It exposes `permissionDecision`
(`allow`/`deny`/`ask`), `permissionDecisionReason` and `updatedInput` — and a
call carrying `updatedInput` still executes.

The nearest approximation is `deny` with the payload in
`permissionDecisionReason`, which does reach the agent and does prevent the
real tool running — but arrives framed as a refusal rather than as a result,
which is a confound for any experiment measuring tool behaviour. Settle this
before wiring it.

## Before you compare anything

No comparative claim — "CI helped N%" — until the T7 discrimination self-test
exists and the baseline variance band is published. Phase 1 is plumbing and
nothing is being compared, but numbers become visible and tempting the moment
the endpoint goes live. An effect smaller than the noise floor is not an
effect, and the noise floor is not known until it is measured.

## Bilateral decisions

Open questions that cannot be resolved by one side alone. Answer here rather
than in either team's private notes — this file is the seam.

### 1. `run_id` ownership — *proposed, needs confirmation*

The CLI **never mints `run_id`**. The format in §02 (`r-20260819-0134`) is the
server's, and a client generating its own would fork the run space silently.
The CLI sends `null` until a server response tells it otherwise.

**Needed:** how does a `run_id` first reach the CLI? Options: returned in a
`session_start` response and held for the session, or pushed via run config.
Until this is answered, every Phase 1 metrics row carries a null `run_id` and
cannot be attributed to an experiment.

### 2. `cache_hint.key` scoping — *blocking the prefetch*

`session_start` warms the cache; `prompt_submit` reads it. **If the two are
scoped differently, the warm is dead weight and every prompt pays a full
synchronous round trip** — the exact cost this design is trying to avoid.

The CLI treats `cache_hint.key` as opaque and honours whatever it is given, so
this is entirely a server-side decision. Session-scoped is cheaper and simpler;
prompt-scoped is more precise and may lift the hit rate enough to pay for
itself. Either is fine — but they must agree.

**Needed:** confirmation that a `session_start` warm produces keys a subsequent
`prompt_submit` can hit. There is a CLI-side fixture asserting warm-then-read
is a hit; it is written against our assumption, and will need updating if
yours differs.

### 3. Contract version bumps

Any change to a field's type or meaning is a version bump, not an edit. This
build degrades to `allow` on an unrecognised `contract`, so a silent change
does not fail loudly — it produces a run where every call quietly did nothing.

**Needed:** agreement that the server never ships a schema change under
`contract: 1`, and a channel for announcing a bump before it deploys.

### 5. Cache lifetime vs. run boundaries — *proposed*

The CLI caches per **session**, under the server's `cache_hint.key`, and sweeps
session files older than 24 h. It has no notion of a run ending, so a long
session spanning an arm switch would serve entries minted under the previous
arm.

**Proposed:** any response whose `arm` differs from the request's is not
cacheable, and the server omits `cache_hint` on it. Cheaper than teaching the
client about arms, and keeps arm assignment entirely server-side.

**Needed:** confirmation, or a different rule. Until then, do not switch arms
inside a live session.

### 4. `arm` opacity

`arm` is opaque to the CLI by design — branching on it client-side would put
experiment logic where changing it costs a release. The CLI records it and
sends it back, nothing more.

**Needed:** confirmation that arm assignment is entirely server-side, including
for replay (T6), where the CLI will need to *request* a specific arm rather
than be assigned one. That is the one place the opacity has to bend.
