# The planner findings loop: find → fix, fed by the product

Jatin's ask, verbatim: *the planner uses the explainability integration every
iteration to find and fix.*

The loop has two halves. **The write half is done and proven** — every traced
session opens a Run keyed by an id the board also knows, so a finding on a Run
can be traced back to the session, the role, and the task that was open at the
time. **The read half is blocked on one credential**, and this page is what
makes that a five-minute unblock instead of a morning of discovery.

Status as of the night of 2026-08-17:

| Half | State |
| --- | --- |
| Planner's own sessions traced | Ready — `aisquare launch planner` with tracing enabled, same as any role |
| Runs joinable to board rows | **Proven with real agents** (one id in four places; see below) |
| Planner reads findings back | **Blocked** — needs a read-scoped credential |

---

## 1. What is blocked, exactly

Our workspace key is **write-only for studio-scoped data, by design**. That is
not a guess and it is not about a misconfigured studio id — both hypotheses
were tested and falsified:

- `GET /v1/studios` **succeeds** and lists 16 studios (ids 144–169).
- `EXPLAINABILITY_STUDIO_ID` is pinned to `21`, which is **not** among them.
  Studio `169` **is**, and 169 is where `register-roster` put
  `aisquare-planner` / `aisquare-coder` / `aisquare-runner`.
- But **every** studio-scoped `GET` returns `403 {"detail":"Studio ID
  mismatch"}` — for all sixteen, including 169, including `/my-capabilities`
  and `/sdk-status`, and unsetting the pin changes nothing.

So correcting the pin cannot unblock this, and it cannot fix governance
either: the policy path needs the same missing credential class. The
`/v1/routing/resolve` check is unaffected — that route is not studio-scoped and
still answers.

### The ask, in one line

> A **read-scoped credential** for the studio our Runs land in — viewer-grade,
> not `EXPLAINABILITY_ADMIN_API_KEYS`, least privilege — plus confirmation of
> which studio id that is.

Append to `/home/work/.config/aisquare/explainability-stg.env` (mode 600, never
committed):

```sh
EXPLAINABILITY_READ_API_KEY=…      # viewer-grade, read-only
EXPLAINABILITY_READ_STUDIO_ID=…    # the studio our Runs actually land in
```

Two new names rather than reusing the existing pair, because they have
different privileges and different blast radii, and a loop that reads should
not be holding a key that can write.

---

## 2. The read path, and why it is driven from OUR side

Every route below was confirmed to **exist** in the live stg OpenAPI spec.
None of them has been **executed** — that is precisely what the credential
unblocks. Treat the shapes as verified and the responses as unseen.

The obvious design — poll the gateway for "runs since last cycle" — does not
work and does not need to. `GET /v1/studios/{studio_id}/runs` takes only
`limit`; there is no `since`. But we do not need one, because **we already
know every Run we started**: one line per traced session is appended to
`~/.aisquare/explainability/joins.jsonl` by the hook inside the agent — the one
place that holds both halves. Each row carries exactly these fields:

| field | what the loop does with it |
| --- | --- |
| `started_at` | the cursor — read rows newer than your last cycle |
| `pipeline_id` | the Run key; what you resolve against the gateway |
| `session_id` | the board row id; quote it in every task you file |
| `agent_name` | the studio identity the Run was filed under |
| `role` | who to route the fix to |
| `cwd` | which checkout it ran in |

So the loop is driven by our own join log and queries the gateway **per Run**:

```
joins.jsonl  ──(started_at > last cycle)──►  pipeline_id
      │
      ▼
GET /v1/studios/{studio}/by-agent-run-id/{pipeline_id}     → the gateway's run_id
      │
      ├─► GET /v1/studios/{studio}/runs/{run_id}/rml/v3/findings
      └─► GET /v1/studios/{studio}/policy/violations?run_id={run_id}
```

`by-agent-run-id` is the important one: it is a first-class route that looks a
Run up **by the id we chose**, which is exactly what the correlation spine was
built to make possible. The board row, the proxy's `X-Pipeline-Id` and this
lookup key are all the same value.

Other findings surfaces that exist, for when the first two are exhausted:
`/v1/studios/{id}/insights`, `/praxis/insights`, `/failure-clusters`,
`/injection-detections`, `/pii-violations`, `/outcomes`.

### Two things to check on the first real call

1. **Whether the gateway merges by `agent.run_id` the way the proxy merges by
   `X-Pipeline-Id`.** Designed for, not proven. A session whose spans arrive by
   both paths — proxy model traffic *and* `aisquare explainability ship` —
   should be **one** Run, not two.
2. **The rows where `session_id != pipeline_id`.** On a launch we could not
   pin — a wrapper binary, `--resume`, `--continue` — the agent keeps its own
   session id and the Run is keyed by the one we minted, so the two columns
   differ. Those rows are still fully joinable, which is why the join is
   written by the hook inside the agent rather than by the launcher. **Query
   `by-agent-run-id` with `pipeline_id`, never with `session_id`** — using the
   board id would silently miss exactly these Runs, and they are the ones a
   wrapper-bound or resumed role produces.

---

## 3. The loop step to add to the planner prompt

Drop this in as one more numbered step in the planner's cycle, after reading
the board and before dispatching work.

> **(N) CONSUME EXPLAINABILITY.** Read
> `~/.aisquare/explainability/joins.jsonl` for sessions started since your last
> cycle, **deduped on `(session_id, pipeline_id)`** — the log records session
> STARTS, so one session that was `/clear`ed or resumed appears more than once
> and would otherwise be triaged twice. For each distinct `pipeline_id`,
> resolve the Run
> (`by-agent-run-id`) and pull its RML findings and policy violations. Triage
> every finding into exactly one of three outcomes, and say which:
> **REAL** → `aisquare task add` with the run id, the board session id and the
> finding quoted in the contract, routed to a coder; the fix closes with a
> receipt as any other task does.
> **NOISE** → record it as noise on the board with the rule that fired, so SDK
> calibration has a corpus rather than an anecdote.
> **UNCLEAR** → say so and leave it; an unclear finding triaged as real spends
> a coder's cycle on a guess.
> Never fix it yourself — findings become tasks, coders fix, receipts close the
> loop. That is the same shape the board already runs on; the only new thing is
> where the work comes from.

Two properties worth keeping when this is edited:

- **Cursor by `started_at`, not by "last N runs".** A quiet cycle must read
  nothing rather than re-triage the same Runs, or the noise corpus fills with
  duplicates and every cycle costs more than the last.
- **Dedupe on `(session_id, pipeline_id)` before resolving anything.** The
  rows are observations, not state: the hook appends on every session START,
  so a `/clear` or a resume writes a second row for a session already triaged.
  The cursor alone does not catch it, because the second row has a *newer*
  `started_at` — it looks like new work and is not.
- **A finding names its session, not just its Run.** The board session id is
  what lets a reader jump from a finding to the notes, claims and prompts that
  were live when it happened. That join is the whole point of the spine, and a
  task that quotes only the run id throws it away.

---

## 4. What is proven, so the loop is not built on hope

Real `claude` sessions, real proxy, on the night of 2026-08-17:

- `aisquare launch coder -p …` minted `e879aa6a-…`; the proxy opened
  `pipeline_id=e879aa6a-… trace_id=db0346159225927a`; the **board row id** was
  `e879aa6a-…`; the join-log row had `session_id == pipeline_id`. One id in
  four places.
- A second role produced its own Run and its own row.
- 14/14 `POST /v1/traces/ingest` → `202 Accepted`.
- Every one of those sessions ran **ungoverned**: the proxy log shows
  `POST /v1/studios/21/policy/check/output → 403` then
  `policy check degraded (FAIL_OPEN)`. Governance needs the same credential
  work as the read half.

Nobody has yet **seen** one of these Runs in the Studio. That is the other
thing the credential buys.
