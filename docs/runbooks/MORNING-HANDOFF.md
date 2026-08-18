# Morning handoff — explainability integration, `rc/v2026.08.18`

**Read this first, then `explainability-prod-cutover.md` to execute.** This file
answers four questions: what is done, what is *proven* and how, what needs you,
and what was deliberately left. Two further sections are not questions and are
easy to miss from this list: **the one thing to eyeball**, which is the only
thing here that asks you to look at something outside this repo, and **the
doctrine this integration holds to**, which is what to read before changing any
of it. It assumes you have read none of the board.

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

**All seven at a glance, because the detail below runs long and item 1 alone is
forty lines.** Read the ones you need:

1. **Reinstall non-editable** (§0). Not blocked on anything but typing, and it
   is a **precondition**, not just a hazard-avoidance step: it fixes a command
   that is broken on this machine *today*, and three of the checks these documents
   tell you to run cannot answer until it has happened. Long here because it also
   closes a way three commands could silently strip your config afterwards.
2. **Governance** — the one real blocker. Needs a credential we do not hold.
3. **Nobody has read the studio.** Every delivery claim stops at the gateway.
4. **Prod values unverified.** Mechanisms verified against staging, not values.
5. **A proxy already answers §3 on this box**, more than a day old. §3 can pass
   without you.
6. **One consent question**, never forced. Nothing breaks by leaving it.
7. **Install the shipping timer** (§5b). Not a blocker, but forever after.


1. **Step 1 of the runbook (§0, the non-editable reinstall) has not been run.**
   Nothing blocks it but the typing; the command was verified working at the
   current head. Until it runs, `aisquare` on `PATH` is the pyenv build — whose
   `cli/launch.py` contains **no `resolve_binary` at all** (`grep -c` counts `0`
   there and `1` on this train), which is the grep the runbook uses to tell the
   two builds apart.

   **What running it gets you, beyond the hazard below.** `aisquare config list`
   exits **1** with a traceback on this machine right now — see "Bugs found and
   fixed"; the trigger is your `[team.profiles.*]`, nothing to do with tracing,
   and step 1 is the fix. And three checks these documents tell you to run
   cannot answer before it, all three measured:

   - **§0's own verification.** It uses `doctor`'s provenance row, and the build
     on `PATH` today prints **no such row at all** — that absence *is* the tell
     that it predates the check, but it is not an answer to "which tree am I
     running". After the reinstall the row reads `installed (non-editable)
     from …` and names it.
   - **§5b's timer check.** `command -v aisquare` answers the same path before
     and after — §0 installs into that same pyenv `bin` — but the BUILD behind
     it changes, and the older one has no `ship`. Run before this item, the
     check exits **2** with a usage line instead of the **1** it is looking for.
   - **Reading why either blocked task is blocked.** `task show` prints
     `stopped because …` as of this train; the build on `PATH` predates that and
     prints nothing, so item 3's workaround is only needed until this item is
     done.

   This item is first in the list for a reason.

   **And it is not only about what you gain by running it.** Once you *have*
   configured explainability, three commands run from a shell that still
   resolves to the stale binary silently strip that configuration: `team bind`,
   `config set`, and `init --reinit`. They exit **0** with no warning — measured
   — because the stale build's config model has only three explainability
   fields, so it drops `target`, `roles`, `ship`, `gateway_url` and the whole
   `[explainability.targets]` table on every write. The three keys a stale
   `config set` will *accept* are exactly the three that *survive*, which is why
   nothing looks wrong afterwards.
   **What eventually looks wrong is an empty insight spool.** `record_prompt`
   no-ops unless `ship` is true, so once `ship` has been stripped the client
   lane captures *nothing* while the proxy lane keeps working and `status`
   keeps reading green — the same asymmetry as the missing timer, arriving from
   a different direction. Measured, both halves, under a throwaway home; setting
   the key back with a stale build does not help either, because
   `config set explainability.ship true` there prints
   `✗ unknown config key: explainability.ship` and exits non-zero, which is the
   one loud member of this family.
   **First suspect your reading, not the config.** The only time anyone here
   thought the spool was empty — ninety minutes of it — it was full; the reading
   was wrong, not the configuration. Confirm it is really empty before you
   suspect a stripped `ship`, and see the next paragraph for where to look.
   **Read the counter, not the directory — and if you do look, the directory is
   `~/.aisquare/explainability/queue/`, not `spool`.** `spool` is this
   codebase's word for the buffer (`status` prints `spool: 1 queued`) and is
   not a path anywhere; the ninety minutes above were lost searching for a
   `spool` directory that does not exist. Nothing shipped points at the wrong
   name — swept both runbooks, the README and CONTRIBUTING — so this line is
   here to name the right one, not to correct a bad one.
   The cutover itself cannot be half-done this way — `explainability enable`
   does not exist on the stale build and exits 2 — so the danger is entirely
   *after* you configure.

   **Same three commands, two different builds, two different verdicts — do not
   read the paragraph above as a statement about the train.** Step 1 closes the
   stale-build case entirely. At the train head the picture is different, and it
   is now measured command by command, because generalising from one of the three
   is a mistake two of us made in opposite directions:

   - `config set` on an unrelated key — **safe**. All **seven** top-level
     `[explainability]` keys — `enabled`, `proxy_url`, `agent_name_template`,
     `target`, `roles`, `ship`, `gateway_url` — and the
     `[explainability.targets]` table survive. (This read *five* until
     `8dd460fb` re-ran it at `d8b600b`: `config set api_url …` changed exactly
     one line and left the section byte-identical. The count was wrong when it
     was written, not rotted — the paragraph above already enumerates seven,
     three the stale build knows and four it drops.)
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
   **[re-run 2026-08-18, `8dd460fb`, at `d8b600b`, in a throwaway
   `AISQUARE_HOME`.]** Every claim in this item that can be executed at the
   train head was: `config set` on an unrelated key changed one line and left
   the section byte-identical; `init --reinit` **refused** at exit 1, named
   what would go (`targets tst; tracing enabled`) and left the file identical;
   `--reinit --yes` reset at exit 0 and *reported* what it removed; an
   unreadable config reset without asking. Also confirmed: plain
   `explainability ship` with no key exits **0** and `ship --strict` exits
   **1**, and `doctor` prints the provenance line. **Not** re-run: the stale
   build's three write paths, deliberately — nothing has installed over that
   build, so re-measuring it re-derives rather than checks, and pointing it at
   a home is the one experiment here that can damage a real one.
   Two tells that need no comparison: `doctor` reports where the running build
   came from, and a build that prints **no** provenance line predates that check
   and is therefore older than this train.
   **`--version` is not a third tell, and it is the one you will try first.**
   Measured at `d8b600b`: the pyenv build and this checkout both print
   `aisquare 0.4.0rc1`. The version string cannot separate them, so use the
   provenance row. After §0 it reads `✓ provenance: installed (non-editable)
   from …` and names the tree — **non**-editable, because that is what §0 has
   you install and seeing `(editable)` there means the warning below was not
   heeded.
   `command -v aisquare` is **not** a third tell either, for a reason worth
   knowing: it answers the same path before and after §0, because §0 installs
   into that same pyenv `bin`. It tells you WHERE the binary is, never WHICH
   build is behind it.

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
   **When that credential exists**, the first thing to run is §5's
   `explainability doctor --live` (read-only, a remediation per line), and then
   the one check nobody has ever executed: **§5c**, which walks one id through
   the join log, the board row and the process environment, and then hands the
   fourth hop — reading the Run back from the studio — to your credential. Three
   of the four need nothing from you but the earlier steps; §5c says which is
   which and why it sits after §4 rather than beside it. That is the join this
   whole integration is for. To read either blocker **with the build you have right now, from any
   directory**: `aisquare team log --limit 200 --as <session>`, where
   `<session>` is any id from the sessions list at the top of `aisquare
   board` — `dfd9a883` was the planner. The `--as` routes
   board resolution through that session's row instead of your working
   directory, which is what makes it directory-proof — verified from the project
   directory, a worktree and `$HOME`, the same 200 events each time. This is the
   one form that does not depend on step 1.
   **After step 1** two things get easier. `aisquare task show <id>` prints the
   blocker directly, as `stopped because …` with `stopped_because` in `--json`
   — the build on your `PATH` predates that and prints nothing, which is the
   *third* check step 1 gates. And a plain board read stops being able to lie
   to you quietly: it warns that our `AISQUARE_TEAM_HUB='./'` is relative and
   is being ignored, a worktree now reads the real board rather than an empty
   one, and from a non-repo directory it names the board it answered —
   `reading board work — /home/work is not a git repository, so the board
   follows your directory; pass --as <session>`.
   **Before step 1 none of those warnings exist.** The stale build answers `No
   team events match` from a worktree and returns twelve plausible events from
   `$HOME`, both silently. So until you reinstall, use `--as` and do not read a
   quiet board as an empty one.

4. **Prod values are unverified.** Every *mechanism* here is verified against
   staging; the prod URL and key are `[unverified-prod]` throughout.

5. **A proxy is already listening on 9190, and it answers §3's check perfectly.**
   Not started by anyone here. So `doctor` reports `explainability proxy: ok`
   whether or not your §3 start succeeded — and would report it if you skipped §3.
   **The tell you can actually use is the age**, because you do not know the PID of
   a process you have not started yet: `ss -ltnp | grep 9190` then
   `ps -o pid,etime,args -p <pid>`. Two independent observations, the payload it
   returns, and what is *not* listening on 9090 are all under "the operator's real
   machine" below — recorded once there rather than twice. §3 and the at-a-glance
   table carry the same caveat at the point of use.

6. **One consent question, asked five-plus times and never forced:** may we
   write to a `/mnt` (Windows) path? A 9p/DrvFs filesystem measurement is
   parked on it. Nothing is broken by leaving it parked.

7. **Not a blocker, but the one thing you must set up and then keep:** install
   the shipping timer from §5b. Nothing drains the insight spool by itself, so
   without it you deliver whatever was captured before you finish and then
   nothing, forever, while model traffic keeps flowing and `status` reads green.
   Use `ship --strict` in the timer — plain `ship` exits **0** with no key, which
   is right for an interactive run and is exactly why a naive cron line reports
   success for as long as you leave it there. Details in "What is done and
   folded"; it is listed *here* too because a recurring obligation described only
   under "done" gets read as done.
   **Do this after item 1, not before.** The wrapper needs the absolute path to
   *your* `aisquare`, substituted at write time because cron has no useful `PATH`,
   and §5b's own check cannot give you a meaningful answer until the reinstall has
   happened: measured on this box, the path §5b used to hardcode does not exist at
   all, and resolving it with `command -v aisquare` before item 1 finds the stale
   build, which has no `ship` — so the check exits **2** with a usage line rather
   than the **1** it is looking for. Exit 2 with usage in it means the wrong
   *build*, not a broken wrapper.

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

  **And this lane ships under a fourth identity you would not guess: `aisquare-cli`.**
  Insights are attributed to the board role of their session, and fall back to the
  role `cli` whenever the board cannot say whose a Run is — an unattributed run, a
  session missing from the store, a store read that threw. So §4b registers **four**
  names, not three, and if you are counting agents in the studio, four is right.
  Unregistered, that name's insights are not lost but they never land: the gateway
  answers `409 agent_not_registered`, which the SDK retries forever and drains the
  moment the name is registered. §1a has the full taxonomy and why the *other* 409
  is the permanent one; §4b registers it.

The correlation spine: one id in four places — launcher mint → `--session-id`
argv → `X-Pipeline-Id` header → `AISQUARE_PIPELINE_ID` marker → board row via
the `SessionStart` hook → `joins.jsonl`.
**Count that chain and you get more than four, so name the four that are
pinned**, because they are not quite the four that were watched. `SPINE_PLACES`
in `tests/test_correlation_spine.py` is the header, the marker, the board row
and the join log; the hand-walk that established this watched the header, the
**argv expansion**, the board row and `joins.jsonl`. (That log is written only
once tracing is on, so it is absent from your home until §4 — see item 3. Its
appearance in this list is a record of what was watched, not a file you can
open right now.) The argv hop is covered a
different way — `test_the_spawn_template_passes_the_flag_the_parser_looks_for`
pins the flag *spelling* the parser matches, and the template interpolates the
variable rather than a literal id — so nothing here is unheld. But "four
places" names two different fours, and only one of them fails the gate.

Also folded: a doctor that verifies and helps wire it (#51) — plain `doctor`
creates no state, `doctor --fix` creates only the home layout, both measured and
pinned (see the doctrine section); per-role identity resolved from the board
role; redaction in
standard and strict modes, credentials-only by default, and scrubbed *into* the
spool rather than on the way out — a claim that rested on a one-field assertion
until this shift and is now checked against the **bytes on disk**; an inventory of all 13 process-spawning seams,
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
- **`config list` is broken on this machine right now, and it is not about
  tracing.** Measured 2026-08-18 against your real `~/.aisquare` with the build
  currently on `PATH`: `aisquare config list` exits **1** with `TypeError:
  Object of type 'NoneType' is not TOML serializable`. Your config has **no**
  explainability targets — the trigger is `[team.profiles.*]`, written by
  `team bind`, whose `bin` is unset. **Step 1 clears it**; the train build
  prints all 45 lines. If you have already seen that traceback, it is this, it
  is known, and nothing you did caused it.
  The mechanism, which is wider than either trigger:
  `save_config` dumps with `exclude_none=True` because TOML has no null;
  `emit_config` renders the same model through the same library and did not.
  So `explainability enable` (a target that overrides nothing) *or* `team bind`
  without `--bin` left `aisquare config list` exiting **1** with a traceback —
  §2 of the runbook, and then the obvious next thing you would type to check
  that §2 worked. Both existing tests of that command pass `--json`, and JSON
  has null, so the gate covered the command and not the branch you see.
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

### The train against this machine's real state

Everything else in this file was measured in a throwaway `AISQUARE_HOME`. On
**2026-08-18** `coder2` ran the train build read-only against the real one — a
**71 MB** store with a night of rows and the accumulated config — and
`config.toml` was byte-identical afterwards. `status`, `doctor`, `explainability
status`, `team status`, `config get` and `context list` all exit **0** with no
traceback, and `--json` on the first four parses. Only `config list` differed,
and that is the entry above.

**The evidence behind item 5**, read-only, nothing started and nothing killed —
the consequence and what to do about it are in that item and are not repeated
here:

```text
ss -ltnp   LISTEN 127.0.0.1:9190  users:(("python",pid=20753,fd=13))
ps         20753  ELAPSED 1-03:15
           ./.venv/bin/python -m aisquare.explainability.claude_proxy
/health    {"status":"ok","service":"aisquare-proxy","mode":"claude_code", …}
```

**And the age is not merely the tell you CAN use — it is the only one that is
stable here.** Three readings of that PID's *absolute* start time, by two
sessions, gave three answers:

| read at | `ELAPSED` | `ps lstart` |
|---|---|---|
| ~05:49 | 1-02:35:44 | Mon Aug 17 03:13:35 |
| ~06:03 | 1-02:48:27 | Mon Aug 17 03:14:45 |
| ~06:33 | 1-03:15:36 | Mon Aug 17 03:17:20 |

Each is self-consistent with its own `ELAPSED`, and the absolute time advanced
about four minutes over forty-four minutes of wall clock. `lstart` is derived
from the kernel's boot time, which is being stepped on this box, so a start
*timestamp* here is not reproducible and yours will be a fourth one. Compare
ages, never clock times.

Also, so the standing instruction is not misread: **nothing is listening on
9090 in this namespace.** "Never kill whatever holds 9090" is a rule about
another context, not a statement that 9090 is serving something here. Measured
at the train head, because I nearly wrote the wrong version of this sentence:

* **never configured** — `doctor` says `✓ explainability: tracing is off`, and
  `explainability status` prints the proxy URL with **no probe line at all**. A
  machine that never asked for tracing reports no failure, which is deliberate.
* **enabled, still pointing at the shipped default** — `✗ explainability proxy:
  proxy unreachable at http://127.0.0.1:9090/health … Connection refused`, with
  the remediation beside it. That is the *correct* red, and it is what you will
  see between §2 and §3 if you leave `proxy_url` at its default.

One warning from running that sweep: **`aisquare note` takes text, not a
subcommand.** `aisquare note list` does not list anything — it posts a note
whose body is the word "list", to whichever project your directory maps to.
There is no `note list`; `aisquare board` is the reader. Left as-is rather than
guarded, because changing the command every session uses to post receipts is not
a change to make on cutover morning.

## What is proven, and by what evidence

Against staging, with real agent processes — not fixtures:

| Claim | Evidence |
|---|---|
| One id in four places | observed in all four simultaneously — **and now pinned**: `tests/test_correlation_spine.py`. **Re-observed on this train against a real `claude` session**, not a stub: `launch runner` printed the minted id, the proxy opened its Run under that same `pipeline_id`, 6× ingest `202`, exit 0 (`d124bc26`). Confirmed **up to the read wall** — that a Run appears in the gateway UI keyed by that id is the one hop still unseen |
| Per-role separation | 3 roles → 3 distinct trace ids — **pinned** in the same file |
| Proxy lane ingest | 70/70, then 14/14, all `202` |
| Client lane delivery | 6/6 `DISPATCHED`, 0 `dead_letter`, 0 `auth_failed`, read from the SDK's own inbox — **and re-observed on THIS train `9747e37` (@9bbc8ed7):** `1 queued → 1 sent`, four spans `dispatched`, `retries=0`, inbox byte-identical after. No longer only the pre-shift measurement |
| Client-lane identity | `agent.run_id` = board session id; `agent.name` = `aisquare-coder`, accepted — that is the *attributed* case; see the fourth name below |
| Redaction on the wire | standard **and** strict |
| No secret in the spooled **bytes** | 3 capture paths incl. the prompt hook — **pinned**: `tests/test_no_secret_reaches_the_spool_file.py`, with a redaction-off control |
| Token-shape coverage | all 11 vendor shapes survive the JSON→OTel round trip |
| Both lanes, one session | same key carried on both (see the caveat above) — **re-measured at `948772e`**, four places one value. Verify it as "did every record captured *inside* the session carry the key?", not "did every record from this launch": a launch also spools one parent-captured `team_event` whose key is correctly `None` |
| Proxy build pinned | **the capability, not the number** — `_has_valid_correlation` is in the build now serving, byte-identical to the checkout the receipts used. **Re-measured at `ae805c2`**: the live proxy self-reports **`1.0.6`** — an editable install of `bb88bb5`, whose branch never bumped the version — so `>=1.1.0` is an install target and **not** a check. `IN FORCE` does not invert to "we are on >=1.1.0", and if §3 already prints it, **do not reinstall**. No `1.1.x` artefact is cached on this box, but byte-identity to the released `1.1.0` is now **measured** (@8dd460fb pulled the PyPI wheel): the `_has_valid_correlation` **function** is byte-identical, the **file** is a later build that gates it behind `_is_cc_mode()` — so `1.1.0` reproduces the receipts' behaviour for a `claude_code` proxy but is not the same bytes; see §3 of the cutover runbook |

Everything in that table stops at the gateway boundary. Nothing in it is a
statement about what the studio shows.

**Three of those rows are stronger than the rest, and the difference matters.**
The first two and the spool-bytes row are now *pinned* — a test fails the gate on the day they stop
being true. Every other row is a **staging measurement taken once**, most of
them before a night of changes to the launcher, the store and the hooks; they
were true when taken and — with three exceptions this shift — nothing re-checks
them. **The exceptions, each with a receipt above:** *client lane delivery* was
re-shipped end to end on this train (`9747e37`), *both lanes one session* was
re-measured at `948772e`, and *proxy build pinned* was re-measured against the
live proxy at `ae805c2` (its capability, not its version number — see §3 of the
cutover). The other rows still stand on their original single measurement. The
spine row was in that weaker category until this shift, and the guard behind it is deliberately built
so it cannot be quietly narrowed: the four places are data, each records its
value *and its origin*, and the claims are pinned by name so a deleted check
fails and an unregistered new one fails too. It does not reach a fixed point —
the guard itself can be removed — but removing a claim now takes two visible
edits in one diff, which reads as a decision rather than an oversight.

### The fifteen minutes — what is and is not established

The runbook was walked **§0 → §7 in order, in one sitting**, in a throwaway home
(@8dd460fb). **Seven steps executed, four blocked** — §1 and §4b need a
credential nobody holds or mutate shared workspace state, §3 needs the SDK extra
that §0 forbids installing over an editable checkout.

What that establishes, and it is worth having: **every executable step ran in
documented order, none stalled or looped, and no step needed state a later step
creates.** The ordering defect that walk was run to look for does not exist.

**What it does not establish is the number**, and a second, fuller walk
(@8dd460fb, against loopback stubs, all twelve sections accounted for) does not
establish it either — deliberately. That walk measured **~29 seconds of command
time, 23 of which is one `pip install`**, with ten of twelve sections walked or
substituted, §1 not walked at all, and §5b's delivery half not exercised in that
walk because the extra was not installed — **since reached: @9bbc8ed7 installed the
real `aisquare` 1.1.0 from PyPI and shipped a live insight (`9747e37`), so the
delivery half is demonstrated on this train, not merely deferred.** It declined to put a wall clock on the *reading*, on the grounds that
an agent's reading speed is not a person's and the number would be a confident
fabrication.

So the honest statement is narrower than either a confirmation or a correction:
**the command path is ~29 seconds and everything else in the fifteen minutes is
reading, deciding, and §1.** What the fifteen minutes must cover is now measured
even though the minutes are not: **1330 lines across twelve sections, 44 fenced
blocks, 12 ⚠ caveat blocks, and §3 alone is 179 lines with three of them.**
Nobody has walked this as a person, so fifteen minutes remains the design target
rather than a measurement. **§1 is the budget** — a dashboard task plus a roster
POST; the rest are single commands.

**And one caution the walk found that per-step checking could not.** §3 was
blocked, and §5 still reported `✓ explainability proxy: claude_code proxy
healthy`, because another process on this box was serving that port. The check
is right that *a* proxy answered; it cannot tell you *whose*. So a green proxy
row can hide a §3 that silently failed, and model traffic then goes to the older
proxy. §3 and the at-a-glance table now carry the caveat and its commands — §3
because that is the CAUSE, where you can still tell "mine" from "someone
else's", rather than only §5 where the symptom shows. **And this is no longer
hypothetical: see "The train against this machine's real state" above for the
process currently answering that port.** This
was invisible to per-step verification because whoever verifies §5 has just done
§3, so the state is always right.

## What was deliberately left

Named so you can tell a decision from an oversight:

- `root_package_shadowed()` was listed here as "a safety net wired to nothing",
  and that was wrong — corrected rather than deleted, because this section's
  whole value is telling a decision from an oversight, and a false entry in it
  costs more than a missing one. It **is** wired: `sdk_presence()` calls it,
  carries the result as `SdkPresence.shadowing`, and the `explainability sdk`
  doctor check reads it and warns with a remediation naming who the collision
  affects. That row appears once tracing is configured, which is the state you
  are in after §4 of the cutover. The wiring (a5c8987, 05:52 UTC 08-17) predates
  the note claiming its absence by four hours. Now pinned end-to-end by
  `tests/test_the_shadow_check_reaches_doctor.py`, which removes the attribute
  the real predicate reads rather than stubbing the function.
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
