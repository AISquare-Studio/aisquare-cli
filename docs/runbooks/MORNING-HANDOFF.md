# Morning handoff — explainability integration, `rc/v2026.08.18`

**Read this first, then `explainability-prod-cutover.md` to execute.** This file
answers four questions: what is done, what is *proven* and how, what needs you,
and what was deliberately left. It assumes you have read none of the board.

Why it is a file and not a board note: the handoff was a note for most of this
shift, and notes scroll. Late in the shift the planner tried to retrieve the
previous handoff and **could not** — `aisquare log` carries prompts, not notes,
and `aisquare board` is a windowed view. A handoff that expires is not one.

How current is this file? Ask git rather than trusting a line in it —
`git log -1 --format='%h %ad' -- docs/runbooks/MORNING-HANDOFF.md`. It used to
name a train head, which @9bbc8ed7 pointed out is *structurally* always one
commit behind, because a file cannot name the commit that folds it. A stale
number is worse than no number when the whole point is telling you how stale
something is.

---

## What needs you — in order, blockers first

1. **Step 1 of the runbook (§0, the non-editable reinstall) has not been run.**
   Nothing blocks it but the typing; the command was verified working at the
   current head. Until it runs, `aisquare` on `PATH` is the pyenv build and
   `resolve_binary` reports 0.

   **And it is not only about what you gain by running it.** Once you *have*
   configured explainability, three commands run from a shell that still
   resolves to the stale binary silently strip that configuration: `team bind`,
   `config set`, and `init --reinit`. They exit **0** with no warning — measured
   — because the stale build's config model has only three explainability
   fields, so it drops `target`, `roles`, `ship`, `gateway_url` and the whole
   `[explainability.targets]` table on every write. The three keys a stale
   `config set` will *accept* are exactly the three that *survive*, which is why
   nothing looks wrong afterwards.
   The cutover itself cannot be half-done this way — `explainability enable`
   does not exist on the stale build and exits 2 — so the danger is entirely
   *after* you configure. Step 1 closes the whole class, and that half is
   measured too rather than assumed: at the train head, `config set` on an
   unrelated key preserved all five top-level `[explainability]` keys **and**
   the `[explainability.targets]` table (with a control confirming the write
   actually landed — the first run of that check silently wrote nothing).
   Two tells that need no comparison: `doctor` reports where the running build
   came from, and a build that prints **no** provenance line predates that check
   and is therefore older than this train.

2. **Governance is the one real blocker, and it is not a config edit.**
   No agent name resolves to a studio. Fixing it needs *both* an agent→studio
   binding *and* a credential class we do not hold. Measured: `GET /v1/studios`
   with the workspace key succeeds and lists 16 studios (144–169), yet **every**
   studio-scoped call 403s for **all sixteen** — including `169`, which is the
   one the roster registration returned. Unsetting the stale
   `EXPLAINABILITY_STUDIO_ID=21` pin changes nothing. Until this is resolved,
   runs are **traced but ungoverned** — enforcing nothing.

3. **Nobody has read the studio.** Every delivery claim in this repo stops at
   *the gateway accepted the bytes*. Two tasks are blocked on a read-scoped
   credential (`tsk_01kzdee4pjw8e0ep2g968ejsq6`,
   `tsk_01kze9s8w1n6nmctyr83an5kpt`). See "the one thing to eyeball" below.

4. **Prod values are unverified.** Every *mechanism* here is verified against
   staging; the prod URL and key are `[unverified-prod]` throughout.

5. **One consent question, asked five-plus times and never forced:** may we
   write to a `/mnt` (Windows) path? A 9p/DrvFs filesystem measurement is
   parked on it. Nothing is broken by leaving it parked.

---

## The one thing to eyeball, stated precisely

Open a single session's Run in the studio and check that it contains **both**
the model traffic **and** the `HumanIntervention`/`Decision` spans.

Be precise about what is and is not established, because the difference is
one word. We proved that **we send the same key on both lanes** — the proxy
sends it as `X-Pipeline-Id`, the client sweeper sends it as `agent.run_id`, and
both were observed carrying the same value in one session. We did **not** prove
that the gateway *unifies* them into one Run on that key. That is designed-for,
not demonstrated, and demonstrating it requires reading the studio.

Counts cannot answer this and neither can the CLI. `2 sent` reads identically
whether the two spans landed in one Run or two.

---

## What is done and folded

Two lanes, both wired end to end:

- **Proxy lane** — model traffic via `ANTHROPIC_BASE_URL`. The env dict carrying
  that variable is built *only on the healthy path*, so a rejected proxy is
  never routed to.
- **Client lane** (#50) — CLI insights spooled, then shipped by
  `explainability ship`.

The correlation spine: one id in four places — launcher mint → `--session-id`
argv → `X-Pipeline-Id` header → `AISQUARE_PIPELINE_ID` marker → board row via
the `SessionStart` hook → `joins.jsonl`.

Also folded: a doctor that verifies and helps wire it (#51) and does **not**
create state; per-role identity resolved from the board role; redaction in
standard and strict modes, credentials-only by default, scrubbed *into* the
spool rather than on the way out; an inventory of all 13 process-spawning seams,
each ruled `TRACED` or `EXCLUDED` with a stated reason and held by an AST guard;
atomic config writes; and a write-boundary guard that follows imports, aliases,
and rebindings.

### Bugs found and fixed that would have bitten you specifically

- **Nested-session identity theft.** A child session inherited its parent's
  headers while printing "launching untraced".
- **`$'…'` exports were bash-only.** Under `sh`/`dash` the launch did not
  degrade — it *died*. Now POSIX single-quoted and re-verified under `dash`.
- **Split brain across the two lanes.** Configuring shipping with a staging
  shell sourced and then switching to prod moved model traffic to prod while CLI
  insights kept going to staging — and `status` printed the prod gateway,
  because that line resolved the target and the shipping line did not. Both
  halves looked healthy and nobody was told. One target switch now moves both
  lanes, and if the target names a key variable that is not set, shipping
  **refuses by name** rather than quietly using a stored one.
  The only check that can detect a recurrence:
  `aisquare --json explainability status | jq -r .shipping.gateway`
- **`AISQUARE_AGENT_NAME` collided with the SDK's own routing variable.**
- **A store-migration race** (TOCTOU) under concurrent first opens.

## What is proven, and by what evidence

Against staging, with real agent processes — not fixtures:

| Claim | Evidence |
|---|---|
| One id in four places | observed in all four simultaneously |
| Per-role separation | 3 roles → 3 distinct trace ids |
| Proxy lane ingest | 70/70, then 14/14, all `202` |
| Client lane delivery | 6/6 `DISPATCHED`, 0 `dead_letter`, 0 `auth_failed`, read from the SDK's own inbox |
| Client-lane identity | `agent.run_id` = board session id; `agent.name` = `aisquare-coder`, accepted |
| Redaction on the wire | standard **and** strict |
| Token-shape coverage | all 11 vendor shapes survive the JSON→OTel round trip |
| Both lanes, one session | same key carried on both (see the caveat above) |
| Proxy build pinned | `aisquare>=1.1.0`; `_has_valid_correlation` byte-identical to the checkout the receipts used |

Everything in that table stops at the gateway boundary. Nothing in it is a
statement about what the studio shows.

## What was deliberately left

Named so you can tell a decision from an oversight:

- `root_package_shadowed()` is a safety net wired to nothing — deletion is
  reserved for the next train (`tsk_01m07k2vr7avjmg63bcbgc2r3z`).
- The write-boundary guard follows imports, aliases and name rebindings but
  **not** attribute, dict, or closure rebindings. Open by choice: a
  dynamically-built attribute is genuinely not statically resolvable, and the
  guard over-approximates in the safe direction. Measured on this tree: the
  over-approximation adds **no** spurious members (closure 16 either way).
- `EROFS`/`ENOSPC` on config writes are exercised only via injected exceptions,
  never on a real read-only or full filesystem.
- `doctor --fix` state creation is unmeasured.
- A 9p/DrvFs write measurement — parked on the consent question above.

## Doctrine this integration holds to

Worth knowing before you change anything, because each of these has a test
behind it:

- **Fail-open.** Tracing may cost a *trace*; it may never cost a launch or an
  exit code.
- **No hard SDK dependency.**
- **Nothing ships before you configured it.** No key or config ⇒ nothing
  captured, and nothing logged as an error either.
- **No secret in the repo, on the board, or in a fixture.** Keys are read from a
  named environment variable or `~/.aisquare/explainability-key` (mode 0600),
  never from `config.toml`, and no command prints one. Credential-shaped test
  data is assembled at import time rather than written as a literal — a literal
  fixture was rejected by push protection once, and the bypass was not used.
- **Diagnostic commands do not create state; ordinary commands may.** `doctor`
  is read-only. `status` **does** create and migrate the store, and the runbook
  depends on that at §0b and again in wedge recovery — do not "fix" it.
