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

**All six at a glance, because the detail below runs long and item 1 alone is
forty lines.** Read the ones you need:

1. **Reinstall non-editable** (§0). Not blocked on anything but typing. Long here
   only because three commands could silently strip your config afterwards.
2. **Governance** — the one real blocker. Needs a credential we do not hold.
3. **Nobody has read the studio.** Every delivery claim stops at the gateway.
4. **Prod values unverified.** Mechanisms verified against staging, not values.
5. **One consent question**, never forced. Nothing breaks by leaving it.
6. **Install the shipping timer** (§5b). Not a blocker, but forever after.


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
   *after* you configure.

   **Same three commands, two different builds, two different verdicts — do not
   read the paragraph above as a statement about the train.** Step 1 closes the
   stale-build case entirely. At the train head the picture is different, and it
   is now measured command by command, because generalising from one of the three
   is a mistake two of us made in opposite directions:

   - `config set` on an unrelated key — **safe**. All five top-level
     `[explainability]` keys and the `[explainability.targets]` table survive.
   - `init --reinit` — **destroyed it silently; now it refuses.** It calls
     `save_config(AppConfig())` unconditionally, and a fresh default config is
     not a merge, so `enabled` went `true` → `false` and the **whole
     `[explainability.targets]` table disappeared** at exit **0**. Preserving
     *unknown* keys — which we do — cannot help, because these are *known* keys
     written at their defaults. It now **refuses** when a reset would discard a
     configured section, names what would go, and points at `--yes`; with
     `--yes` it resets as before and reports what it removed. An *unreadable*
     config still resets without asking, because that is the recovery `doctor`
     sends you here for.
   - `team bind` — **safe at head.** Measured with a control proving the write
     landed (the bound profile is present afterwards): every `[explainability]`
     key and the targets table survive.

   So of the three, only `init --reinit` ever destroyed anything, and it now
   stops and asks. If you pass `--yes` you still lose the section — re-run §1/§2
   afterwards and verify with §5b's `jq -r .shipping.gateway`, because nothing
   downstream reports a missing targets table.
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

6. **Not a blocker, but the one thing you must set up and then keep:** install
   the shipping timer from §5b. Nothing drains the insight spool by itself, so
   without it you deliver whatever was captured before you finish and then
   nothing, forever, while model traffic keeps flowing and `status` reads green.
   Use `ship --strict` in the timer — plain `ship` exits **0** with no key, which
   is right for an interactive run and is exactly why a naive cron line reports
   success for as long as you leave it there. Details in "What is done and
   folded"; it is listed *here* too because a recurring obligation described only
   under "done" gets read as done.

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
  `explainability ship`. **Nothing drains that spool automatically**, by design,
  and this is the one thing here that needs you *after* the cutover rather than
  during it: `ship` runs once, so without a timer you deliver the insights
  captured before you finish and then none ever again — while model traffic
  flows and `status` reads green, because the proxy lane is unaffected. §5b now
  carries a wrapper script and a cron line; use `ship --strict` in a timer,
  which exits non-zero when a run could not ship at all. The plain command
  exits **0** when it has no key, which is correct for an interactive run and
  is why a naive crontab line reports success forever.

The correlation spine: one id in four places — launcher mint → `--session-id`
argv → `X-Pipeline-Id` header → `AISQUARE_PIPELINE_ID` marker → board row via
the `SessionStart` hook → `joins.jsonl`.

Also folded: a doctor that verifies and helps wire it (#51) — plain `doctor`
creates no state, `doctor --fix` creates only the home layout, both measured and
pinned (see the doctrine section); per-role identity resolved from the board
role; redaction in
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
  Confirm which deployment you actually ended up on:
  `aisquare --json explainability status | jq -r .shipping.gateway`
  (This used to be billed as *the* split-brain detector. It is not, any more —
  both lanes now resolve from one target, and across three configurations I
  could not make that value disagree with the active target. It confirms where
  you are pointed; it no longer proves the lanes agree, because they cannot
  currently disagree.)
- **`AISQUARE_AGENT_NAME` collided with the SDK's own routing variable.**
- **A store-migration race** (TOCTOU) under concurrent first opens.
- **`~/.aisquare/credentials` had two writers with incompatible formats, and
  either destroyed the other.** This one is aimed at your machine: that file
  exists here already. `init --api-key` wrote a bare key string as a whole-file
  replace; `serve_token()` read the same path as JSON, treated an unparseable
  file as *no data*, and overwrote it. So `aisquare serve` after `init
  --api-key` silently destroyed the API key — and `serve_token()` is reachable
  in normal use (`serve --show-token` and `run_http`, measured). Both writers now
  go through one read-merge-write helper, and a pre-existing **bare** file is
  *migrated* rather than discarded — because treating it as unparseable is the
  exact reading that lost the data, so a fix that kept it would have preserved
  the bug for every file already on disk.

## What is proven, and by what evidence

Against staging, with real agent processes — not fixtures:

| Claim | Evidence |
|---|---|
| One id in four places | observed in all four simultaneously — **and now pinned**: `tests/test_correlation_spine.py` |
| Per-role separation | 3 roles → 3 distinct trace ids — **pinned** in the same file |
| Proxy lane ingest | 70/70, then 14/14, all `202` |
| Client lane delivery | 6/6 `DISPATCHED`, 0 `dead_letter`, 0 `auth_failed`, read from the SDK's own inbox |
| Client-lane identity | `agent.run_id` = board session id; `agent.name` = `aisquare-coder`, accepted |
| Redaction on the wire | standard **and** strict |
| Token-shape coverage | all 11 vendor shapes survive the JSON→OTel round trip |
| Both lanes, one session | same key carried on both (see the caveat above) |
| Proxy build pinned | `aisquare>=1.1.0`; `_has_valid_correlation` byte-identical to the checkout the receipts used |

Everything in that table stops at the gateway boundary. Nothing in it is a
statement about what the studio shows.

### The fifteen minutes — what is and is not established

The runbook was walked **§0 → §7 in order, in one sitting**, in a throwaway home
(@8dd460fb). **Seven steps executed, four blocked** — §1 and §4b need a
credential nobody holds or mutate shared workspace state, §3 needs the SDK extra
that §0 forbids installing over an editable checkout.

What that establishes, and it is worth having: **every executable step ran in
documented order, none stalled or looped, and no step needed state a later step
creates.** The ordering defect that walk was run to look for does not exist.

**What it does not establish is the number.** The measured total was about seven
seconds of *command* time. Fifteen minutes is a *human* reading, deciding,
typing, and waiting on a proxy and a gateway, and four of the eleven steps did
not run at all. Nobody has walked this as a person, so treat fifteen minutes as
the design target rather than a measurement. **§1 is the budget** — it is a
dashboard task plus a roster POST; the rest are single commands.

**And one caution the walk found that per-step checking could not.** §3 was
blocked, and §5 still reported `✓ explainability proxy: claude_code proxy
healthy`, because another process on this box was serving that port. The check
is right that *a* proxy answered; it cannot tell you *whose*. So a green proxy
row can hide a §3 that silently failed, and model traffic then goes to the older
proxy. §5 now carries the confirming command beside its expected output. This
was invisible to per-step verification because whoever verifies §5 has just done
§3, so the state is always right.

**Two of those rows are stronger than the rest, and the difference matters.**
The first two are now *pinned* — a test fails the gate on the day they stop
being true. Every other row is a **staging measurement taken once**, most of
them before a night of changes to the launcher, the store and the hooks; they
were true when taken and nothing re-checks them. The spine row was in that
weaker category until this shift, and the guard behind it is deliberately built
so it cannot be quietly narrowed: the four places are data, each records its
value *and its origin*, and the claims are pinned by name so a deleted check
fails and an unregistered new one fails too. It does not reach a fixed point —
the guard itself can be removed — but removing a claim now takes two visible
edits in one diff, which reads as a decision rather than an oversight.

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
- A 9p/DrvFs write measurement — parked on the consent question above.

## Doctrine this integration holds to

Worth knowing before you change anything. **Each clause names the test that
pins it** — that used to read "each of these has a test behind it", which was an
assertion rather than a fact, and it was **false for fifteen hours**: the
primary-path clause had no test at all until `9bbc8ed7` measured and pinned it
this shift. Naming the file makes the claim checkable instead of trusted:

- **Fail-open, and say what it cost.** — `test_launch_survives_a_damaged_store.py`, `test_hooks_say_what_failing_open_costs.py`, `test_disable_names_ambient_routing.py`. Tracing may cost a *trace*; it may never
  cost a launch or an exit code — *and when it does fail open it says so on
  stderr*, because a surface that fails open quietly is indistinguishable from
  one that is working. `launch` against a damaged store is the worked example:
  exit **0**, the agent runs, and one line naming what was lost — no board row,
  therefore no join to a gateway Run, one lost trace. The second half of this
  clause is the one that gets dropped; both halves have tests.
- **No hard SDK dependency.** — `test_insight_shipping.py`, `test_explainability_ops.py`.
- **Nothing ships before you configured it.** — `test_insight_sweeper.py`, `test_explainability.py`. No key or config ⇒ nothing
  captured, and nothing logged as an error either.
- **No secret in the repo, on the board, or in a fixture.** — `test_key_never_crosses_deployments.py`, `test_credentials_single_format.py`. Keys are read from a
  named environment variable or `~/.aisquare/explainability-key` (mode 0600),
  never from `config.toml`, and no command prints one. Credential-shaped test
  data is assembled at import time rather than written as a literal — a literal
  fixture was rejected by push protection once, and the bypass was not used.
- **Looking does not create; asking to fix does.** — `test_doctor_does_not_create_state.py`. Plain `doctor` is read-only
  — it leaves a fresh home absent rather than building one. `doctor --fix`
  creates the `~/.aisquare` layout, which is the flag's job, and **nothing
  else**: measured, it does not rewrite `config.toml` on a configured machine
  (byte-identical afterwards), does not install the Claude Code hooks it reports
  as missing (it prints the `agents connect` line and leaves your agent config
  alone), does not prompt so it cannot hang a script with no terminal, and still
  exits non-zero for whatever it could not repair. Pinned in
  `tests/test_doctor_does_not_create_state.py`.
- **Never a millisecond on the primary path either.** — `test_no_network_on_the_primary_path.py`, pinned by *mechanism* (no socket is opened) rather than by a clock, because a wall-clock bound in CI is flaky by construction and a muted test is worse than none. Measured on this box: a configured-but-dead proxy costs the hook nothing.
- **Diagnostic commands do not create state; ordinary commands may.** `status`
  **does** create and migrate the store, and the runbook depends on that at §0b
  and again in wedge recovery — do not "fix" it. (The *migrate* half is
  unpinned; it also cannot fire at this cutover, because this RC adds no
  migration — `_MIGRATIONS` has ten entries on `main@ce6bc46` and ten here.)
  **If that store is ever wedged, stop and fix it before anything else** — the
  recovery is in §0b of the runbook, measured end to end, and it **empties the
  board**: every session, task and note. Nothing about explainability lives in
  that file, so your config, targets and key survive it.
- **A wedged store is loud; a TRUNCATED one is not, and it is already too
  late.** A zero-length `context.db` is read by SQLite as a brand-new database,
  so it is silently re-created and `doctor` reports it green while every
  session, task and note it held is gone. The tell is an **empty board with a
  healthy `doctor`** — that is truncation, not corruption, and unlike a wedge
  there is no recovery, because the data is gone rather than unreachable.
  Explainability config, targets and key survive it exactly as they survive a
  wedge. The CLI now says so on the first open that sees the empty file; that
  is the only moment it is knowable, because one line later the schema is back.
