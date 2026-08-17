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
  `X-Pipeline-Id` (the Run key)

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

**[unverified]** That the gateway merges spans arriving by both paths into a
single Run is designed for and not yet demonstrated end-to-end against staging.
Until someone has seen one Run rather than two for a session that used both,
treat it as an assumption.

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
