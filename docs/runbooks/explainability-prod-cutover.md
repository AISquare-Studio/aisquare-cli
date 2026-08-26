# Runbook — explainability cutover, staging → production

**Audience:** one operator, at a keyboard, aiming to have the team generating
traced data against explainability **prod** in about 15 minutes.

**Read [`MORNING-HANDOFF.md`](MORNING-HANDOFF.md) first** if you have not
already — it is the cold-read summary of what is done, what is proven and by
what evidence, what needs you, and what was left on purpose. This file is how
to execute; that one is what you are executing and why.

**Written:** 2026-08-17, night shift, by the runner session (`d124bc26`), against
train `rc/v2026.08.18` @ `0b5cfd1`.
**Refreshed:** 2026-08-17 ~06:00 by `coder3`, against the train at the commit
this file ships with. The original was 49 commits stale and predated every
command purpose-built for this cutover — `explainability enable`, `register`,
`ship`, `disable`, and `doctor --live` — so following it produced a cutover with
no registered identities and no insight delivery, while every step appeared to
succeed. Steps I re-executed carry my markers; where I could not execute
something I say so and name who did.

**How to read the evidence markers.** Every step is marked, and the markers are
load-bearing — they tell you which steps I actually executed and which ones you
are the first to run:

- **[verified-stg]** — I ran exactly this against staging and quote the output.
- **[verified-train]** — I ran exactly this against the CLI on the train.
- **[unverified-prod]** — the shape is known, but I had no prod credentials.
  Expect to confirm the value, not the mechanism.

**When the markers were last exercised.** A marker records that someone ran
something once; it does not age well on its own, and three claims in this file
were falsified this shift by fixes that landed after them. On **2026-08-18**
`coder3` re-ran, against the current train in throwaway homes, every
`[verified-train]` claim that has a command next to it — **thirteen of them, all
still holding**: §0b's warm-store and concurrency numbers, both damaged-store
messages, the truncation warning, the recovery, `launch`'s fail-open line (with
a healthy-store control), `enable`, `status`, `ship` plain-versus-`--strict`,
the destination check, and the proxy-interpreter finder. The other twenty-nine
markers are prose with no command attached.

**Six of those prose markers have since been re-run too, and "cannot be
constructed" was too pessimistic.** `coder2` built §0's brick warning in
throwaway venvs — both halves, editable *and* non-editable — and it holds,
mechanism included. `coder3` then re-ran five more against the current train,
each with a control so that "it printed something" could not pass for "it
printed the documented thing":

* `status --json` returns all eleven documented keys, and the key VALUE appears
  nowhere in the payload or the human output — checked with a sentinel value
  actually exported, so the absence means something;
* the `eval` really is shell-agnostic — no `$'…'` quoting emitted, and it
  evaluates cleanly under `dash`, `sh` and `bash`;
* without `--session-id` two calls mint different ids; with
  `--session-id d124bc26` the header carries exactly that;
* a `✓ database` line means the file OPENED, not that it is intact — page 20
  zeroed gives `status` exit 0 and `✓ database: … (10 user entries)` while
  `PRAGMA integrity_check` reports `database disk image is malformed`;
* `AISQUARE_AGENT_NAME` is referenced twice in the source — the constant and a
  comment saying it is deliberately not used — and never appears in the emitted
  environment.

That leaves roughly two dozen prose markers unexercised. Several are other
people's measurements, where re-running is re-deriving rather than checking.
Read these notes as covering what they name and nothing wider.

> **Read §1 and §6 before you touch anything.** §1 is a blocker that staging
> hit and prod will hit identically. §6 is how you get out.

---

## 0. Preflight (2 min)

```bash
cd /home/work/work/aisquare-cli
git fetch origin
git checkout rc/v2026.08.18 && git merge --ff-only origin/rc/v2026.08.18
git log --oneline -1 && git status -sb      # ← what you are about to install
```

> ⚠️ **[verified-train, coder2 `8dd460fb`, 2026-08-18] The fetch is not the
> checkout, and the old check could not tell.** This block read
> `git fetch origin && git log --oneline -1 origin/rc/v2026.08.18`, which prints
> the remote head and **changes nothing** — no checkout, no merge. `pip install`
> then installs whatever the working tree happens to be on. It *was* a
> comparison with one side: you were handed the remote head and never your own.
> **[verified-train, @9bbc8ed7 2026-08-18] `-sb` and not `--short`, because
> AHEAD IS NOT DIVERGENCE.** `--ff-only` stops on a genuine divergence — measured:
> `fatal: Not possible to fast-forward, aborting.` But a branch that is merely
> ahead of origin fast-forwards to nothing and reports success:
>
> ```text
> git merge --ff-only origin/rc/v2026.08.18  ->  Already up to date.   exit 0
> git log --oneline -1                       ->  your local commit
> git status --short                         ->  (empty)
> ```
>
> Every line passes and you are not on origin's train. That is the same
> one-sided comparison this block was fixed for, pointing the other way: you
> used to be shown origin's head and not your own, and would now be shown your
> own and not origin's. **This is the normal state of the tree named above** —
> a repo someone has been folding into sits ahead of the remote, and it was
> four commits ahead when this was measured.
>
> `git status -sb` costs nothing and closes it. It still lists the uncommitted
> files (pip installs them, which is why `--short` was here), and adds the line
> that carries the other side:
>
> ```text
> ahead   ->  ## rc/v2026.08.18...origin/rc/v2026.08.18 [ahead 1]
> correct ->  ## rc/v2026.08.18...origin/rc/v2026.08.18
> ```
>
> **No bracket after the branch name.** That is the whole check.
>
> `--ff-only` is deliberate — if the local branch has diverged it stops instead
> of merging, and a divergence here is something to look at, not to resolve at
> 08:00.

Reinstall the CLI so the binary on your `PATH` is the train, not a stale copy —
**not** as an editable install:

```bash
python3 -m pip install '.[dev]'      # NOT -e / --editable, see below
which aisquare
aisquare doctor
```

> ⚠️ **[verified-train, coder2 `8dd460fb`, 2026-08-18] `--version` cannot verify
> this and used to be the check here.** Measured: the pre-§0 pyenv build and a
> fresh install from this checkout **both** print `aisquare 0.4.0rc1`. A version
> string neither build disagrees on cannot tell you which one you are running.
> `doctor`'s **provenance** row can, and it answers both of §0's questions at
> once — measured after a real non-editable install into a throwaway venv:
>
> ```text
> ✓ provenance: installed (non-editable) from /home/work/work/aisquare-cli
> ```
>
> The path is the tree you just checked out, and `(non-editable)` is the `-e`
> warning below confirmed rather than assumed. A build that prints **no**
> provenance row predates that check and is therefore older than this train.
>
> **[verified-train, coder2 `8dd460fb` + `9bbc8ed7`, 2026-08-18] The provenance
> row and the explainability section are two readings of ONE install — and that
> install was watched as a transition, not inferred from two builds.** Into a
> throwaway venv, `python3 -m pip install '<train>[dev]'` exactly as above:
>
> ```text
> BEFORE  python -c 'import aisquare'        ModuleNotFoundError
> AFTER   --json doctor: provenance          installed (non-editable) from <train>
> AFTER   --json doctor: explainability      present   (absent on the PATH build)
> AFTER   aisquare.core                      23 modules incl. redaction/insights/outbox
> ```
>
> **The louder symptom is the absent section, and on this box it is the state you
> start in.** The build currently on `PATH` here prints **no explainability
> section at all** — not a warning, not a skipped row, the whole subject of this
> runbook simply missing — because its `aisquare.core` lacks `insights`,
> `outbox`, `redaction`, `credentials`, `spawn` and `version`: the entire client
> lane. It still exits `0`, and so does the train build, so **neither the exit
> code nor `--version` distinguishes them.**
> So presence of the section is the check: **if `aisquare doctor` shows you no
> `explainability` rows, you are not running this train** — expected before the
> install above, and a red flag after it.
>
> **Count the section, not the rows — no total is a property of the build.** It
> moves on two independent axes. Rendered rows depend on terminal width (the same
> build reads as 17 rows on one terminal and 19 on another). And the `--json`
> check total moves with **configuration**: this build reports 14 checks with 1
> `explainability` unconfigured and 18 with 5 once a target and key are set,
> because the section expands by design when the feature is set up (a six-line
> section about a feature nobody enabled is how the rest of `doctor` stops being
> read). Jatin configures at §2 and §4, so a count captured at §0 is already
> stale when he re-runs `doctor`. **Presence of the `explainability` check is the
> reading that holds** across width, configuration and rendering — the tell is
> the section, never a number.
>
> **If the section is still absent AFTER the install above**, that is a different
> fault with the same symptom: the install landed somewhere that is not on your
> `PATH`. `which aisquare` separates the two, which is why it sits in the block
> above. And the install is still a human's to run on their own `python` — the
> transition above was a throwaway venv, not the operator's site-packages.

> ⚠️ **[verified-train, planner `dfd9a883`] Do not use `-e` for a cutover.** §5b
> has you install `aisquare-cli[explainability]`, and over an editable checkout
> that install **bricks the CLI** — the SDK ships a real `aisquare/` directory
> which shadows the editable path hook, and `aisquare.cli` disappears
> (`ModuleNotFoundError`, verified by `coder3`). An earlier revision of this
> runbook opened with `pip install -e` and carried that warning 450 lines later,
> phrased as a developer hazard — which it is, right up until §0 makes it yours.
> Over a normal install the extra is safe. Develop from an editable checkout if
> you like; do not run **this document** from one.
>
> **[verified-train, coder2 `8dd460fb`, 2026-08-18] Both halves re-run in
> throwaway venvs, mechanism and outcome.** This was one of the prose markers
> nobody had re-executed this shift, and it is the one §0 rests on. Measured
> against the current train, with the SDK's `aisquare/` package present:
>
> | install | result |
> |---|---|
> | `pip install -e` | **exit 1**, `ModuleNotFoundError: No module named 'aisquare.cli'` |
> | `pip install` (normal) | **exit 0**, `aisquare.cli` *and* `aisquare.explainability` both import |
>
> The stated cause holds too: with the package present, `aisquare` resolves to
> site-packages rather than the checkout, so the editable path hook is bypassed
> and the CLI's own modules vanish. Reproduced by creating the package
> directory, not by installing the real extra — the shadowing is the mechanism
> the warning names, and it is what was tested.

**[verified-train] Do not skip this.** On this box overnight the installed
binary and the train both reported `aisquare 0.4.0rc1` while being *different
programs*: `site-packages/aisquare/cli/launch.py` had no `resolve_binary`, so a
role bound to a wrapper silently launched the default agent and exited 0.
**Version does not distinguish them.** Confirm by asking the binary you will
actually run:

```bash
aisquare --json doctor | jq -r '.[]|select(.name=="provenance")|.detail'
```

Expect `installed (non-editable) from <this repo>`. **Empty output means the
build predates the provenance check and is therefore older than this train** —
reinstall. This runs the `aisquare` on your `PATH`, so nothing about your
interpreter can fool it.

Empty is the *bad* reading here, so rule out the boring cause first: a missing
`jq` also prints nothing. `aisquare doctor | grep provenance` answers the same
question without it.

**`installed (editable) from …` is also a fail, and it will not look like one**
— it is non-empty, so it passes the rule above while putting you in exactly the
state the warning at the top of §0 forbids. **[verified-train, planner
`dfd9a883`]** this box answers `installed (editable) from
/home/work/work/aisquare-cli` for a venv build, so the reading is reachable, not
theoretical. If you see `editable`, reinstall non-editable before §5 — that is
the install that bricks the CLI when the explainability extra lands on top.

> ⚠️ **[verified-train, coder3 `9bbc8ed7`] The older form of this check could
> tell a correctly-installed operator to reinstall.** It was
> `grep -c resolve_binary "$(python3 -c '…aisquare.__file__…')/cli/launch.py"`,
> which asks whichever `python3` **the shell** resolves where the *package*
> lives — not the interpreter behind the `aisquare` on your `PATH`. Measured in
> three states: with a venv on `PATH` it reports `1` (correct, because `PATH`
> brought both); on a genuinely stale box it reports `0` (correct); and with
> **only the binary** on `PATH` — the shape `pipx` produces, and the shape
> *`doctor`'s own remediation recommends* — it reports `0` **for a freshly
> installed train build**, because it inspected an unrelated
> `site-packages`. Following §0 exactly keeps them in step, since `python3 -m
> pip install` installs into that same `python3`; the trap is for anyone who
> installed as a global tool. If you still want the specific symbol, run the
> grep with the interpreter you installed *with* — and do not parse the console
> script's shebang to find it, because `pip` writes a `#!/bin/sh` exec-hack
> when the interpreter path is long, which is exactly the `pipx` case.

---

## 0b. Warm the store before you launch the crew (10 seconds)

**[verified-train]** If `~/.aisquare` does not exist yet — a new machine, a new
operator account — run **one** `aisquare` command by itself before starting
several sessions at once:

```bash
aisquare status >/dev/null    # creates and migrates ~/.aisquare/context.db
```

**On a home this new, `aisquare doctor` exits 1 with `fail home` — before the
line above, not after.** Measured by `8dd460fb` in a throwaway home. This is
not your case (yours holds days of data), and it is recorded because §0's own
verify step *is* `doctor`: on a genuinely first-ever install that verify fails
for a reason that has nothing to do with explainability, and the fix is the one
command above rather than anything in this runbook.

**Why.** Several sessions opening a *brand-new* store simultaneously can race
its migration and fail with `store_error: duplicate column name: account`. That
database is then permanently wedged for that migration — it is not a transient
error you can retry past. One command first does the whole migration alone, and
everything after it opens a store that needs no migrating.

**[verified-train]** Measured on the train, both directions:

```
fresh home + one command  -> user_version 10, journal_mode wal, integrity_check ok
then 8 concurrent opens   -> 8/8 exit 0
12 concurrent FIRST opens on a fresh store, no artificial load
                          -> {"error":"store_error","detail":"duplicate column name: account"}
```

The failure needs a *fresh* store, so a machine that has ever run `aisquare` is
not exposed. Full characterisation, reproduction and the open root-cause
hypothesis are in `docs/store-migration-race.md`.

### If this very command fails, you are in the case §0b exists for

> **If `aisquare status` exited 0, you are done with §0b — go to §1.** Everything
> below is for the case where it did not, and it is long because a damaged store
> has three different shapes with three different tells. You do not need any of
> it on a healthy machine.

**[verified-train, coder3 `9bbc8ed7`]** A damaged store says so in one line, and
the line carries the recovery:

```
✗ the context store cannot be opened: ~/.aisquare/context.db (file is not a
database). Move it aside and re-create: mv … && aisquare init — the board
history in it is lost; config.toml and credentials are untouched
```

**This replaced a stack trace.** All fourteen commands that used to print 59-75
lines of Python traceback on a damaged store — `status`, `init`, `log`,
`inject`, `context list/export/preview` (and the `ctx` aliases), `project
list/info`, `workspace list/info` — died in one place, `open_store`, and are
translated in one place now. That the class stays closed is asserted by
`tests/test_no_traceback_on_a_damaged_store.py`, whose ratchet is empty, rather
than remembered here. If you DO see a traceback, you are on an older build; the
recovery below still applies.

**`launch` is not one of them, and that is the part that matters at 08:05.**
**[verified-train]** A damaged store used to kill every launch — exit 1, a stack
trace, and the agent never started, so you could not even open a session to work
the problem. Fixed: launch now exits **0**, the agent runs, and it tells you what
it cost —

```
board: context.db unreadable (file is not a database) — launching without a board row
Launching … as coder with no board row (context.db unreadable)…
```

No board row means no join to a gateway Run for that session: a lost trace, which
is what the fail-open rule says to spend. **So you can start agents while the
store is broken — but fix the store before you care about traces.**

**[verified-train] Not every damaged store looks like that — and an earlier
version of this paragraph promised more than the code delivers.** It said four
shapes were loud and *all* gave the one-line message above. That is true only of
damage found when the file is **opened**: non-database bytes and a truncation.
Damage found later by a **query** now gets its own one-line message — *"the
context store is **damaged**"* rather than *"cannot be **opened**"*, because at
that point the file opened fine. **[verified-train, planner `dfd9a883`]** pages
3 and 5 zeroed: one line each, no frames, same recovery. An earlier version of
this paragraph said query-time damage still produced a stack trace; that was
true when it was written and stopped being true when the seam widened.

**If the file opens and a later read hits a bad page, you still get a stack
trace** — the one-line seam has already let the command through by then.
Measured at this train, zeroing one page of a small store: pages 3 and 5 gave
**36 and 46 lines** with source frames and `database disk image is malformed`;
pages 1, 2, 10 and 20 gave exit 0 and looked entirely normal. **Which page is
fatal depends on what that store happens to hold, so do not read those numbers
as a rule** — read the shape: *opened-then-queried damage can still traceback,
and some of it is silent instead.* The recovery below is the same either way.

**"Loud" for a corrupted page means loud once something reads it.**
**[verified-train]** Whether a bad page is fatal depends on whether the command
you ran reaches it — @8dd460fb's store failed immediately at page 2 and mine did
not, on the same page number, because the two files held different things.
Damage in a region nothing reads sits there
silently until a query happens to reach it — which can be days later, on a
command with no connection to whatever broke it. **So a store that opened
cleanly this morning is not evidence that it is undamaged**, and a `✓ database`
line means the file opened, not that all of it is readable. **[verified-train]**
Measured: with a page zeroed deep in the file, `status` exits 0 and `doctor`
prints `✓ database: context.db is readable (1 user entries)` — the row it counts
still reads, while the file is malformed.

To check the whole file rather than the part that happens to be read — nothing
in the CLI does this, because it reads the entire database:

```bash
python3 -c 'import sqlite3,os
p=os.path.expanduser("~/.aisquare/context.db")
try: print(sqlite3.connect(p).execute("PRAGMA integrity_check").fetchone()[0])
except Exception as e: print("DAMAGED:", e)'
```

`ok` means intact. **[verified-train]** both ways: `ok` on a healthy store,
`DAMAGED: database disk image is malformed` on the zeroed-page one above.
Python rather than the `sqlite3` shell because that binary is **not installed
on this machine** — the first version of this instruction used it and would
have failed in your hands.

**The fifth is silent, and it is the one to know about.** A file **truncated to
zero bytes** is read by SQLite as a brand-new empty database, so the store is
re-created and migrated and `status` exits 0 — while every session, task and
note that file held is gone. The CLI prints one line at the first open that
sees it:

```
board: ~/.aisquare/context.db exists but is empty — it was truncated, and the
tasks, notes and sessions it held are gone.
```

**That line goes to whichever process opened the file first, which is usually a
HOOK** — its stderr reaches neither you nor the agent. So do not rely on seeing
it. **[verified-train]** Ask `doctor` instead, at any point afterwards:

```
⚠ database: context.db is readable (0 user entries) — but it was found
TRUNCATED and rebuilt at 2026-08-18T01:19:55+00:00; the sessions, tasks and
notes it held are gone
    → Nothing to repair — the history was lost before this. Acknowledge it
      with: rm ~/.aisquare/store-was-truncated
```

**So an empty board plus that ⚠ means truncation, not corruption** — and the
recovery below does not apply, because there is nothing left to recover. The
warning persists until you remove the marker file yourself, because a warning
that clears itself is one nobody has to answer. Clearing it returns `doctor` to
`✓`.

Move it (`mv`, as below) or remove it (`rm`) — but **never truncate it with a
redirect**. `> ~/.aisquare/context.db` puts you in exactly this row, the one
shape where the repair and the damage are indistinguishable. (@9bbc8ed7's
phrasing: it matters that `rm` is still fine, because the older recovery block
further down this file uses it.)

Recovery, **[verified-train, coder3 `9bbc8ed7`]** end to end — this is the
command the error above and `aisquare doctor` both print, verbatim:

```bash
mv ~/.aisquare/context.db ~/.aisquare/context.db.broken   # keep it; see below
aisquare init                                             # ONE process, alone
aisquare doctor | grep database                           # expect: ✓ readable
```

> ⚠️ **This empties the board.** `context.db` holds every team session, task and
> note — the whole history. After the move, `aisquare board` reports an empty
> orchestrator, **[verified-train]**. That is the price of the recovery and
> there is no partial version of it, which is why the file is MOVED rather than
> deleted — the bytes survive for whoever wants to look at them. Nothing about
> explainability lives in this file: your config, targets and key are untouched,
> **[verified-train, coder3 `9bbc8ed7`]** on both damaged states.

---

## 1. Bind the agent names to a studio — **the real blocker** (5 min)

Do this **first**. Everything else can be green while this is broken, and you
will not notice.

### What is wrong today

**[verified-stg]** On staging, every studio-scoped policy check fails and fails
*open*. From the proxy log:

```
POST https://stg-explainability-api.aisquare.studio/v1/studios/21/policy/check/output "HTTP/1.1 403 Forbidden"
WARNING [aisquare.explainability.policy] policy check degraded (FAIL_OPEN): policy gateway returned 403
```

29 of them in one short session — 19 `check/output` + 3 `check/retrieval` +
7 `check/tool`, all 403, with `{"detail":"Workspace does not own this studio"}`.

> ⚠️ **[verified-stg, runner `d124bc26`, 2026-08-18] CORRECTION — this is not a
> wrong-studio-id problem, and an earlier revision of this section said it was.**
> The id is a red herring. **The workspace `ingest:write` key cannot perform
> studio-scoped policy operations against *any* studio.** Measured — a full
> sweep of every studio the key can see, same key, same header:
>
> ```text
> studio 144 145 146 147 148 149 150 151 152 153 158 159 162 165 166 169
>   policy/check/output  ->  403 on all 16
>   agent-rule-books     ->  403 on all 16
> studio 21 (the pinned one)  ->  403 on both, same as the rest
> ```
>
> Those sixteen are **every studio this very key can list** — `GET /v1/studios`
> with it returns `200` and exactly those ids. I swept all of them, not a
> sample: **16/16 403 on the policy surface, 0 non-403.** So the key
> authenticates fine and is refused everywhere it could be used. That is **authorization, not a bad id and not a bad key**, and
> correcting `EXPLAINABILITY_STUDIO_ID` to an owned studio does not fix it —
> `169`, the number our own env-file comment records, is owned *and* still 403s.

What remains true, and still matters: a pinned value short-circuits the
gateway's own lookup — `policy.py:_ensure_studio` opens with `if self.studio_id:
return self.studio_id`, so the binding is never consulted. And the binding does
not exist yet:

```
GET /v1/routing/resolve?agent_name=aisquare-runner
  -> HTTP 404 {"detail":"No studio bound to this agent yet"}
```

**[verified-stg]** — same 404 for `aisquare-planner`, `aisquare-coder`,
`aisquare-cli-test`, `aisquare-subagent-probe2`. Either way — pinned or
unpinned — **nothing is enforced today**, and every check fails open, so the
dashboard looks healthy.

> ⚠️ **[verified-stg, runner `d124bc26`, 2026-08-18] There is no separate "bind
> the agents" step, and an earlier revision of this runbook invented one.**
> Read from **staging's own OpenAPI** (`GET $EXPLAINABILITY_GATEWAY_URL/openapi.json`
> — `info.version` `0.2.0`, identical to the local build), `/v1/routing/resolve`
> documents itself:
>
> ```text
> Workspace key -> resolve via the local agent_studio_routing binding
> (written at attach time by ensure_agent_studio_binding); 404 until the
> agent's rule book is attached (nothing to enforce yet anyway).
> ```
>
> **The binding is a side effect of attaching the rule book.** So the 404 above
> does not mean "bind first, then you may attach" — it means *no rule book is
> attached yet*. Attach per agent, and the binding writes itself.

**The actual 08:00 blocker is a credential, not a config value.** Attaching is
`POST /v1/studios/{studio_id}/agent-rule-books` (required body: `agent_name`,
`rule_book_url`, `label` — **[verified-stg]**, present on staging, not merely
locally), and that endpoint returns `403` with the workspace key in our env file.
**Have a studio-scoped key or a dashboard/human JWT ready before you start** —
the ingest key in `explainability-prod.env` cannot do this step.

> **That credential has a second use, and it is the only instrument for it.**
> Attaching the rule book is what this section needs it for. It is *also* the
> only way to **verify** two of the things this document says nobody has
> checked: whether board rows join gateway Runs on a shared key, and whether
> runs are governed rather than merely traced. Both come from one call,
> `GET /v1/studios/{studio_id}/ui/runs` — see §5c, hop 4. `session_id` appears
> on exactly one route in the entire published surface, so **if this credential
> is not provisioned there is no workaround to reach for.** Worth knowing while
> you decide how urgently to get it.

### The order that actually works

**1a. Register the roster.** **[unverified-prod]** — the auth shape is
**[verified-stg]**: header `X-API-KEY` with the raw workspace key, and **no
`Authorization` header** (a fronting layer tries to verify it as a JWT and fails
the whole call — confirmed: the same request with `Authorization: Bearer` returns
`401 {"detail":"Token verification failed"}`).

> **The body shape below is confirmed against the deployment's own declaration,
> not against a checkout.** That distinction earns its place here because
> `aisquare` **1.0.6** names two different artefacts on this box (§3), so a
> source tree is precisely the thing whose identity cannot be assumed — whereas
> a running gateway publishes what it will accept. From staging's unauthenticated
> `GET /openapi.json` (`info.version` `0.2.0`): `RosterRegisterRequest` requires
> `agents`, an **array of plain strings** — exactly the `curl` below, and exactly
> what `aisquare explainability register` sends.
>
> **A schema match is not a successful call.** It rules out one failure mode —
> a malformed body rejected `422` while you are reading a different section for
> the cause — and rules out nothing else: not that prod accepts your key, not
> that the roster is applied, not that prod publishes the same schema as staging.
>
> Its `200` is declared **free-form** (`additionalProperties: true`, no
> structure), because the gateway forwards the Studio backend's body verbatim.
> That is why `register` walks the response for `publication_id` instead of
> parsing a fixed shape, and why it prints the raw body when it finds none — if
> the backend renames that field, the command degrades to showing you the answer
> rather than claiming there wasn't one.

```bash
set -a; source /path/to/explainability-prod.env; set +a   # see §2
curl -sS -X POST "$EXPLAINABILITY_GATEWAY_URL/v1/agents/register-roster" \
  -H "X-API-KEY: $EXPLAINABILITY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agents": ["aisquare-planner", "aisquare-coder", "aisquare-runner", "aisquare-cli"]}'
```

The response carries a **`publication_id` per agent**. That is the id of the
agent's publication record in the workspace — it is **not** a studio id, and
putting it in `EXPLAINABILITY_STUDIO_ID` is precisely the mistake that produced
the staging 403s. Record the values; do not wire them anywhere yet.

> Use the real role names. `agent_name_template` defaults to `aisquare-{role}`
> (**[verified-train]**), so the names the proxy will present are
> `aisquare-planner`, `aisquare-coder`, `aisquare-runner`. A name that is not
> registered is rejected at ingest with `409 agent_not_registered` (see below —
> `no_agent_identity` is a different, permanent case).
>
> **`aisquare-cli` is the fourth name and it is not a typo.** The CLIENT lane
> attributes each Run to the board role of its session, and falls back to the
> role `cli` whenever the board cannot say whose a Run is — an unattributed
> run, a session missing from the store, a roleless row, or a store read that
> threw. Those are the sessions nobody is watching, so leaving the name out
> strands exactly the data hardest to notice. **[verified-train]** on this tree:
> `_agent_name_for(settings, UNATTRIBUTED_RUN)` returns `aisquare-cli` while
> the roster held only the three roles.
>
> **It queues, it does not vanish — and the difference decides what you do
> about it.** An earlier draft of this note said these spans were lost
> permanently. That was wrong, and `dfd9a883` caught it: there are three
> distinct `409`s and they diverge exactly here. From `aisquare/explainability/sweeper.py` at
> `bb88bb5` (`aisquare` 1.0.6), in its own words — `agent_not_registered` is
> "the agent is named but IAM has no mapping … a routine onboarding race,
> transient — retried forever"; only `no_agent_identity` (the batch holds the
> trace's true root span and still has **no** agent name anywhere) is
> "deterministic POISON" and dead-lettered. `aisquare-cli` **is** a name, so an
> unregistered one takes the first branch: `gateway/main.py` raises
> `409 agent_not_registered` with `routing.agent_name` attached.
>
> **[verified-stg, `9bbc8ed7`, 2026-08-18] On staging it does not 409 at all —
> and that is a fact about the WORKSPACE, not about the name.** A real insight
> shipped from a train build under `aisquare-cli`, an identity nobody has
> registered, was **accepted**: four spans, inbox status `dispatched`, zero
> retries, no error. Ingest resolves `(workspace_id, agent_name)` through IAM's
> auto-register endpoint, which `gateway/routing.py` describes as returning the
> existing publication "or creates one when the workspace opted into
> auto-discovery" — so acceptance under an unregistered name means **staging's
> workspace has auto-discovery on**. Whether prod does is one of the open prod
> questions, and nobody here can read that setting.
>
> **That is exactly why §4b still matters.** Registering removes the dependency
> on a workspace setting you cannot see: with auto-discovery on the roster is
> belt-and-braces, with it off the paragraph below is what happens instead.
> Do not read "it worked on staging" as "the name need not be registered".
>
> **And do not use `/v1/routing/resolve` to predict this.** It returned `404`
> for `aisquare-cli` *after* that agent's spans were accepted, and `404` for a
> name never sent at all — measured. Its `404` is not evidence about whether
> ingest will route you.
>
> With auto-discovery **off**, the cost of leaving the name out is a **growing
> backlog**, not a deletion.
> **Registering is idempotent** (a known name returns its existing
> `publication_id`), so the fourth name costs nothing, and registering it
> *later* still drains whatever queued in the meantime. Do not go looking for a
> dead-letter queue for this case; there isn't one, by design.
>
> **And the backlog is not silent — `doctor` already reports it, with the
> remedy.** The SDK's `delivery_backlog` check counts `pending` rows whose
> `last_error` starts `gateway_status:409`; its docstring calls these "the exact
> states the sweeper's silent retry loop otherwise hides from the customer".
> **[verified-train]** — seeded one such row into a throwaway inbox and ran the
> real command:
>
> ```text
> empty inbox   ✓ sdk:delivery_backlog: empty
> one 409 row   ⚠ sdk:delivery_backlog: pending=1 — 1 pending row(s) last rejected
>               with gateway_status:409. … if the agent is named but unregistered,
>               register it … or enable auto-discovery (autoregister_unknown_agents)
> ```
>
> **Note the `⚠`, and note that `doctor` still exited 0.** The SDK returns
> `error`; this CLI degrades every SDK row to a warning while tracing is OFF,
> because an observer may never cost an exit code for a lane you have not turned
> on. Turning tracing on flips the same row — measured, same seeded inbox, same
> command:
>
> ```text
> tracing off   ⚠ sdk:delivery_backlog: pending=1 — …     doctor exit 0
> tracing on    ✗ sdk:delivery_backlog: pending=1 — …     doctor exit 1
> ```
>
> So before §4 this is a line you have to *read*; after §4 it is one that stops
> you.
>
> **And there is a third state that prints nothing at all.** On a machine where
> tracing was *never configured*, the SDK section collapses and this row is
> **absent** — not `ok`, not `⚠`. That is correct (an untouched machine has no
> backlog either) but it is a different reading from "tracing off", and the two
> are easy to conflate. **[verified-train, `8dd460fb`]**, seeded inbox, with the
> control that makes the rest mean anything:
>
> ```text
> ── measured with TRACING ON; the two ✗ rows are ⚠ / exit 0 with tracing off ──
> missing inbox file             ✓  "No inbox database at <path> …"          exit 0
> inbox present, table empty      ✓  "empty"                                  exit 0
> unreadable file                 ⚠  "Cannot read inbox at <path>: …"         exit 0
> clean pending row, no 409       ✓  pending=1                                exit 0   <- CONTROL
> pending, last_error 409         ✗  pending=1 — … gateway_status:409         exit 1   ← ⚠ / exit 0 if tracing off
> dead_letter row                 ✗  dead_letter=1 — … no_agent_identity     exit 1   ← ⚠ / exit 0 if tracing off
> tracing never configured        (row absent entirely)                      exit 0
> ```
>
> The condition is in the header because that is where the misreading happens:
> §5b comes *after* §4 in the document but not necessarily on the machine, so a
> reader who has not enabled tracing yet, hits a real backlog and glances at the
> table sees a warning where it promises a cross — and a qualifier underneath
> arrives after they have already drawn the wrong conclusion. **The backlog is
> exactly as bad in both readings; only the loudness changes.**
> **[verified-train]** — all six states re-measured with tracing off: the two
> `✗` rows read `⚠` at exit 0, and the other four are byte-identical to the
> readings above, so exactly two rows move.
>
> The control is the load-bearing line: a clean pending row is `ok`, so the
> failure is about **the 409** and not about having a queue at all. The
> unreadable-file row is the doctrine on a path nobody had walked — a corrupt
> buffer costs a warning, never the exit code. And the real proxy-written inbox
> reads identically to a hand-built one: its schema has **ten** columns where the
> fixture had three, and the check selects only `status` and `last_error`, so the
> difference cannot matter (`8dd460fb`, read-only, file byte-identical before and
> after).
>
> **⚠️ The two green readings are not the same news, and this is the one to learn.**
> `empty` means the file is there and drained — the client lane is working and has
> nothing queued. `No inbox database` means the file is *not there*, which given
> the relative-default hazard above may simply mean **you are looking somewhere
> else**. Same tick, opposite diagnostic value: one says "nothing to deliver", the
> other cannot distinguish that from "not watching the queue that is filling". If
> you see `No inbox database` after §4 and any work has run, check
> `EXPLAINABILITY_INBOX_PATH` before you believe it.
>
> **Which gives the one sentence to remember about this row: it has two
> preconditions, and missing either turns a real backlog into silence.** Tracing
> must be on, *and* `EXPLAINABILITY_INBOX_PATH` must point at the file that was
> actually written. Miss the first and the row is absent; miss the second and it
> says `✓ … No inbox database … (nothing recorded yet)` over a filling queue.
> Both failures look like health.
>
> **That reporting is only as good as `EXPLAINABILITY_INBOX_PATH`, which is
> where §2 earns its keep.** The check reads that variable and defaults to the
> RELATIVE `explainability_inbox.db`, so unset it resolves against whatever
> directory `doctor` was run from — and `8dd460fb` measured that the proxy
> writes an absolute path of its own while the doctor and the client library
> share the relative default. Set it in the env file and this row watches the
> inbox that is actually filling; leave it unset and a green
> `sdk:delivery_backlog` may only mean you were standing somewhere else.
>
> **Rather than retyping this list, let the CLI derive it:** `aisquare
> explainability register` builds the roster from `explainability.roles` plus
> the fallback and POSTs the same body, so it cannot drift from what the CLI
> actually emits. The curl above is kept because it shows the auth shape and
> runs before anything is configured.

**1b. Attach a rule book, per agent — this IS the bind step.**
**[verified-stg]** There is no separate binding action; attaching writes the
`agent_studio_routing` row (see the correction above). Do it in the studio
dashboard, or call it directly if you hold a studio-scoped credential:

```bash
curl -sS -X POST "$EXPLAINABILITY_GATEWAY_URL/v1/studios/$STUDIO_ID/agent-rule-books" \
  -H "X-API-KEY: $STUDIO_SCOPED_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "aisquare-runner", "rule_book_url": "<url>", "label": "<label>"}'
```

`agent_name`, `rule_book_url` and `label` are the required fields
(**[verified-stg]** from staging's OpenAPI). Repeat for `aisquare-planner`,
`aisquare-coder` and `aisquare-cli`.

> The workspace `ingest:write` key in `explainability-prod.env` gets `403` here
> — measured against every studio it can list. Use the dashboard or a
> studio-scoped key.

**1c. Verify the attach took — this is the gate for the whole runbook:**

```bash
curl -sS "$EXPLAINABILITY_GATEWAY_URL/v1/routing/resolve?agent_name=aisquare-runner" \
  -H "X-API-KEY: $EXPLAINABILITY_API_KEY"
```

- ✅ `{"agent_name": "…", "studio_id": "<a real id>"}` → attached and bound.
- ❌ `404 {"detail":"No studio bound to this agent yet"}` → **stop.** The rule
  book is not attached. Tracing will work and governance will not.

Repeat for each name. **[verified-stg]** this call works with the ordinary
workspace key, so it is the one governance check Jatin can run without the
studio credential.

**1d. Probe the attachment — confirm the rule book actually loads.**
**[verified-stg]** staging exposes a purpose-built check that beats reading a
dashboard:

```bash
curl -sS -X POST \
  "$EXPLAINABILITY_GATEWAY_URL/v1/studios/$STUDIO_ID/agent-rule-books/$ATTACHMENT_ID/probe" \
  -H "X-API-KEY: $STUDIO_SCOPED_KEY"
```

It forces a live fetch of the attachment URL with **no cache** and returns
`{"ok": bool, "status_code": int, "sample_policies": [{"id": …, "name": …}]}`.
`ok: true` with a non-empty `sample_policies` is the proof the rule book is live
— a resolvable `studio_id` alone only proves the row exists.

Then make one traced call (§5) and confirm `policy check degraded (FAIL_OPEN)`
has **stopped** appearing in the proxy log.

**1e. Leave `EXPLAINABILITY_STUDIO_ID` unset** in the prod env file so one-key
mode resolves the studio by agent name (§1e is why the pin is harmful: it
short-circuits resolution). Never put a `publication_id` there.


---

## 2. Prod gateway URL and key handling (2 min)

**[unverified-prod]** — I had no prod credentials; this mirrors the verified
staging arrangement.

Keep the prod secrets in a file outside every repo, mode `600`:

```bash
install -m 600 /dev/null /home/work/.config/aisquare/explainability-prod.env
```

Contents (values from the prod workspace — Settings → Studios → API keys):

```sh
EXPLAINABILITY_GATEWAY_URL=https://<prod-explainability-host>
EXPLAINABILITY_API_KEY=<prod ingest:write workspace key>
EXPLAINABILITY_AGENTS=aisquare-planner,aisquare-coder,aisquare-runner
EXPLAINABILITY_INBOX_PATH=/home/work/.aisquare/claude_proxy_inbox.db
# EXPLAINABILITY_STUDIO_ID intentionally NOT set — see §1e.
```

> ⚠️ **[verified-train, coder2 `8dd460fb`, 2026-08-18] `EXPLAINABILITY_INBOX_PATH`
> is the fourth line for a reason: without it the SDK's own backlog check reads a
> different file from the one its proxy writes.** In the pinned SDK
> (`aisquare` 1.0.6 @ `bb88bb5`) it is **three-and-one**, not a two-way split:
> the **proxy** overrides the path to `~/.aisquare/claude_proxy_inbox.db`, while
> the **doctor**'s `_check_delivery_backlog`, the client **init**'s
> `inbox_path=` default and `InboxWriter`'s `db_path=` default are all the same
> **relative** `explainability_inbox.db`. So the doctor does not disagree with
> the client library — it agrees with it, and both differ from the proxy. (All
> four live in the SDK, not in this repo, which is why they are named by symbol
> here rather than by path.)
>
> **The path above is a literal, not a placeholder — and it is the one value in
> this block that fails silently if you get it wrong.** Everything else you must
> substitute is written in `<angle brackets>`; an earlier revision of this line
> said `/home/you/…`, which looks like a value and is not, sitting three lines
> under an `install` command that already names the real home. A wrong *binary*
> path in §5b exits **127** and stops. A wrong *inbox* path stops nothing: the
> backlog check prints "No inbox database … (nothing recorded yet)" and the §5b
> timer drains a file that was never written. Check it against the `install` line
> above and against `ls ~/.aisquare/claude_proxy_inbox.db`.
>
> **And the shared default being relative is the half that costs data, not just
> visibility.** A relative path resolves against the process working directory —
> measured: `InboxWriter()._db_path` is the bare string `explainability_inbox.db`
> in two different directories. So with this variable unset, the CLI's own
> insight buffer FOLLOWS YOU AROUND: `aisquare explainability ship` run from two
> directories drains two different inboxes, and nothing tells you which one you
> are looking at. §5b puts `ship` in a **timer**, and a timer whose working
> directory differs from the shell that captured the insights would drain an
> empty inbox and report success — the silent-spool failure §5b exists to
> prevent, arriving through the path instead of the key. Setting an ABSOLUTE
> path here fixes that as well as the doctor row: all four sites read this one
> variable. So with
> the variable unset, `doctor --live` prints
> `✓ sdk:delivery_backlog: No inbox database … (nothing recorded yet)` over a
> live inbox. Measured both ways on a probe inbox holding one pending row: unset
> → "No inbox database"; set → `pending=1`.
> That check is the one designed to surface what the sweeper's retry loop hides
> — its own docstring says it fails on dead-lettered rows and on pending rows a
> gateway 409'd — so pointed at the wrong file it cannot do its job, and the day
> there IS a backlog it will still say nothing was recorded. **The defect is the
> SDK's and belongs upstream; this line is the mitigation available from here.**
> Observed on this box: the proxy's inbox was 5.2 MB after 27 hours and held
> **zero** rows — allocated space after rows were written, delivered and pruned.
> Nothing was hidden; the detector simply could not see the file.

> ⚠️ **[verified-train] Do NOT put `AISQUARE_AGENT_NAME` in this file.** An
> earlier version of this runbook set it to `aisquare-runner`. That variable is
> the **SDK's routing identity**, not ours — the CLI only ever reads it and
> never writes it — and it is SDK-wide: `doctor --live` reports it as the
> default identity that stamps rootless spans. Pin it to a role in a shared env
> file and every session that sources the file routes as that role, which is
> exactly the misattribution the whole correlation spine exists to prevent.
> Leave it unset and let each launch carry its own identity.
>
> The CLI's own markers are `AISQUARE_PIPELINE_ID` and
> `AISQUARE_TRACE_AGENT_NAME`. They are internal, the launcher sets them per
> session, and neither belongs in an operator env file.

Load it **per shell**, never globally:

```bash
set -a; source /home/work/.config/aisquare/explainability-prod.env; set +a
```

Rules that are not negotiable: the path is never baked into source, the contents
never go into a repo, a board note, or a ticket. Use an `ingest:write` key —
writes traces, cannot read or rebind. Rotation is new key → deploy → revoke old.

---

## 3. Start the proxy (2 min)

**Which build.** Pin **`aisquare>=1.1.0`**. Overnight receipts were collected
against a local checkout of branch `f9/suppress-cc-shell-run` @ `bb88bb5`, and
that raised a fair question: is the evidence reproducible from anything you can
install? It is, with one correction to how it was first phrased. `1.1.0` is on PyPI and
carries the junk-run suppression — the `_has_valid_correlation` **function** in
`claude_proxy.py` is byte-identical to the checkout's (verified against the real
PyPI wheel; the measured note below has the shas). The `claude_proxy.py` *file*
around it is **not** byte-identical:
`1.1.0` is a later build, and it gates that same function differently — read the
note before you treat `1.1.0` as bit-for-bit the receipts' proxy. The *released*
`1.0.6` and `1.0.7` do **not** have the function at all, and on those
the junk-run behaviour returns silently as extra Runs in the dataset — but the
`bb88bb5` checkout itself also self-reports `1.0.6`, so do not use the version
string to decide; run the check below. (Measured block after it.)

**Since 0.5.0, prefer the CLI.** It resolves the gateway, key and port from the
target configured in §1–§5, records what it started, and waits until `/health`
answers before reporting success:

```bash
aisquare explainability proxy up
aisquare explainability proxy status     # exits 0 when up, 1 when not
aisquare explainability proxy down
```

`status` is the only thing that answers **whose** proxy is on the port. The
`explainability proxy` doctor row goes green for any service replying as
`aisquare-proxy` in `claude_code` mode — including a proxy left running against
the *previous* target after a cutover, whose Runs go to the old deployment.
`proxy status` reports `managed: false` in that case and names the stale target;
`proxy up` stops it and starts one for the current target. Branch scripts on
`managed`, not on `healthy`.

The manual form below still works, and is the right one when the SDK lives in a
different environment from the CLI. It is also what produced the receipts in this
runbook:

```bash
set -a; source /home/work/.config/aisquare/explainability-prod.env; set +a
export AISQUARE_PROXY_PORT=9190
python -m pip install 'aisquare>=1.1.0'
python -m aisquare.explainability.claude_proxy
```

> A proxy started this way is **foreign** to `proxy status` and `proxy down` by
> design: the CLI has no record of it, cannot know its gateway or key, and will
> not signal a process it did not start. Stop it where you started it.

> ⚠️ **[verified-train, @9bbc8ed7 2026-08-18] If something already holds the
> port, this fails — but it says `Application startup complete` first.**
> Measured against an occupied throwaway port (never 9190, never 9090):
>
> ```text
> INFO:     Started server process [56195]
> INFO:     Waiting for application startup.
> INFO:     Application startup complete.
> ERROR:    [Errno 98] error while attempting to bind on address
>           ('127.0.0.1', PORT): address already in use
> INFO:     Application shutdown complete.
> ```
>
> Exit code 1, no traceback — a clean failure. But the three INFO lines above
> the ERROR are uvicorn reporting that the *application* started, not that the
> *socket* bound, and they print in that order. **Read to the last line, not
> the third one.** This matters here more than it would elsewhere: a proxy is
> already listening on 9190 on this box (see the caveat below), so a clash is
> the expected case rather than the unlucky one, and every check after this
> step passes whether or not your own start succeeded.

**[verified-train]** Confirm the running proxy really has it — from the process
itself, so it answers for the build that is actually serving rather than for
whatever you last installed:

```bash
# the proxy's own interpreter, found by argv TOKEN (not `pgrep -f`, which
# matches any shell that merely mentions the string — including this one)
PID=$(python - <<'EOF'
import pathlib
for e in pathlib.Path("/proc").iterdir():
    if e.name.isdigit():
        try: argv=[a.decode() for a in (e/"cmdline").read_bytes().split(b"\0") if a]
        except OSError: continue
        if "aisquare.explainability.claude_proxy" in argv: print(e.name); break
EOF
)
EXE=$(readlink -f "/proc/$PID/exe" 2>/dev/null) || EXE=python   # no sudo needed for your own
$EXE -c "import importlib.util as u; src=open(u.find_spec('aisquare.explainability.claude_proxy').origin).read(); print('junk-run suppression:', 'IN FORCE' if '_has_valid_correlation' in src else 'MISSING')"
# IN FORCE            -> good
# MISSING             -> THIS BUILD lacks the suppression; extra Runs will appear
#                        in the dataset. Not a statement about the version number:
#                        a 1.0.6-labelled checkout can print IN FORCE (block below)
# ModuleNotFoundError -> $EXE is NOT the proxy's interpreter; see below
```

> ⚠️ **[verified-train, @9bbc8ed7 2026-08-18] This line used to gate on `sudo`,
> and the gate defeated the check it guarded.** It read
> `sudo -n true … && EXE=$(readlink -f /proc/$PID/exe) || EXE=python`. On a box
> without passwordless sudo — this one — the test fails and it takes the
> fallback, `EXE=python`, WHICH IS EXACTLY "whatever you last installed", the
> thing the comment above says this block exists not to answer for.
>
> **The sudo was never needed.** `/proc/<pid>/exe` is readable without privilege
> for your OWN processes, and you start this proxy yourself:
>
> ```text
> ls -l /proc/20753/exe -> lrwxrwxrwx 1 work work … -> /usr/bin/python3.10
> that interpreter      -> junk-run suppression: IN FORCE
> ```
>
> Measured against the proxy running on this box: the correct answer was
> available with no privilege at all, and the sudo gate threw it away and
> substituted a `ModuleNotFoundError` — a third outcome the two comment lines
> did not cover. Reading the symlink directly and falling back only when the
> READ fails keeps the privileged case working and stops inventing a wrong
> answer in the ordinary one.
>
> If you do see `ModuleNotFoundError`, the fallback was taken: `$EXE` has no
> `aisquare.explainability`, so the answer would have been about your shell's
> Python rather than the proxy's. Find the proxy's interpreter by hand before
> trusting anything this block prints.

Verified to discriminate: the live proxy reports `IN FORCE`; the same check run
against a fresh `aisquare==1.0.6` reports `MISSING`.

> ⚠️ **[verified-train, @8dd460fb 2026-08-18] That sentence is true and it is
> not the whole truth: the version number is not the oracle, the capability
> check is.** The proxy running on this box reports `IN FORCE` — and it is
> **`1.0.6`**. Measured at pid 20753 (up 33.4h): its interpreter imports
> `…/AISquare-Explainability-SDK/aisquare/explainability/claude_proxy.py`, an
> *editable* install (`__editable__.aisquare-1.0.6.pth`) of the `bb88bb5`
> checkout, whose `pyproject.toml` says `version = "1.0.6"` because the
> suppression branch never bumped it. Two artefacts here both call themselves
> `1.0.6` and only one carries the symbol:
>
> ```text
> live editable source, bb88bb5    218592 bytes  sha256 f063820d…  2 hits
> pip-cached aisquare-1.0.6 wheel  218592 bytes  sha256 f063820d…  2 hits  <- byte-identical, built from that checkout
> pip-cached aisquare-1.0.3 wheel   49105 bytes  sha256 39d46716…  0 hits
> ```
>
> **What follows, in the order you will meet it.**
> **(a)** "`1.0.6` does not have it" above is true of the *PyPI release* and
> false of the *checkout the overnight receipts used*. It is a claim about
> provenance wearing a version number.
> **(b)** The troubleshooting line does not invert: `IN FORCE` does **not**
> mean you are on `>=1.1.0`.
> **(c)** **If the check already prints `IN FORCE`, do not reinstall.**
> `pip install 'aisquare>=1.1.0'` would replace a working editable install with
> a PyPI build and detach the proxy from the tree that produced every overnight
> receipt; run against a *live* proxy it also rewrites the disk under a process
> that already imported the old code. Reinstall is the remedy for `MISSING`,
> never routine tidying.
> **(d)** No `1.1.x` artefact exists on this box — pip cache, uv cache and every
> `aisquare-*.dist-info` under `/home/work` are `1.0.3` or `1.0.6`, so that
> install needs network. **[verified-train, coder2 `8dd460fb`, 2026-08-18]**
> `@9bbc8ed7` showed the network is open and `1.1.0` installs; I then pulled the
> real PyPI wheel (`pip download aisquare==1.1.0 --no-deps`) and diffed it, and
> the flat "byte-identical to `1.1.0`" claim was too strong:
>
> ```text
> claude_proxy.py    bb88bb5   218592 bytes  sha256 f063820d…
>                    PyPI 1.1.0 221830 bytes  sha256 9484a9ca…   NOT byte-identical (+158 lines)
> _has_valid_correlation() body   1226 chars  sha 6ebd3b0e   IDENTICAL in both
> its gate  bb88bb5     correlated = _has_valid_correlation(pipeline_id, traceparent)
>           PyPI 1.1.0  correlated = _is_cc_mode() and _has_valid_correlation(pipeline_id, traceparent)
> ```
>
> So the *function* reproduces exactly; the *file* is a later build that adds a
> `_should_adopt_cc_session` path and now scopes the suppression behind
> `_is_cc_mode()`. That gate is not per-request: `_is_cc_mode()` returns
> `PROXY_MODE == "claude_code"`, and `PROXY_MODE` is read **once** at process
> start from `AISQUARE_PROXY_MODE` (default `claude_code`), so for a `claude_code`
> proxy the `and` short-circuits to the same `_has_valid_correlation(...)` gate
> bb88bb5 has and the suppression fires identically — `1.1.0` is a sound prod pin.
> **The caveat this exposes is real in the code, and doubly hard to reach
> through the CLI** (`@9bbc8ed7` measured the reachability; my first wording
> overclaimed it): `1.1.0` scopes the suppression to `claude_code` mode where
> bb88bb5 applied it unconditionally, so a proxy started
> `AISQUARE_PROXY_MODE=creator` gets **no** junk-run suppression from `1.1.0`.
> But it is reachable only if such a proxy is actually deployed AND the CLI's own
> check is bypassed: `probe_proxy` pins `_EXPECTED_MODE = "claude_code"` and
> refuses a creator proxy by name (`proxy at <url> runs mode 'creator', need
> 'claude_code'`), even though the default `proxy_url` is the creator port
> `http://127.0.0.1:9090`. On the reference box today the single running proxy is
> `claude_code` on 9190 and 9090 is empty, so there is nothing to act on here now
> — the finding is a property of a creator-mode deployment, not of this box. The
> default cutover path is `claude_code` and is unaffected. Whether
> your prod proxy is in `claude_code` mode is one `/health` read
> (`"mode":"claude_code"`), not an assumption. The half the evidence row rests
> on is unchanged: the build now serving is byte-identical to the checkout the
> receipts used — that is a `1.0.6`-labelled `bb88bb5`, not `1.1.0`.

**[verified-stg]** Health check — run it yourself, do not assume:

```bash
curl -s http://127.0.0.1:9190/health
{"status":"ok","service":"aisquare-proxy","mode":"claude_code","governance":"gateway"}
```

> ⚠️ **[verified-train, @9bbc8ed7 2026-08-18] A healthy `/health` is not proof
> you started it.** This check identifies a *payload*, never a *process*. Any
> program serving those fields on that port satisfies it — measured with this
> repo's own forty-line `tests/proxy_stub.py`, which answers
> `{"status":"ok","service":"aisquare-proxy","mode":"claude_code"}` and is a
> test fixture. A proxy left running by yesterday's cutover passes identically,
> and then model traffic goes to the OLD one.
>
> @8dd460fb hit exactly this by walking the runbook with §3 skipped: the row
> still read healthy because something else on the box was serving 9190. That
> warning was recorded in §5, about the `doctor` row — the *consequence*. This
> is the step that *causes* it, so confirm the owner here, where you can still
> tell the difference between "mine" and "someone's":
>
> ```bash
> ss -ltnp | grep 9190                  # note the pid=
> ps -o pid,etime,args -p <that pid>    # ELAPSED is the tell
> ```
>
> **Ask the age, not the identity.** "The PID should be the one you started" is
> useless advice to someone who has not started it yet and has no PID to
> compare against. `ELAPSED` needs no prior knowledge: a proxy you started for
> this cutover is minutes old, and anything older was already there.
>
> **[verified-train, @9bbc8ed7 2026-08-18 06:00] There is one on this box as you
> read this.** Measured, read-only, nothing started and nothing killed:
>
> ```text
> LISTEN 127.0.0.1:9190  users:(("python",pid=20753,fd=13))
> 20753  ELAPSED 1-02:35   (a day and two hours)
>        ./.venv/bin/python -m aisquare.explainability.claude_proxy
> /health -> {"status":"ok","service":"aisquare-proxy","mode":"claude_code",...}
> ```
>
> **Do not compare the absolute start time — it is not reproducible on this
> box.** `ps` does not store a start time, it computes one:
> `lstart = btime + starttime_ticks/HZ`, and `etime = uptime - starttime_ticks/HZ`.
> Both come from `/proc/uptime`, which on WSL2 does not track wall clock. Three
> readings of THIS process across forty minutes gave three different start
> times — 03:13:35, 03:14:45, 03:18:07 — drifting forward. Measured over a
> timed interval rather than inferred: in 48.2s of wall clock `etimes` advanced
> **45s** and `lstart` advanced **3s**. So the age runs about 6% slow and the
> start time walks forward by roughly four minutes an hour, which is what the
> three readings above show. The only fixed quantity in that arithmetic is the
> process's own `starttime` in `/proc/<pid>/stat`, which nobody quotes.
>
> **That is why the test is orders of magnitude and not minutes.** A day-old
> proxy against one you started five minutes ago survives any drift this box
> produces; "started at 03:13" does not survive being read twice.
>
> A day old, and it satisfies every check in this section. So on this
> machine §3 goes green **whether or not your start succeeded**, and `ELAPSED`
> is the only line above that says so.
>
> This is not a defect in the CLI. Asking the kernel who owns a socket is
> `ss`'s job, not a diagnostic's; a payload check is the right check for "is
> the interface on this port the one I expect". It just is not the same
> question as "is it mine".

**Three of those four fields matter now.** The CLI refuses any `/health` whose
`service` is not `aisquare-proxy`, whose `mode` is not `claude_code`, or whose
`status` is present and not `ok` — and it **fails open**, so a wrong-mode proxy
produces *untraced launches with no error*, not a failure. Silence is the failure
mode. Check `/health` yourself.

`status` was read only from this cycle (@9bbc8ed7). Until then the CLI inspected
`service` and `mode` and **discarded the field the proxy uses to say it is
unwell**: a proxy answering `{"status":"degraded"}` with the right service and
mode was reported "proxy healthy" and model traffic routed to it. The check is
deliberately tolerant — an **absent** `status` stays healthy, so an older proxy
build keeps working, and only an explicit non-`ok` value is rejected. `governance`
is still not read; that is recorded as unexamined rather than as approved, and one
sample from one proxy is all anyone has.

> **Port 9090 on this box belongs to a long-lived creator-mode proxy. Never kill
> it.** Use `AISQUARE_PROXY_PORT` and point `explainability.proxy_url` at your
> port. (Nothing was listening on 9090 overnight on 2026-08-17, but treat the
> port as reserved.)

---

## 4. Turn tracing on — ONE command (2 min)

**[verified-train]** This replaces the two `aisquare config set` calls an
earlier version of this runbook used. Those still work, but they cannot set a
target, and everything downstream (`register`, `doctor --live`, `ship`) is
target-aware.

```bash
aisquare explainability enable --target prod \
  --gateway-url "$EXPLAINABILITY_GATEWAY_URL" \
  --key-env EXPLAINABILITY_API_KEY \
  --proxy-url http://127.0.0.1:9190
```

Run by me against staging, output verbatim (prod values will differ):

```
✓ tracing enabled for target 'stg'
  gateway:  https://stg-explainability-api.aisquare.studio
  key from: $EXPLAINABILITY_API_KEY (set)
  proxy:    http://127.0.0.1:9190
  agents:   aisquare-planner, aisquare-coder, aisquare-runner
  next:     aisquare doctor --live
```

**`--key-env` names the VARIABLE, never the key.** The key itself is never
written to config; the config records which env var to read. Nothing here can
leak a credential into a file people paste into tickets.

Default `proxy_url` is `http://127.0.0.1:9090` — always pass `--proxy-url`, and
never 9090 on this box (§3).

**Rollback:** `aisquare explainability disable` (§7).

---

## 4b. Register the agent identities — **without this, spans are rejected** (2 min)

The earlier runbook had no registration step at all. That is the omission that
breaks a cutover while every other step reports success: unregistered names are
refused by the gateway with **409 `agent_not_registered`**, so traces leave the
machine and land nowhere until the name is registered. They are *retried*, not
discarded — see §1a for why that distinction decides what you do about it.

```bash
aisquare explainability register --target prod
```

Prints each agent name with its `publication_id`, and is **idempotent** — a
second run returns the same ids rather than creating duplicates.

> **[verified-stg by coder1, NOT re-run by me]** Against staging this returned
> `aisquare-planner` / `aisquare-coder` / `aisquare-runner`, all
> `publication_id 169`, idempotent on a second run. I did **not** execute it
> myself: it mutates shared workspace state, and the standing rule this shift
> has held is that mutations wait for a human. The command's flags
> (`--target`, repeatable `--role`) I did verify. Auth shape is handled for you
> — `X-API-KEY`, never `Authorization`; a fronting layer 401s the whole call if
> you send the latter.

**Rollback:** none needed — registration is additive and idempotent. If a name
is wrong, register the correct one; the wrong one simply goes unused.

---

## 4c. Verify registration from the workspace side — read-only, no studio key (2 min)

§4b registers the names and §5 proves a Run leaves the machine. Neither shows
you the gateway's own verdict on the names. There is a read-only view that
does, and it needs only the ordinary workspace `X-API-KEY` — no studio key, no
dashboard JWT — so it is the one governance check you can run before Jatin has
a studio credential in hand.

> **[verified-stg, @8dd460fb 2026-08-18]** Measured against staging, read-only
> GETs, nothing created.

**The workspace id is `31`.** It is not in the env file and not in any
response body; the gateway derives it from the key server-side. To find it
without guessing, use the gateway as an oracle — a workspace-scoped route
returns `200` only for the caller's own workspace and `403 "API key is not
scoped to this workspace"` for every other id:

```bash
set -a; source /home/work/.config/aisquare/explainability-<env>.env; set +a
for id in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "X-API-KEY: $EXPLAINABILITY_API_KEY" \
    "$EXPLAINABILITY_GATEWAY_URL/v1/workspaces/$id/my-capabilities")
  [ "$code" = 200 ] && export WS=$id && echo "your workspace id: $WS"
done
# staging answered 31, and only 31, in 1..40. Prod may differ — re-derive it.
# $WS is used by every workspace-scoped call below. If it is empty, widen the
# range: the loop only searched 1..40.
```

> **A word before you `curl` `my-capabilities` directly, because its body looks
> alarming and is not.** On staging it returns `"bypass": true` beside a full
> set — `view_runs`, `attach_rule_book_to_agent`, `replay_run` all `true`. On a
> page about governance that reads like the gate is wide open. It is not: that
> is the **expected** shape of a *workspace* key (`@9bbc8ed7`, from
> `gateway/auth.py` at `bb88bb5`) — there is no human role to gate, so the capability map is
> permissive, and it is a **separate gate** from studio-scoped enforcement.
> Measured here: `my-capabilities` reports `view_runs: true` while
> `GET /v1/studios/169/runs` on the same key returns `403`. The capability flag
> is what the principal *may* do; the studio guard is what this *key* can reach,
> and they disagree by design. `bypass` here is not a statement about whether
> governance is enforced — that is §1's routing story, a different mechanism.

Then read the gateway's rejection view for that workspace:

```bash
curl -s -H "X-API-KEY: $EXPLAINABILITY_API_KEY" \
  "$EXPLAINABILITY_GATEWAY_URL/v1/workspaces/$WS/ingest-rejections"
# {"rejections":[]}   on staging right now
```

**`[]` IS NOT PROOF OF CLEAN DELIVERY, and reading it as such is the trap.**
The store behind this route is in-memory and **windowed to 900 seconds**
(`gateway/ingest_rejections.py`, `_DEFAULT_WINDOW_SECONDS = 900`). So `[]`
means "no name was rejected in the last 15 minutes" — which is exactly what an
**idle** workspace returns, indistinguishable here from a healthy one. Nothing
is ingesting from this box until §5 runs, so the empty view on staging is the
idle case, not a verdict.

To make it a verdict, read it **inside the window, right after a real ingest**:

```bash
aisquare explainability register --target <env>     # §4b, once
# ... run §5 so a traced Run actually ships ...
curl -s -H "X-API-KEY: $EXPLAINABILITY_API_KEY" \
  "$EXPLAINABILITY_GATEWAY_URL/v1/workspaces/$WS/ingest-rejections"
```

Now a non-empty result is the direct read of §4b's failure mode. Each row is
`{code, agent_name, count, last_seen}`, and the `code` names the remedy:

- `agent_not_registered` — the name is known but unmapped. Re-run §4b; it is a
  registration race and it drains on retry.
- `no_agent_identity` — a span carried no `agent.name` at all. This one does
  **not** drain on retry; it is an integration bug, not an onboarding gap.

If instead the roster names do **not** appear after an ingest, that is the
green reading you actually want: the gateway accepted them and routed them.

**Cross-check, and it closes a loop.** This workspace owns exactly **one**
studio — `169`, `has_runs: true` — read from `/v1/workspaces/$WS/studios`. That
is the same `publication_id 169` §4b reports for all three roster names, from
the opposite side of the boundary: the register call maps the roster *to* 169,
and 169 is the studio this workspace *owns*. `GET /v1/studios` lists sixteen
studios, but that is what the key can **see**, not what it owns; the owned set
is the one studio, and mixing the two is how "sixteen owned studios" got onto
the board earlier tonight.

This is also the workspace-side confirmation of §1e's "leave
`EXPLAINABILITY_STUDIO_ID` unset": the value currently in the env file matches
neither the owned studio `169` nor any of the sixteen listable ids, so a pin
built from it points at a studio this workspace can neither reach nor list.

### The success side — confirm runs LANDED, not just that none were rejected

`ingest-rejections` above is the failure view; it is empty when nothing is
wrong **and** when nothing is flowing. To confirm the integration actually
worked — model traffic reached the gateway and insights shipped, both
attributed — read the workspace's own record of its runs. Same workspace key,
no studio credential:

```bash
curl -s -H "X-API-KEY: $EXPLAINABILITY_API_KEY" \
  "$EXPLAINABILITY_GATEWAY_URL/v1/workspaces/$WS/credits/usage/by-run?since_days=1&limit=100"
```

> **[verified-stg, @8dd460fb + @9bbc8ed7 2026-08-18]** On staging this returns
> the workspace's runs, each `{run_id, agent_name, studio_id, calls, credits,
> last_at}`. Measured: **every run under studio `169`**, attributed
> `aisquare-planner` / `aisquare-coder` / `aisquare-runner` / `aisquare-cli` —
> the roster, each name distinct. That is the whole integration confirmed from
> the gateway's side: per-role identity survived to delivery, and the
> `aisquare-cli` fallback has real runs, so auto-discovery accepted the
> unregistered name.
>
> **It covers BOTH lanes.** A proxy-lane launch and a client-lane
> `explainability ship` both land here — measured, the same `by-run` call
> carried a traced launch (`agent_name aisquare-runner`) and three shipped
> insights (the attributed one as `aisquare-coder`, two unattributed as
> `aisquare-cli`). So one call answers both "did my model traffic land" and
> "did my insights land".
>
> **`run_id` is the OTel trace id, NOT the pipeline id the board joins on.**
> Do not grep this view for your `AISQUARE_PIPELINE_ID` / board session id and
> conclude it is broken when you do not find it — the pipeline id is not a
> field in this projection. Match a run by its `agent_name` and `last_at`, or
> by the OTel `trace_id` if you captured it. **This is why the join is not yet
> closed:** the shared key the board joins on is absent here, so "board rows
> join gateway Runs on a shared key" still needs the studio-scoped Run detail,
> which `403`s. This view proves the runs EXIST and are ATTRIBUTED, not that a
> given board row maps to a given Run.

One more read answers the billing question a hard enforcement band would
raise: `GET /v1/workspaces/$WS/credits/balance` → `{granted_credits,
balance_credits, low_balance, band}`. On staging: granted `1000`, band `ok`.
A `hard` band under hard enforcement is what returns `402` at ingest; `ok` is
clear.

---

## 5. The one command that proves it green (1 min)

**[verified-stg]** `doctor --live` is the real round-trip — gateway ready, key
accepted, a test span actually ingested — not a ping. Run it, and read the
`ingest` line:

```bash
aisquare doctor --live
```

Run by me against staging, the explainability section verbatim:

```
✓ explainability: tracing on, target 'stg' via config
✓ explainability sdk: SDK present (console script)
✓ explainability config: target 'stg' -> https://…  (config), key from $EXPLAINABILITY_API_KEY, identities: aisquare-planner, aisquare-coder, aisquare-runner
✓ explainability redaction: standard — credentials are removed from insights leaving this machine …
✓ explainability proxy: claude_code proxy healthy at http://127.0.0.1:9190
✓ explainability gateway: https://…/ready — HTTP 200
✓ explainability ingest: test span accepted as 'aisquare-planner' (HTTP 202)
⚠ explainability governance: traces land, but runs stay UNGOVERNED until a rule book is attached to the studio (an ingest key cannot verify this from here)
    → Attach a rule book to the studio in the dashboard, then re-run aisquare doctor --live
✓ sdk:gateway_live: Alive        ✓ sdk:gateway_ready: Ready
```

> ⚠️ **[verified-train, coder2 `8dd460fb`] The proxy row proves a proxy is
> answering, not that YOU started one.** Walking this runbook end to end with §3
> deliberately skipped, that line still read healthy — another process on the box
> was serving 9190. So on a re-run, or on a machine where an earlier cutover left
> a proxy up, a green proxy row can hide a §3 that silently failed, and the
> traffic would go to the OLD proxy. If you did not watch §3 start it, confirm
> whose it is before trusting this row: `ss -ltnp | grep 9190`.

**`ingest: test span accepted … (HTTP 202)` is the line that matters.** It is
the only one that proves the key, the gateway and the identity all work
together. The `governance ⚠` is expected until §1 is done and is not a failure
of this step.

One caveat on `sdk:sdk_version`: that reports the SDK **the CLI** imports, which
is not necessarily the build the **proxy** runs (§3 pins that separately and
gives its own check).

Quick read afterwards, without the network:

**[verified-train]**

```bash
aisquare explainability status; echo "exit=$?"
```

Green looks like this — **[verified-train]**, captured from the built binary
with tracing on and the proxy up. It has grown since this runbook was first
written; if you are comparing line-for-line, compare against this:

```
enabled:  True
target:   stg
gateway:  https://… [config]            <- your prod value; [config] is where it came from
key:      $EXPLAINABILITY_API_KEY is set
proxy:    http://127.0.0.1:9190
identity: aisquare-{role}
agents:   aisquare-planner, aisquare-coder, aisquare-runner
probe:    claude_code proxy healthy at http://127.0.0.1:9190
shipping: off — nothing is captured (aisquare init --explainability to turn it on)
spool:    0 queued, 0 sent, 0 dead-letter — /home/work/.aisquare/explainability/queue
redaction: standard — credentials are removed from insights leaving this machine (paths and hostnames are kept); local capture keeps what you typed
exit=0
```

> ⚠️ **[verified-train, planner `dfd9a883`, 2026-08-18] That spool path used to
> read `/home/you/…`, inside a block labelled captured-from-the-binary.** No run
> on this machine prints that, so the transcript was partly hand-edited — which
> is the one thing a `[verified-train]` block must not be, because its whole value
> is that a reader can diff their output against it line for line, as the sentence
> above invites. Note that the same block anonymises the gateway as `https://…`:
> **an explicit ellipsis is honest, a plausible-looking substitute is not.** If a
> value must be hidden inside a captured block, hide it visibly.

The path after the spool counters is where those records actually sit, and it
is there because the counter says *spool* while the directory is `queue` — a
mismatch that cost a reviewer ninety minutes and produced a false "the spool is
empty" while the record was on disk. Read the counter; if you go looking, that
is the directory.

The two lines that depend on YOUR environment are `gateway`, `key` and that
path; the
sandbox run that produced this had neither set and showed `(unset)` and `is NOT
set`. Everything else is what a correctly wired machine prints.

`status` exits non-zero **only** when tracing is enabled *and* the probe fails —
the precise state in which launches would silently fall back to untraced. That
is what makes it the right single check.

> **[verified-train]** `status` honours `--json` now (it used to print human
> text under the flag). `aisquare --json explainability status` returns a real
> payload — `enabled`, `target`, `gateway`/`gateway_source`,
> `key_env`/`key_set`/`key_source`/`key_origin` (never the key itself),
> `proxy`, `identity`, `agents`, `probe`, `shipping`,
> `redaction` — so the cutover can be scripted rather than eyeballed.
> **Branch on `key_source`, not on `key_env`.** `key_env` is the variable the
> target NAMES, set or not; `key_source` is where the key actually came from
> (`env`, `file`, or `unset`) and `key_origin` renders it — `$VAR` or the path
> of `~/.aisquare/explainability-key`. On the single-deployment machine §1
> produces, the key comes from the FILE while `key_env` still reads
> `EXPLAINABILITY_API_KEY`, so a check that rotated or debugged the named
> variable would be working on a credential that is not in play. The spool
> counters live **inside** `.shipping`, not under a top-level `.spool`. That key
> list is now asserted against the real payload in both directions by
> `tests/test_runbook_json_paths.py`, so it cannot drift unnoticed again.

Then make one real traced call and watch the proxy log:

```bash
# Shell-agnostic since the POSIX-quoting fix — bash, zsh, sh and dash all work.
eval "$(aisquare explainability env runner --session-id "$SESSION_ID")"
claude -p "reply with the word OK and nothing else"
```

> ✅ **[verified-train] FIXED — this `eval` is shell-agnostic now.** It used to
> be bash-only: `explainability env` emitted `$'…'` quoting, and under `dash`
> the `$` was taken literally, so the launch died with `API Error: Invalid URL`
> and exit 1 instead of degrading to untraced. That was a fail-open violation
> and it is gone — the emitter uses POSIX single-quoting, which carries a real
> newline in every shell. Re-measured on the current train, under `/bin/sh`
> (which is `dash` here):
>
> ```
> BASE=[http://127.0.0.1:9190]
> HDR=[X-Agent-Name: aisquare-runner
> X-Pipeline-Id: dashcheck]      exit=0
> ```
>
> So Makefile recipes, systemd units, CI steps, cron and
> `subprocess(..., shell=True)` are all fine. Kept as a note rather than
> deleted because anyone on an **older build** still has the old behaviour, and
> the symptom is worth recognising.

**Pass `--session-id`.** **[verified-train]** Without it the pipeline id is a
fresh random UUID on every invocation — two consecutive calls produced
`6fa4fd37-…` then `66e7ee90-…`, i.e. two separate Runs. With
`--session-id d124bc26` the header is exactly `X-Pipeline-Id: d124bc26`. One
session = one Run only if every seam passes it.

---

## 5b. Deliver the CLI's own insights — **once to set up, then forever** (2 min)

Model traffic flows through the proxy on its own. The CLI's **insights** —
prompts, notes, task events — do not: they **spool to disk** on the primary
path and leave only when you drain them. Skip this and half the integration is
silent while everything looks healthy.

```bash
aisquare init --explainability      # turn capture on — ONCE
aisquare explainability ship        # drain the spool — RECURRING, see below
```

> ⚠️ **`ship` is a recurring obligation, not a cutover step.** This is the one
> instruction in this document whose tense matters. Nothing drains the spool
> automatically — the only caller of the shipping path anywhere in the CLI is
> this command, deliberately, because the primary path is not allowed to do
> network I/O. So an operator who runs the cutover exactly as written ships the
> insights captured before 08:05 **and then never again**: every prompt, note
> and task event after that sits on disk while the proxy lane keeps working
> perfectly. Model traffic flows, `status` is green, Runs appear — and clause
> two of the north star is true only of the first few minutes.
>
> **If §4b registered the roster late, the insights already shipped are not
> lost — they are queued.** The client lane attributes an unattributable Run to
> `aisquare-cli`, and until that name is registered the gateway answers
> `409 agent_not_registered`, which the sweeper treats as a transient
> onboarding race and retries indefinitely rather than dead-lettering. So the
> recovery is the registration itself: run §4b (or `aisquare explainability
> register`, which sends the whole roster including `aisquare-cli`) and the
> backlog drains on the next sweep with no further action. **[read from
> `aisquare/explainability/sweeper.py` and `gateway/main.py` at `bb88bb5` / `aisquare` 1.0.6; not
> observed against a live gateway.]**
>

**Run it on a timer** — and the obvious crontab line ships nothing, forever,
while reporting success. Three facts combine: cron has almost no environment,
so the key is not in scope; `ship` **exits 0 when it cannot ship** (correct —
"no key means nothing logged as an error"); and crontab lines are written with
output discarded. **[verified-train]** measured under `env -i`, which is how
cron runs, not a login shell:

```text
aisquare explainability ship            exit=0   ← what cron reads today
aisquare explainability ship --strict   exit=1   ← what cron reads now
```

So use `--strict` in a timer: it exits non-zero when the run could not ship at
all — shipping off, no gateway, no key, or the extra missing — while a
**deferral** (gateway unreachable) stays quiet, because the next tick is the
retry and mail about a transient outage is mail you learn to ignore.

**A wrapper script, not a bare crontab line**, because the key must come from
the env file and never from the crontab, and because a script is something you
can run once by hand to check. Save as `~/.aisquare/ship-insights.sh`,
`chmod +x`:

```bash
#!/bin/sh
set -a
. "$HOME/.config/aisquare/explainability-stg.env"   # your env file; 0600
set +a
exec /ABSOLUTE/PATH/TO/aisquare explainability ship --strict
```

`set -a` matters: without it the file's values are shell variables, not
environment variables, and the CLI never sees them. Use the **absolute** path
to `aisquare` — cron's `PATH` will not find it.

> ⚠️ **[verified-train, coder2 `8dd460fb`, 2026-08-18] Substitute that path;
> do not guess it.** This line read `/usr/local/bin/aisquare`, and on this
> machine **nothing is installed there** — nor in `/usr/bin`, nor in
> `~/.local/bin` (whose only match is `aisquare-proxy`, a different binary).
> Get the literal from your login shell:
>
> ```bash
> command -v aisquare
> ```
>
> On this box today that answers
> `/home/work/.pyenv/versions/3.12.3/bin/aisquare`, and **§0 does not move
> it**: §0 installs with `python3 -m pip`, and that interpreter's script
> directory *is* that pyenv `bin`. So the path is the same before and after the
> reinstall. Run as written with the old literal, under the model-cron line
> below, the wrapper exits **127** — `exec: /usr/local/bin/aisquare: not
> found` — before the CLI is ever reached.

Then:

```bash
*/5 * * * * $HOME/.aisquare/ship-insights.sh
```

No `>/dev/null`: a non-zero exit is the entire signal, and cron mails you the
reason.

Check it before trusting it, with the key in scope exactly as the wrapper puts
it there. A `0` means it really shipped; anything else prints why — and read the
number, because the failures mean different things: **127** is the `exec` line,
so the path above is wrong and the CLI never ran; **1** is the CLI itself
reporting a real reason through `--strict`; and **2** has *two* causes — the
wrapper dying before the `exec` (see the model-cron note below), **or the CLI
running and not having this command at all.** That second one is measured: with
the correct path substituted but §0 not yet run, `aisquare` is the older build,
and the wrapper exits **2** with `Usage: aisquare explainability …`. So run this
check *after* §0, and read a `2` with a usage line in it as "wrong build", not
"broken wrapper".

```bash
aisquare explainability ship --strict
env -i HOME="$HOME" LOGNAME="$LOGNAME" SHELL=/bin/sh PATH=/usr/bin:/bin \
  "$HOME/.aisquare/ship-insights.sh"; echo "exit=$?"
```

> ⚠️ **[verified-train, coder3 `9bbc8ed7`] Model cron; do not exceed it.** An
> earlier revision of this line was a bare `env -i sh -c …`, which clears the
> environment *entirely* — including `HOME`. The wrapper above reads
> `"$HOME/.config/…"` **inside** the script, so with `HOME` unset it sources
> `/.config/aisquare/…`, dies before reaching the CLI, and reports **exit 2**
> on a perfectly good timer. Measured, exit codes captured directly: bare
> `env -i` → **2**, "cannot open /.config/aisquare/…"; the line above → **1**
> with a real reason from `--strict`; and with the key deliberately out of
> scope → **1**, "no workspace key". `man 5 crontab` on this box: "SHELL is set
> to /bin/sh, and LOGNAME and HOME are set from the /etc/passwd line of the
> crontab's owner." A check stricter than the thing it models fails a correct
> setup, which teaches you to ignore it — the same way a check that is too
> lenient teaches you to trust a broken one. The explicit variables above are
> what cron really gives you, and clearing everything else still catches the
> two hazards that matter: no key in the environment, and a `PATH` that will
> not find `aisquare`.

> Then watch the drift:
>
> ```bash
> aisquare --json explainability status | jq -c .shipping
> ```
>
> That object carries `queued`, `sent` and `dead` — the same three numbers the
> human `spool:` line renders. Until this revision the command above read
> `jq -r '.shipping, .spool'`, and there has never been a top-level `.spool`:
> `jq -r` answers a missing key with the bare word `null` and **exits 0**, so in
> a cron it reads as output rather than as a mistake. Every jq path on this page
> is now asserted against the real payload by
> `tests/test_runbook_json_paths.py`.
>
> **[verified-train]** `status` shows `on → <gateway> — N buffered` when there
> is a backlog and `nothing buffered` when there is not, so a growing N is the
> signal that draining has stopped. That counter is the saving grace: the
> failure is invisible in every other surface but obvious here.
>
> **[verified-train, coder3 `9bbc8ed7`] Read it off the `spool:` line, not that
> one.** The `shipping:` reason is an ordered chain, and `N buffered` is the
> *last* branch — reached only once a key is in scope AND the extra is
> installed. In the two states where draining is actually broken it says
> something else and carries no count at all. Measured with five records
> captured: with no key, `shipping: on → … — but no workspace key: set $VAR`;
> with the extra missing, `shipping: on → … — buffering, the explainability
> extra is missing: …` — and in **both**, `spool: 5 queued, 0 sent, 0
> dead-letter`, with `jq -c .shipping` reporting `"queued":5`. Nothing is lost;
> the number simply moves off the line you were told to watch, in exactly the
> two states that make a cron timer ship nothing forever. The ordering is
> right — "you have no key" is more actionable than "5 queued" — so watch the
> surface that answers in every state, which is the `jq` command this section
> already gives you.
>
> Two details that bite a scripted drain:
> - `--limit` defaults to **500** records per pass. A backlog larger than that
>   needs repeated runs, or one run with a bigger `--limit` — a single
>   `ship` is not automatically "catch up".
> - `ship` exits non-zero **only when records were dead-lettered**. A deferral
>   is the design working, not a failure, so do not alarm on a delay — and
>   equally, **exit 0 does not mean the spool is empty**. Read the counter, not
>   the exit code.

**[verified-train]** Run by me with capture off, verbatim: `shipping is not
configured — nothing to do`, exit 0 — it declines cleanly rather than
pretending.

> ⚠️ **[verified-train] `init --explainability` needs the extra installed.** On
> a CLI without it the step declines with `Explainability not configured —
> explainability extra not installed`, and `ship` then reports `shipping is not
> configured` forever. Install `pip install 'aisquare-cli[explainability]'`
> first. A plain install traces model traffic and ships nothing, which is the
> silent half-cutover this step exists to prevent.
>
> **[verified-train]** Installing the extra over a NORMAL install is safe — I
> installed the CLI, then the SDK, and `aisquare --version` still answered. It
> is **not** safe over an `-e/--editable` developer checkout: the SDK's real
> `aisquare/` directory shadows the editable path hook and `aisquare.cli`
> disappears. That is a developer-machine hazard, not yours, but do not run the
> cutover from an editable checkout.

**[verified-train] Assert the destination, not the counts.** After shipping is
on, check that the client lane points where you think it does:

```bash
aisquare --json explainability status | jq -r .shipping.gateway
# must equal your PROD gateway URL
```

**[verified-train, planner `dfd9a883`] Read this as "confirm the resolved
gateway", not "detect a split brain".** It was written when the two lanes could
resolve *independently* — model traffic to prod while insights kept going to
staging. That fix landed: both lanes now resolve from the one active target, and
across three configurations — target `stg`, target `prod`, and a hand-pinned
stale top-level `gateway_url` — `.shipping.gateway` tracked the active target
every time. **I could not construct a state where it disagrees**, so treat the
old billing as retired: this tells you *which deployment you are actually
pointed at*, which is still worth one command before you trust the run. Counts
still cannot do that:
`2 sent` reads identically whichever gateway it went to. The two lanes —
proxy traffic and shipped insights — used to be able to point at DIFFERENT
deployments, with `status` reporting only the proxy lane's target; configure
shipping under a staging shell, then `enable --target prod`, and model traffic
moved while insights kept going to staging with nothing to tell you. Shipping
follows the active target now, so one switch moves both, and this line is how
you prove it rather than assume it.

**What `sent` means, and it is not what it sounds like:** handed to the SDK's
durable inbox, **not acknowledged by the gateway**. A green `sent` count with a
dead gateway is a correct report of a local handoff. Only a Run visible in the
Studio proves delivery.

**Rollback:** `aisquare init --no-explainability` stops capture. The spool is
left on disk, not deleted, so nothing already captured is lost.

---

## 5c. Prove the join — one id in three local places (3 min)

**Run this only after §4 and after at least one traced session has launched.**
Before that `~/.aisquare/explainability/joins.jsonl` **does not exist**, and
neither does its parent directory. That absence is correct, not a fault: the
writer creates the directory on its first append, and nothing has appended
because nothing has been traced. A reader who runs these commands too early
gets `No such file or directory` and cannot tell expected-absence from
breakage — which is why this section sits here and not next to §4.

**And "launched" is not the precondition — "its `SessionStart` hook has fired"
is.** The join record is written by the hook *inside* the agent, so a session
that is running and already has its id in the environment can still have **no
line in the join log**. Measured by `8dd460fb` walking this section: a stand-in
agent that printed its environment without firing the hook produced a real
pipeline id and an empty join log, and the check below came back `False` — the
document was right and the session was simply too young. If you get a
disagreement with an id in hand, let the agent do one turn of work and re-read
before concluding the join is broken.

This is the north star's third clause — *board rows join to gateway Runs on a
shared key* — and until now the runbook had no step that checked it. Three of
the four hops need no credential and are below. The fourth does, and is marked.

> ⚠️ **[verified-train, `9bbc8ed7`, 2026-08-18] The finder must select a TRACED
> claude, and an empty id must stop you — the earlier version did neither, and
> the pair produced a FALSE PASS.** On this box 12 processes match `claude`; the
> old loop took whichever `/proc` listed first, which had no
> `AISQUARE_PIPELINE_ID`. Hop 1 then printed nothing — easy to skim past — and
> hop 2 substituted that empty string into `grep`, **which matches every line in
> the join log and exits 0**. Measured: an empty pattern returned 2 of 2 rows on
> a two-row file. So a wrong process read as a proven join. The loop now requires
> the marker in the process's environment, and hops 1 and 2 refuse to run on an
> empty id.

**Anchor on the RUNNING session, not on the newest record.** Each launch mints
its own id, so `tail -1 joins.jsonl` names whichever session started last,
which is not necessarily the one you are looking at. Measured while writing
this: a second launch carried `29dc19d4…` while the newest join record still
said `9fa349b2…`. Start from the process and work outward.

```bash
# A TRACED claude, not merely the first claude — see the warning below.
PID=$(python3 - <<'EOF'
import pathlib
for e in pathlib.Path("/proc").iterdir():
    if not e.name.isdigit(): continue
    try:
        if b"claude" not in (e / "cmdline").read_bytes(): continue
        if b"AISQUARE_PIPELINE_ID=" in (e / "environ").read_bytes(): print(e.name); break
    except OSError: continue
EOF
)
[ -n "$PID" ] || echo "no TRACED claude is running — launch one (§4), or tracing is off"
ID=$(tr '\0' '\n' < /proc/$PID/environ | sed -n 's/^AISQUARE_PIPELINE_ID=//p')
[ -n "$ID" ] && echo "$ID"                                             # hop 1
[ -n "$ID" ] && grep -F "$ID" ~/.aisquare/explainability/joins.jsonl   # hop 2
aisquare --json team status                                            # hop 3
```

`team status` takes no `--as` — it resolves the board from the current
directory, so **run it from the repository**, not from a worktree or `$HOME`.
If it answered for another board it now says so on stderr (`reading board … —
this directory resolves elsewhere`); silence means the board is the one this
repository maps to. That asymmetry is real and unfixed: `team log` and
`note` route through `--as`, `team status`, `board` and `task list` cannot be
pointed at a board at all.

**[verified-train, @9bbc8ed7 2026-08-18] Run end to end under a throwaway
`AISQUARE_HOME` with a stub proxy — never against the operator's home.** One
traced session, all three hops, real output:

```text
running process  AISQUARE_PIPELINE_ID = 207fea48-f832-44de-b9ad-a52e3b4cd78c
joins.jsonl      pipeline_ids         = ['207fea48-f832-44de-b9ad-a52e3b4cd78c']
board            session ids          = ['207fea48-f832-44de-b9ad-a52e3b4cd78c']
ALL THREE AGREE: True
```

One id, three places, none of them needing the gateway.

> **[verified-stg, `9bbc8ed7`, 2026-08-18] And a fourth place that DOES need the
> gateway: the Run the insights actually shipped under.** The three hops above
> are local agreement. This one is delivery — a train build with
> `aisquare[explainability]` installed, capturing inside a session and draining:
>
> ```text
> board session id                 a9efb7d2-072b-4a80-b77e-c7360735935c
> ship "runs:"                     a9efb7d2-072b-4a80-b77e-c7360735935c
> SDK inbox                        12 rows dispatched, 0 errors, max retries 0
> ```
>
> **The negative control is what makes that mean anything**, because a run key
> that is merely *present* is indistinguishable from one that is *correct*: the
> same capture with `AISQUARE_PIPELINE_ID` unset ships under
> `aisquare-cli-unattributed` instead. Marker set, the session's id; marker
> unset, the shared bucket. Same rig, same target, one variable.
>
> `run_key()` reads that marker from the **ambient** environment — the processes
> that capture are children of the traced session and inherit its wiring — so a
> capture inside a real launch picks it up without being told. **[stand-in]**
> the measurement above exported the marker rather than launching a real agent
> around it; `d124bc26`'s receipt covers the real-launch half of the same chain.
>
> This still does not reach the studio. It shows the insight left under the
> session's key and the gateway accepted it, not that a Run with that id can be
> read back — hop 4 below is unchanged.

> ⚠️ **HOP 4 IS BLOCKED AND THAT IS BY DESIGN, NOT BY OVERSIGHT.** Reading the
> Run back from the studio — confirming the gateway holds a Run whose id is the
> one above — needs a **studio-scoped** credential. That is
> `tsk_01m0bx5e12m91jjqydxrk26a5h`, and until it closes **every delivery claim
> in this document stops at "the gateway accepted the bytes"**. Three hops
> same as the join being observed end to end, and nothing here should be read as
> upgrading that.
>
> **But the call that closes it is now named, and it is exactly one.**
> **[verified-stg, `9bbc8ed7` + `8dd460fb`, 2026-08-18]** The board's key is a
> first-class field on the gateway's Run record — `session_id` on
> `UIRunListItem` — returned by:
>
> ```text
> GET /v1/studios/{studio_id}/ui/runs        # 403 with a workspace key
> ```
>
> With a studio-scoped credential, compare each Run's `session_id` against the
> board's session ids and clause 3 is closed end to end. **The same response
> also answers "traced but ungoverned"** — each Run carries `is_governed` and a
> block of `policy_*` and `runtime_*` counters. One call, two of the three
> things this document says nobody has checked.
>
> ⚠️ **There is no credential-free substitute, and that is measured rather than
> assumed.** `session_id` appears in exactly two schemas across the whole
> published surface — `UIRunListItem` and its wrapper — and exactly one route
> returns either. §4c's `credits/usage/by-run` is workspace-scoped and reachable,
> but its Run rows do **not** carry `session_id`, which is precisely why the join
> stays invisible there. So the studio credential is **necessary** for the join,
> not merely convenient: if it is not provisioned, there is nothing else to reach
> for.
>
> ⚠️ **Present in the schema is not populated in the row.** Neither of us has
> *seen* a `session_id` value — the route refuses us. If it comes back null for
> your Runs, that is a different defect from the one this section describes, and
> it is the first thing to check once the credential exists.
>
> **An earlier version of this note called the key "write-only". It is not** —
> it is *workspace*-scoped, which fails differently and points at a different
> remedy. **[verified-stg]**: `GET /v1/studios` returns `200` and sixteen
> studios with the same key that `GET /v1/studios/<id>/runs` refuses. It reads
> workspace-scoped routes and is refused on studio-scoped ones.
>
> The reason is in `gateway/auth.py` (bb88bb5): `validate_ingest_api_key`
> "returns the base `studio_id` for studio keys, or **`None` for workspace
> keys**", and the studio guard rejects "any caller whose resolved studio does
> not match the studio named in the path" — so a workspace key, resolving to
> `None`, matches no studio and is refused on every studio-scoped route. Not
> for the right id, not for a studio the workspace owns. **The remedy is a
> studio-scoped key (the gateway's "legacy studio key") or the dashboard JWT —
> never a corrected id.**
>
> ⚠️ **AND THE TWO 403 STRINGS ARE ONE CAUSE, WHICH MATTERS BECAUSE ONE OF THEM
> ARGUES AGAINST THIS PAGE.** `policy/check/*` and `agent-rule-books` answer
> `{"detail":"Workspace does not own this studio"}` (the inline call sites);
> `/runs` and its siblings answer `{"detail":"Studio ID mismatch"}` (the folded
> guard). **[verified-stg]** — studios `169`, `144` and the pinned `21` all
> return the latter. "Studio ID mismatch" reads like an invitation to go fix an
> id, and §1 has just finished explaining that correcting
> `EXPLAINABILITY_STUDIO_ID` fixes nothing. Same wall, two sentences; see §1 for
> the write-side half.

---

## 6. What healthy looks like, and what is just noise

**A healthy run, in the proxy log** (**[verified-stg]**):

```
INFO [__main__] pipeline-session: opened pipeline_id=<your id> trace_id=<32 hex>…
INFO [httpx] HTTP Request: POST <gateway>/v1/traces/ingest "HTTP/1.1 202 Accepted"
INFO:     127.0.0.1:xxxxx - "POST /v1/messages?beta=true HTTP/1.1" 200 OK
```

One `pipeline-session: opened` per session, ingest `202`, messages `200`.
Overnight on staging: **70/70 ingest calls returned 202, zero non-202.**

> **What a verification label does and does not promise — this block and §5b's
> `status` block carry the same one and support different claims.** Every value
> above is per-run or per-deployment by construction: the pipeline id, the trace
> id, the gateway host, the port. So the promise here is **the shape** — these
> lines appear, in this order, with these statuses — and diffing your output
> against these characters is not a thing a reader can do or should try.
> §5b's `status` block is the other kind: on this machine its values *are*
> reproducible, its own prose invites a line-for-line comparison, and that is
> exactly why one hand-edited path in it was a defect worth fixing while the
> placeholders above are correct. **The label certifies that a block was
> captured; whether you can diff it depends on whether its values reproduce.**
> When you add one, say which kind it is — `8dd460fb` swept both runbooks for
> hand-edited values and found five suspect tokens, all five legitimate and four
> of them here, precisely because this is a shape.

Backlog check:

```bash
explainability-doctor          # on PATH — see the note below, NOT ./.venv/bin/
```

Healthy: `delivery_backlog [OK] dispatched=N`, `gateway_live [OK]`,
`gateway_ready [OK]`.

> ⚠️ **[verified-train, `9bbc8ed7`, 2026-08-18] This used to read
> `./.venv/bin/explainability-doctor`, which cannot run anywhere you will be
> standing.** §0 installs the CLI **non-editable into your python**, so there is
> no `.venv` in your home — and the repo checkout's `.venv` does not contain
> this script either, because it belongs to the SDK, not to `aisquare-cli`.
> Measured: missing in both, present on `PATH`. The bare name is right for the
> same reason the CLI itself prefers it — the console script "reaches an SDK
> installed anywhere on the machine". If the bare name is not found, the SDK is
> not installed; that is §5b's `explainability` extra, not a path problem.

**Known noise — do NOT treat as red.** **[verified-stg]** on a fully healthy
run. **These tables are a catalog across every tool this section covers, not the
output of any one command** — the first three rows are
`explainability-doctor`'s, the fourth is the proxy's log, the fifth is the test
suite. Running the command above shows you the first three and none of the
others, which is correct and not a missing check:

| Line | Verdict |
|---|---|
| `agno [MISSING] Install optional dependency: .[agno]` | expected — optional integration, unused |
| `openinference_agno [MISSING]` | expected — same |
| `openai_api_key [WARNING] Set OPENAI_API_KEY (required for RML extraction)` | expected — gateway-side, unrelated to tracing |
| `HEAD /api/hello … 405 Method Not Allowed` | expected — a client probe the proxy does not implement |
| `pydantic_settings IncompleteFieldDefinitionWarning` in the test suite | pre-existing, unrelated |

**Genuinely red** — same catalog, spanning `explainability-doctor`,
`aisquare doctor --live`, the proxy log and the CLI's own errors. The credit-band
row in particular comes from `aisquare doctor --live` (§5), not from the command
above:

| Line | Meaning |
|---|---|
| `policy check degraded (FAIL_OPEN)` | governance is off — go back to §1 |
| ingest returning anything other than `202` | traces are not landing |
| `409 agent_not_registered` | the agent name is not registered — §1a. Transient: the rows retry and drain once you register it |
| `409 no_agent_identity` | a batch with **no** agent name anywhere — a different, permanent case; the SDK dead-letters it after 3 attempts |
| `test span accepted … — but the workspace credit balance is in the 'hard' band` | **expected on a workspace nobody has granted credits to, which a brand-new prod workspace is.** Not a misconfiguration; see the note below this table |
| `probe: proxy unreachable` with `enabled: True` | launches are silently untraced — §3 |
| `API Error: Invalid URL` with `exit=1` | you are on a build older than the POSIX-quoting fix, **or** an `ANTHROPIC_BASE_URL` in your own environment is malformed — the CLI now names it on stderr just above the failure |
| `✗ context store error: duplicate column name: account` | you skipped §0b on a brand-new `~/.aisquare` — recovery below |
| `✗ the context store is corrupt: …/context.db` | the file is damaged, not misconfigured — the message carries the whole recovery, and `aisquare doctor` prints the same one |

> ⚠️ **The credit band rides on a successful ingest, and a fresh workspace starts
> in the worst one.** Read from source, not observed against prod — and the
> checkout matters, because there are two on this box three months apart:
> `/home/work/work/AISquare-Explainability-SDK` @ `bb88bb5` (2026-08-07), which
> is the revision §3 pins for the overnight receipts. Its
> `gateway/billing/enforcement.py`: `balance > warn_threshold` → no band;
> `<= warn_threshold` → `warn`; `<= HARD` (default 0) → `hard`. And in its
> words: **"A never-granted workspace (granted=0, balance=0) lands in 'hard'"**,
> because **"the gateway never auto-grants — credits are issued by our backend."**
>
> So on the prod workspace you stand up this morning, a `hard` band beside a
> `202` is the expected reading rather than a fault in anything you configured.
> The remedy is a credit grant from whoever owns billing — no CLI or config
> change clears it.
>
> **Whether your traces still land at a hard band depends on one server-side
> setting, so do not assume it.** `gateway/main.py` surfaces the band whenever
> it is not `ok`, and converts a hard band into a **402** only when
> `BILLING_ENFORCEMENT_MODE == "hard"`; that variable defaults to `soft`. So on
> a default deployment an exhausted workspace still answers `202` with the band
> in the body and the spans land — but if prod runs `hard`, ingest returns 402
> and the row above ("anything other than `202` → traces are not landing")
> is the one that applies. If you see a hard band, ask which mode prod runs
> before deciding whether you have lost anything. One more branch worth
> knowing: an ingest key with no workspace skips enforcement entirely — "can't
> bill or enforce → fall through (allow)" — so no band at all is also a
> possible healthy reading.
>
> **A 402 costs you latency, not data — both lanes retry it.** This is the part
> worth reading twice, because it is the difference between "we lost the morning's
> traces" and "they arrived late". Read from the pinned SDK,
> `/home/work/work/AISquare-Explainability-SDK` @ `bb88bb5`, `aisquare` **1.0.6**:
>
> - `aisquare/explainability/sweeper.py` puts **402 in `_TRANSIENT_STATUSES`**,
>   with its own comment saying why — *"402 — billing hard-band ('top up to
>   resume ingest'): recoverable"*. A transient status is retried with capped
>   backoff and **never dead-lettered** (`8dd460fb`).
> - **And that covers the proxy lane too, which is not obvious.** The proxy's
>   `ExplainabilityExporter` never posts a span itself: `_flush_locked` writes the
>   trace to the inbox and wakes the sweeper, and
>   `aisquare/explainability/main.py` builds it over that same inbox. So model-traffic spans are delivered by the same `InboxSweeper`
>   that treats a 402 as recoverable. The two lanes do **not** diverge at a hard
>   band — a question raised as plausible-and-unmeasured and closed here as a
>   negative.
>
> So on a hard-mode gateway with an ungranted workspace: `doctor` fails loudly,
> ingest returns 402, and **nothing is discarded** — traces and insights queue and
> drain once credits are granted. `ship --strict` staying quiet on a deferral is
> the design working, not a missed failure.
>
> **[unmeasured]** The inbox is a SQLite queue, so a *long* hard-band outage grows
> a file rather than losing spans. Nobody has measured how large, or whether
> anything bounds it. If credits stay unfunded for days, that is the thing to look
> at — not lost data.
>
> `ship` also does not preflight credits, on the same pinned build: the client
> package has no reference to credits at all, and `/credits/check` exists only
> server-side as `POST /v1/studios/{id}/credits/check`. The gateway's model calls
> it **"the FORCE-STOP … the agent run should be refused before it starts"** — a
> run-start gate, not a delivery gate, so even a future SDK adopting it would fail
> at *launch*, loudly, rather than as a silent client lane.
>
> **What `doctor` actually does at each band, measured through the real CLI
> against a stub gateway** (`8dd460fb`):
>
> | gateway answers | `doctor --live` | ingest row |
> |---|---|---|
> | `202` clean | exit 0 | `ok` — test span accepted |
> | `202` + band `hard` | exit 0 | **warn** — accepted, "but the workspace credit balance is in the 'hard' band" |
> | `402` | **exit 1** | **fail** — "test span not accepted: HTTP 402: {…insufficient_credits…}" |
>
> Neither band case is silent, and the 402 quotes the body. **So there is exactly
> one question to ask whoever stands the prod gateway up: what is
> `BILLING_ENFORCEMENT_MODE`?** You cannot misconfigure this from the CLI.

**[verified-train, planner `dfd9a883`]** **Recovering a wedged store.** If several
sessions raced a *first* open, the store can be left permanently mid-migration:
the DDL applied but its version bump did not, so every later attempt at that
migration fails again — this does not heal on retry and it takes every
`aisquare` command with it (`exit 1`). Characterisation is in
`docs/store-migration-race.md`; §0b prevents it. To recover:

```bash
mv ~/.aisquare/context.db ~/.aisquare/context.db.broken   # or $AISQUARE_HOME/…
aisquare status > /dev/null        # ONE process, alone — this re-migrates
```

`rm` in place of the `mv` works identically and destroys the evidence; there is
no reason to prefer it. This block is the `mv` form because **a fenced block is
what gets pasted** — the preference used to live in prose underneath, which is
the half nobody runs.

Verified by wedging a store to the failing state and back: before, `team status`
exits 1 with the message above; after, `PRAGMA user_version` reports the current
schema, `integrity_check` reports `ok`, and commands work. On a **new** machine
this costs nothing — there is no board data yet. On an **established** one it
discards that machine's local board (sessions, tasks, notes), so prefer §0b to
needing this.

**[verified-train, coder3 `9bbc8ed7`]** `aisquare doctor` now names this recovery
itself, in a form that does not destroy the file: `mv ~/.aisquare/context.db
~/.aisquare/context.db.broken && aisquare init`. Prefer it — the bytes survive
for whoever wants to look at them, and it is the same sentence doctor prints, so
it cannot drift from what actually works. Verified on both damaged states: a
corrupt file (`file is not a database`) and a store wedged mid-migration
(`duplicate column name`); each recovers to `✓ database: context.db is readable`
and leaves a configured `[explainability]` section untouched. Until this landed
doctor said "Re-initialise: `aisquare init`", which crashed with a traceback on
both and repaired neither.

### Known limitation to state out loud before anyone reads a dashboard

**[verified-stg]** **Task subagents and Workflow agents do not appear as separate
agents.** A session that spawned three Task subagents produced **one**
pipeline-session, **one** trace id and **one** AGENT span; the three subagents
left three `Tool:Agent` spans, but their own LLM spans hang off the *root*, so
per-subagent attribution is not recoverable. A Workflow is worse: one opaque
`Tool:Workflow` span, and the fan-out count is not recoverable at all.

The reason is structural — identity rides in process-level env
(`ANTHROPIC_BASE_URL` + `ANTHROPIC_CUSTOM_HEADERS`), and in-process agents
inherit the parent's headers verbatim. **Separation works per *process*:** three
concurrently live roles produced three distinct trace ids, correctly attributed.
Read per-role numbers as real; do not read per-subagent numbers, because there
are none.

---

## 7. Rollback

One line. Returns every session to untraced, changes nothing else:

```bash
aisquare explainability disable
```

**[verified-train]** Run by me, output verbatim:

```
✓ tracing disabled — sessions launch untraced, targets left in place
```

Targets are **kept**, so re-enabling is `aisquare explainability enable --target
prod` with no arguments to retype. After disabling, `status` reports enabled as
false while still showing the target and gateway, and
`aisquare explainability env <role>` exits `1` and emits no exports — so every
session launches untraced. Reversible in both directions, no other behaviour
change **to config**.

### Your shell is not config, and the order matters

§5 had you export `ANTHROPIC_BASE_URL` and `ANTHROPIC_CUSTOM_HEADERS`. `disable`
cannot touch those — a command cannot unset a variable in the shell that ran it,
and a launcher that deleted routing it did not set would be seizing a gateway
you own. So in **that** shell, config is off and launches still go through the
proxy. Then stop the proxy and they point at a dead port.

**[verified-train]** Measured with a stopped port, tracing disabled in config,
`aisquare launch coder`: the banner prints normally, the child still receives
`ANTHROPIC_BASE_URL=http://127.0.0.1:9299` and the header pair, and a request to
it fails to connect. Nothing warns at launch time — by design, because with
tracing off the launcher does not touch the environment at all.

So do this first, in this order:

```bash
unset ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS   # or just close the shell
```

**[verified-train]** With those unset the same launch shows the child receiving
neither variable. `disable` now prints this reminder itself when it can see that
your shell is still routing through the configured proxy.

**Then** stop the proxy: `Ctrl-C` the process from §3. **Not** the one on 9090.
Unsetting after stopping leaves a window where every launch from that shell
dies.

---

## Per-step verification and rollback, at a glance

| Step | Verify | Rollback |
|---|---|---|
| 0 Preflight | `git log --oneline -1` matches origin's head **and** `doctor` provenance names this repo — provenance names a PATH and an editable flag, never a revision, so it cannot tell you which branch that tree is on (empty ⇒ older than this train) | reinstall previous version |
| 0b Warm store | `PRAGMA user_version` on `~/.aisquare/context.db` is non-zero | none — the migration is forward-only |
| 1a Roster | response lists each agent + `publication_id` | re-register; registration is idempotent by name |
| 1b/1c Binding | `/v1/routing/resolve` returns a `studio_id` | unbind in the studio UI |
| 1d Rule book | no `FAIL_OPEN` warning on a traced call | detach the rule book in the UI |
| 2 Secrets | `stat -c %a <env file>` → `600` | `rm` the file |
| 3 Proxy | `/health` → `service=aisquare-proxy`, `mode=claude_code` — **then `ss -ltnp \| grep 9190` for the pid and `ps -o etime` on it**; the payload cannot say whose proxy answered, and ELAPSED is the only line that can (one has been up since Monday — see §3) | `Ctrl-C` (never port 9090) |
| 3 Proxy build | the §3 check prints `IN FORCE` | reinstall `aisquare>=1.1.0` |
| 4 Enable | `status` shows your target, gateway and proxy | `aisquare explainability disable`, then §7 |
| 4b Register | each agent printed with a `publication_id` | none needed — additive and idempotent |
| 5 Green | `doctor --live` → `ingest: test span accepted … (HTTP 202)` | `aisquare explainability disable`, then §7 |
| 5b Insights | `.shipping.gateway` equals your prod URL; `spool:` counts move after `ship` **and keep moving** — a growing `N buffered` means draining stopped | `aisquare init --no-explainability` (spool kept) |
| 5c Join | one id in three local places: the traced process's `AISQUARE_PIPELINE_ID`, its record in `joins.jsonl`, and its board row — anchor on the RUNNING session, not `tail -1` | none — read-only |

---

## Open items handed to the morning

1. **[blocker]** No agent name resolves to a studio on staging (§1). Prod will
   behave identically unless **every step of §1 is done, in order**. Until then
   runs are ungoverned — traced, but enforcing nothing.
   This line used to say "1a–1d". §1 has **five** sub-steps, and the fifth is a
   *"do not"* — **1e: do not pin `EXPLAINABILITY_STUDIO_ID`** — which is the
   exact mistake that produced the staging 403s this section diagnoses. A reader
   scanning for actions skips a prohibition anyway; a range that stopped counting
   at 1d hid it completely. The range is gone rather than corrected, because a
   number written beside a list is a constant that falls behind the list.
2. `EXPLAINABILITY_STUDIO_ID=21` should still be removed or corrected — but it
   is **not** the cause of the 403s, and correcting it alone will not fix
   governance. Measured: `GET /v1/studios` with the workspace key SUCCEEDS and
   lists 16 studios (144–169); `21` is not among them and `169` is. Yet **every**
   studio-scoped call 403s for **all sixteen**, `169` included, and unsetting the
   pin changes nothing. The workspace key simply cannot make studio-scoped calls.
   Governance needs a credential class we do not hold, not a config edit.
3. **[CLOSED]** `explainability status` honours `--json`. It used to print
   human text under the flag; it now returns a real payload — `enabled`,
   `target`, `gateway`/`gateway_source`,
   `key_env`/`key_set`/`key_source`/`key_origin` (never the key itself),
   `proxy`, `identity`, `agents`, `probe`, `shipping`,
   `redaction` — the spool counters are inside `.shipping`, and this list once
   claimed a top-level `.spool` that never existed.
   This matters more than it looks: §5b's split-brain assertion
   (`jq -r .shipping.gateway`) depends on it, and reading this item as still
   open would talk you out of the one check that can catch two lanes pointing
   at different deployments.
4. `POST /v1/agents/register-roster` was **not** executed by the author of this
   runbook — it mutates shared state and they left that call to a human. **But
   do not read that as "nobody ran it".** It HAS since been run against
   staging: `aisquare-planner`, `aisquare-coder` and `aisquare-runner` all
   returned `publication_id 169`, idempotent on a second run. So the names
   exist on the workspace default. If you had staged anything against those
   names, verify it rather than assume it — and note this is staging only, so
   §4b is still a real step for prod.
5. Prod gateway URL and key are **[unverified-prod]** throughout. Every
   *mechanism* here is verified against staging; the prod *values* are not.
6. **[CLOSED]** `explainability env` emitted bash-only `$'…'` exports and the
   launch hard-failed under `sh`/`dash`. Fixed on the train (POSIX
   single-quoting) and re-verified under `dash`: `BASE=[http://…]`, a real
   newline in the header pair, `exit=0`. §5 is shell-agnostic now.
7. **[CLOSED, with a measured refinement]** The proxy build is pinned.
   `aisquare>=1.1.0` is released and carries the junk-run suppression — the
   `_has_valid_correlation` **function** is byte-identical to the `bb88bb5`
   checkout the overnight receipts used (verified against the real PyPI wheel),
   so the mechanism is reproducible from PyPI and nobody needs the unreleased
   branch. The *file* is not identical — `1.1.0` is a later build that gates the
   function behind `_is_cc_mode()`; for a `claude_code` proxy the suppression
   still fires, and §3's (d) note carries the diff. `1.0.6`/`1.0.7` do **not**
   have the function at all. §3 carries a check that reads the *running* proxy and was
   verified to discriminate (live proxy `IN FORCE`, fresh `1.0.6` `MISSING`)
   — but @8dd460fb measured what supplies that `IN FORCE`, and it is a
   **`1.0.6`-labelled editable checkout of `bb88bb5`**, not a `1.1.x`
   install. The discrimination is by provenance, not by version number,
   and `IN FORCE` must not be read back as "we are on >=1.1.0". No
   `1.1.x` artefact exists on this box, so the byte-identity to the
   PyPI release remains unverified here. See the block in §3.
   Two caveats: neither SDK PR #362 nor #363 is on `origin/main` (which is at
   #433), so the fix reached the release by some other route — treat the
   RELEASE, not the PR, as the thing to depend on. And **#363 is gateway-side**
   (`gateway/rml/assumption_mining.py`), not shipped by pip at all, so no
   client install can carry it; it is a gateway deploy gate.
