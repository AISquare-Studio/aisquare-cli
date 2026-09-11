# The tracing boundary: a Run is a process, not an agent

Read this before you design an experiment on this data, and before you read a
number off a dashboard and believe it.

**The rule, in one line: identity rides in process-level environment, so the
unit of attribution is the operating-system process.** Everything below follows
from that one fact.

Evidence tags follow the runbook's convention: **[verified-stg]** was executed
against staging and produced receipts, **[verified-train]** is pinned by the
test suite, **[unverified]** is reasoning nobody has run.

---

## What you can attribute

| Question | Answer | Evidence |
|---|---|---|
| Which agent produced this Run? | Yes — one identity per process | **[verified-stg]** |
| Which session does this Run belong to? | Yes — one Run per session | **[verified-stg]** |
| Did these three roles run concurrently and separately? | Yes — three distinct trace ids | **[verified-stg]** |
| How many Task subagents did this session fan out to? | Yes — countable `Tool:Agent` spans | **[verified-stg]** |
| Which of those subagents made this LLM call? | **No.** Not recoverable | **[verified-stg]** |
| How many agents did this Workflow run? | **No.** Not recoverable | **[verified-stg]** |

**[verified-stg]** (runner `d124bc26`, board seq 21981; reproduced twice.) Three
concurrently live roles through one proxy produced three distinct trace ids with
correct agent names and 70/70 ingest `202`. A session that spawned three Task
subagents produced **one** pipeline-session, **one** trace id and **one** `AGENT`
span; the subagents' own LLM spans hang flat off the root with `agent.name` null
on every non-root span. A Workflow is strictly worse: one opaque
`Tool:Workflow` span for the whole workflow, and even the fan-out count is gone.

## Why — the mechanism, not a limitation of the dashboard

A session joins the trace by carrying two variables in its process environment:

- `ANTHROPIC_BASE_URL` — routes the session's model traffic through the proxy
- `ANTHROPIC_CUSTOM_HEADERS` — carries `X-Agent-Name` (the studio identity) and
  the Run's correlation: `traceparent` when the launcher owns the Run (the
  default — it posted the Run's root span first and names it here), or
  `X-Pipeline-Id` when it could not (no gateway, no key, root refused), in
  which case the proxy keys the Run itself

Those two names are **reserved**. If a launch finds either already set it
stands down and runs the session untraced rather than seize routing you own,
with the reason on stderr. That is deliberate — but note where such a value can
come from. `aisquare team bind <role> --env ANTHROPIC_BASE_URL=…` writes the
binding into `config.toml`, so unlike a variable exported in one shell it
survives every shell and applies to **every** launch of that role until it is
unbound. One line of config can leave a seat permanently untraced, with a
single dim line per launch as the only signal. Bind whatever else a seat needs
— `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR` are the per-seat idiom and do
not touch tracing — and point sessions at a proxy with
`aisquare explainability enable --proxy-url …` instead.

An in-process agent — a Claude Code Task subagent, a Workflow step — is not a
new process. It inherits that environment verbatim, byte for byte, because it
*is* the same process. There is no point at which a different identity could be
attached, so the proxy correctly sees one caller and records one Run. Nothing is
being lost in transit; there was never a second identity to lose.

Separation therefore requires a separate process with its own header pair. That
is exactly what `aisquare launch` and `aisquare team spawn` do, which is why
per-role numbers are real.

## If you are designing an experiment

1. **Spawn one process per agent you intend to measure**, through the CLI's
   spawn seam. That is the only construct that yields a distinguishable
   identity.
2. **Reserve in-process Task and Workflow for work you do not need to
   attribute.** They are not weaker tracing — they are outside the boundary.
   Their fan-out is visible for Task (count the `Tool:Agent` spans) and invisible
   for Workflow.
3. **Do not compute a per-subagent metric.** There is no per-subagent data to
   compute it from; a query that appears to return one is reading root-level
   spans and attributing them to whichever subagent you assumed. This is the
   failure mode this page exists to prevent, because it produces a plausible
   number rather than an error.
4. **Read per-role and per-session numbers as real.** Those are the verified
   units.

## Two lanes, one Run

Model traffic reaches the gateway through the proxy. The insights the CLI itself
holds — human prompts, board notes, task claims — never touch the model API, so
no proxy can see them; they travel separately. Both key the Run on the same
`X-Pipeline-Id`, which is what puts a session's model traffic and its human and
board activity in one place. See the correlation spine in
`aisquare/services/explainability.py`.

**[verified-prod]** (workspace 881, 2026-09-09.) This was **false** as built:
a session that used both lanes produced TWO Runs — `5efb96de…` with the model
traffic and `6fb49942…` with the client lane, same agent name, same
`X-Pipeline-Id`. The gateway keys a Run by OTel `trace_id`, and the two lanes
never agreed on one: the proxy minted a random trace per pipeline session, and
the shipper's `AgentRunTracer` opened another. `X-Pipeline-Id` was an attribute
on each root, not the key.

Fixed by making the pipeline id the SOURCE of the trace id
(`trace_identity`: SHA-256, first 16 bytes trace id, next 8 root span id). The
launcher posts the Run's root span with those ids before the agent starts and
hands the proxy a `traceparent` naming it; the shipper attaches its spans under
that same root. Re-measured after the fix: one Run per session, both lanes in
it. `joins.jsonl` now records that `trace_id`, and it is the id
`GET /v1/workspaces/{ws}/runs/{run_id}` reads back with the workspace key.

## Known limitation: a Run's status and its end time **[unverified]**

The launcher posts the Run's root span **already ended** — `start_time ==
end_time`, `duration_ms` 0.0, no status object (`explainability_ops.open_run_root`).
What the gateway then makes of that root is written down here so the next reader
does not have to re-derive it across two repositories.

In the gateway (`AISquare-Explainability-SDK`):

- `worker/structural.py` derives `is_terminal_batch` from "this batch holds a
  parentless span with an end time". The launcher's root-only batch satisfies
  it, so that batch's `run_status` is `completed`.
- Every later batch of the same trace — the proxy's model spans, the client
  lane's segment — is child-only, therefore not terminal, therefore `running`.
- `graph/queries.py`'s `MERGE_RUN` sets `r.status = $status` outright on
  `ON MATCH`, where its neighbours coalesce. The LAST batch wins.

So an owned Run reads `completed` while it is only a root, flips to `running` on
the first proxy batch, and stays there. `end_time` is
`coalesce($end_time, r.end_time)` and a child-only batch carries no root to take
one from, so it stays frozen at the launch instant.
`gateway/otlp/routes.py:307-311` documents this exact outcome — "stuck at
running FOREVER. Measured." — as the reason that route stopped sending its root
first. Nothing on the CLI side corrects it: `set_status("completed")` at the end
of a drain lands on the client-lane segment, not on the root. Duration survives,
because `run_metrics["duration_ms"]` is computed from the min/max span range
rather than from the root's own zero.

This is parity with a proxy-keyed Run, which has always looked this way, so it is
not a regression for the model lane. It IS a regression against the pre-fix
client-lane Run, whose `AgentRunTracer` root closed last with OK.

**The fix belongs on the gateway side, and none of it is implemented here.**
Either the gateway derives the verdict from the last child rather than from the
presence of an ended root, or the root is closed at session end — and the second
is only half a CLI change, since the launcher has `execve`'d away by then, so it
would fall to the `SessionEnd` hook re-posting the root with a real end time as
the trace's last batch. Which one is right is a gateway decision, and it was
deliberately deferred rather than guessed at.

The **[unverified]** tag is load-bearing. The live check after the one-Run fix
read **tokens and node presence** — one Run per session, model spans and the
prompt span under one trace id. Nobody has read `status` or `end_time` back off
a Run and compared them to the session that produced it, so the question above
is open in both directions: read duration and tokens, and treat a Run's status
and end time as unverified until someone does.

## Two mechanisms people will suggest, and their status

Both are **[unverified]**. Neither is an option today; do not plan around them.

- **Per-subagent header override** — having each in-process subagent set its own
  `ANTHROPIC_CUSTOM_HEADERS`. Nobody has shown that the harness exposes a seam
  where this could be attached per subagent.
- **Proxy-side prompt fingerprinting** — inferring the sub-agent from request
  content. Nobody has shown this distinguishes subagents reliably, and an
  inferred identity in a dataset used to measure identity is worse than none.

## Checking it yourself

The claim above is about the environment of the process being launched. To see
what a launch would actually carry:

```bash
aisquare explainability status          # is tracing on, is the proxy healthy
aisquare explainability env <role>      # the exact env delta, printed
```

A Run whose spans all carry one `agent.name` is a correctly traced process. A
Run you expected to contain several agents contains one because it was one
process.
