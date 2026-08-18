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

> **Read §1 and §6 before you touch anything.** §1 is a blocker that staging
> hit and prod will hit identically. §6 is how you get out.

---

## 0. Preflight (2 min)

```bash
cd /home/work/work/aisquare-cli
git fetch origin && git log --oneline -1 origin/rc/v2026.08.18
```

Reinstall the CLI so the binary on your `PATH` is the train, not a stale copy —
**not** as an editable install:

```bash
python3 -m pip install '.[dev]'      # NOT -e / --editable, see below
which aisquare && aisquare --version
```

> ⚠️ **[verified-train, planner `dfd9a883`] Do not use `-e` for a cutover.** §5
> has you install `aisquare-cli[explainability]`, and over an editable checkout
> that install **bricks the CLI** — the SDK ships a real `aisquare/` directory
> which shadows the editable path hook, and `aisquare.cli` disappears
> (`ModuleNotFoundError`, verified by `coder3`). An earlier revision of this
> runbook opened with `pip install -e` and carried that warning 450 lines later,
> phrased as a developer hazard — which it is, right up until §0 makes it yours.
> Over a normal install the extra is safe. Develop from an editable checkout if
> you like; do not run **this document** from one.

**[verified-train] Do not skip this.** On this box overnight the installed
binary and the train both reported `aisquare 0.4.0rc1` while being *different
programs*: `site-packages/aisquare/cli/launch.py` had no `resolve_binary`, so a
role bound to a wrapper silently launched the default agent and exited 0.
**Version does not distinguish them.** Confirm the fix is present:

```bash
grep -c resolve_binary "$(python3 -c 'import aisquare,os;print(os.path.dirname(aisquare.__file__))')/cli/launch.py"
```

Expect `1` or more. `0` means you are running a stale install — reinstall.

---

## 0b. Warm the store before you launch the crew (10 seconds)

**[verified-train]** If `~/.aisquare` does not exist yet — a new machine, a new
operator account — run **one** `aisquare` command by itself before starting
several sessions at once:

```bash
aisquare status >/dev/null    # creates and migrates ~/.aisquare/context.db
```

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

**[verified-train] Not every damaged store looks like that, and one of them
looks like nothing at all.** Five damage shapes were measured. Four are **loud**
— non-database bytes, a part-way truncation, a corrupted page with an intact
header — and all of them now give you the one-line message above. *(They used to
give 39-75 lines of traceback; if you see that, you are on an older build.)*

**The fifth is silent, and it is the one to know about.** A file **truncated to
zero bytes** is read by SQLite as a brand-new empty database, so the store is
re-created and migrated, `status` exits 0, and `doctor` afterwards reports
`✓ database: context.db is readable (0 user entries)` — while every session,
task and note that file held is gone. The CLI prints one line at the first open
that sees it:

```
board: ~/.aisquare/context.db exists but is empty — it was truncated, and the
tasks, notes and sessions it held are gone.
```

That line appears **once**, on the open that saw it; everything afterwards looks
healthy because by then it is. **So if the board is suddenly empty and `doctor`
is green, the file was truncated, not corrupted** — and the recovery below does
not apply, because there is nothing left to recover.

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
7 `check/tool`, all 403. Probing the endpoint directly names the cause:

```
{"detail":"Workspace does not own this studio"}
```

`EXPLAINABILITY_STUDIO_ID` is pinned to `21`, which is a **publication id**, not
a studio the workspace owns. And a pinned value short-circuits the gateway's own
lookup — `policy.py:_ensure_studio` opens with `if self.studio_id: return
self.studio_id`, so the correct binding is never consulted.

Unpinning alone does **not** save you. The binding genuinely does not exist:

```
GET /v1/routing/resolve?agent_name=aisquare-runner
  -> HTTP 404 {"detail":"No studio bound to this agent yet"}
```

**[verified-stg]** — same 404 for `aisquare-planner`, `aisquare-coder`,
`aisquare-cli-test`, `aisquare-subagent-probe2`.

A definitive 404 is cached as "no rule book" and every check passes through. So:
pinned → 403 spam and ungoverned; unpinned → cleanly ungoverned. **Attaching a
rule book in the studio UI will do nothing until the agent names resolve to a
studio.** The 403s are the worse case only because they look like a permissions
wall and hide the missing binding.

### The order that actually works

**1a. Register the roster.** **[unverified-prod]** — the auth shape is
**[verified-stg]**: header `X-API-KEY` with the raw workspace key, and **no
`Authorization` header** (a fronting layer tries to verify it as a JWT and fails
the whole call — confirmed: the same request with `Authorization: Bearer` returns
`401 {"detail":"Token verification failed"}`).

```bash
set -a; source /path/to/explainability-prod.env; set +a   # see §2
curl -sS -X POST "$EXPLAINABILITY_GATEWAY_URL/v1/agents/register-roster" \
  -H "X-API-KEY: $EXPLAINABILITY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agents": ["aisquare-planner", "aisquare-coder", "aisquare-runner"]}'
```

The response carries a **`publication_id` per agent**. That is the id of the
agent's publication record in the workspace — it is **not** a studio id, and
putting it in `EXPLAINABILITY_STUDIO_ID` is precisely the mistake that produced
the staging 403s. Record the values; do not wire them anywhere yet.

> Use the real role names. `agent_name_template` defaults to `aisquare-{role}`
> (**[verified-train]**), so the names the proxy will present are
> `aisquare-planner`, `aisquare-coder`, `aisquare-runner`. A name that is not
> registered is rejected at ingest with `409 no_agent_identity`.

**1b. Bind those agents to the studio, in the studio UI.** This is the step that
makes routing resolve. Open the studio in the dashboard (human JWT required —
the workspace key cannot do this, it is ingest-write only) and attach/bind the
registered agents to it.

**1c. Verify the binding took — this is the gate for the whole runbook:**

```bash
curl -sS "$EXPLAINABILITY_GATEWAY_URL/v1/routing/resolve?agent_name=aisquare-runner" \
  -H "X-API-KEY: $EXPLAINABILITY_API_KEY"
```

- ✅ `{"studio_id": "<something>"}` → bound. Continue.
- ❌ `404 {"detail":"No studio bound to this agent yet"}` → **stop.** Tracing
  will work and governance will not. Fix the binding before going further.

Repeat for each of the three names.

**1d. Attach the rule book** to that studio in the UI, then re-run 1c and make
one traced call (§5). Confirm you no longer see `policy check degraded
(FAIL_OPEN)` in the proxy log. **Absence of that warning is the only proof the
rule book is live.**

**1e. Do not pin `EXPLAINABILITY_STUDIO_ID`** in the prod env file. Leave it
unset so one-key mode resolves the studio by agent name. Pin it only if you have
a real studio id from 1c, and never a `publication_id`.

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
# EXPLAINABILITY_STUDIO_ID intentionally NOT set — see §1e.
```

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
install? It is. `1.1.0` is on PyPI and carries the junk-run suppression —
`_has_valid_correlation` in `claude_proxy.py` is **byte-identical** to the
checkout's. `1.0.6` and `1.0.7` do **not** have it, and on those the junk-run
behaviour returns silently as extra Runs in the dataset.

```bash
set -a; source /home/work/.config/aisquare/explainability-prod.env; set +a
export AISQUARE_PROXY_PORT=9190
python -m pip install 'aisquare>=1.1.0'
python -m aisquare.explainability.claude_proxy
```

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
sudo -n true 2>/dev/null && EXE=$(readlink -f /proc/$PID/exe) || EXE=python
$EXE -c "import importlib.util as u; src=open(u.find_spec('aisquare.explainability.claude_proxy').origin).read(); print('junk-run suppression:', 'IN FORCE' if '_has_valid_correlation' in src else 'MISSING')"
# IN FORCE   -> good
# MISSING    -> you are on <1.1.0; extra Runs will appear in the dataset
```

Verified to discriminate: the live proxy reports `IN FORCE`; the same check run
against a fresh `aisquare==1.0.6` reports `MISSING`.

**[verified-stg]** Health check — run it yourself, do not assume:

```bash
curl -s http://127.0.0.1:9190/health
{"status":"ok","service":"aisquare-proxy","mode":"claude_code","governance":"gateway"}
```

Both fields matter. The CLI refuses any `/health` whose `service` is not
`aisquare-proxy` or whose `mode` is not `claude_code`, and it **fails open** —
so a wrong-mode proxy produces *untraced launches with no error*, not a failure.
Silence is the failure mode. Check `/health` yourself.

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
refused by the gateway with **409 `no_agent_identity`**, so traces leave the
machine and land nowhere.

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
spool:    0 queued, 0 sent, 0 dead-letter
redaction: standard — credentials are removed from insights leaving this machine (paths and hostnames are kept); local capture keeps what you typed
exit=0
```

The two lines that depend on YOUR environment are `gateway` and `key`; the
sandbox run that produced this had neither set and showed `(unset)` and `is NOT
set`. Everything else is what a correctly wired machine prints.

`status` exits non-zero **only** when tracing is enabled *and* the probe fails —
the precise state in which launches would silently fall back to untraced. That
is what makes it the right single check.

> **[verified-train]** `status` honours `--json` now (it used to print human
> text under the flag). `aisquare --json explainability status` returns a real
> payload — `enabled`, `target`, `gateway`/`gateway_source`, `key_env`/`key_set`
> (never the key itself), `proxy`, `identity`, `agents`, `probe`, `shipping`,
> `redaction` — so the cutover can be scripted rather than eyeballed. The spool
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
exec /usr/local/bin/aisquare explainability ship --strict
```

`set -a` matters: without it the file's values are shell variables, not
environment variables, and the CLI never sees them. Use the **absolute** path
to `aisquare` — cron's `PATH` will not find it. Then:

```bash
*/5 * * * * $HOME/.aisquare/ship-insights.sh
```

No `>/dev/null`: a non-zero exit is the entire signal, and cron mails you the
reason.

Check it before trusting it, with the key in scope exactly as the wrapper puts
it there. A `0` means it really shipped; anything else prints why:

```bash
aisquare explainability ship --strict
env -i sh -c "$HOME/.aisquare/ship-insights.sh"; echo "exit=$?"
```

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

This is the one check that can catch a split brain, and counts can never do it:
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

## 6. What healthy looks like, and what is just noise

**A healthy run, in the proxy log** (**[verified-stg]**):

```
INFO [__main__] pipeline-session: opened pipeline_id=<your id> trace_id=<32 hex>…
INFO [httpx] HTTP Request: POST <gateway>/v1/traces/ingest "HTTP/1.1 202 Accepted"
INFO:     127.0.0.1:xxxxx - "POST /v1/messages?beta=true HTTP/1.1" 200 OK
```

One `pipeline-session: opened` per session, ingest `202`, messages `200`.
Overnight on staging: **70/70 ingest calls returned 202, zero non-202.**

Backlog check:

```bash
./.venv/bin/explainability-doctor
```

Healthy: `delivery_backlog [OK] dispatched=N`, `gateway_live [OK]`,
`gateway_ready [OK]`.

**Known noise — do NOT treat as red** (**[verified-stg]**, all three seen on a
fully healthy run):

| Line | Verdict |
|---|---|
| `agno [MISSING] Install optional dependency: .[agno]` | expected — optional integration, unused |
| `openinference_agno [MISSING]` | expected — same |
| `openai_api_key [WARNING] Set OPENAI_API_KEY (required for RML extraction)` | expected — gateway-side, unrelated to tracing |
| `HEAD /api/hello … 405 Method Not Allowed` | expected — a client probe the proxy does not implement |
| `pydantic_settings IncompleteFieldDefinitionWarning` in the test suite | pre-existing, unrelated |

**Genuinely red:**

| Line | Meaning |
|---|---|
| `policy check degraded (FAIL_OPEN)` | governance is off — go back to §1 |
| ingest returning anything other than `202` | traces are not landing |
| `409 no_agent_identity` | the agent name is not registered — §1a |
| `probe: proxy unreachable` with `enabled: True` | launches are silently untraced — §3 |
| `API Error: Invalid URL` with `exit=1` | you are on a build older than the POSIX-quoting fix, **or** an `ANTHROPIC_BASE_URL` in your own environment is malformed — the CLI now names it on stderr just above the failure |
| `✗ context store error: duplicate column name: account` | you skipped §0b on a brand-new `~/.aisquare` — recovery below |
| `✗ the context store is corrupt: …/context.db` | the file is damaged, not misconfigured — the message carries the whole recovery, and `aisquare doctor` prints the same one |

**[verified-train, planner `dfd9a883`]** **Recovering a wedged store.** If several
sessions raced a *first* open, the store can be left permanently mid-migration:
the DDL applied but its version bump did not, so every later attempt at that
migration fails again — this does not heal on retry and it takes every
`aisquare` command with it (`exit 1`). Characterisation is in
`docs/store-migration-race.md`; §0b prevents it. To recover:

```bash
rm ~/.aisquare/context.db          # or $AISQUARE_HOME/context.db
aisquare status > /dev/null        # ONE process, alone — this re-migrates
```

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
prod` with no arguments to retype. After disabling, `status` reads `enabled:
False` while still showing the target and gateway, and
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
| 0 Preflight | `grep -c resolve_binary …/cli/launch.py` ≥ 1 | reinstall previous version |
| 0b Warm store | `PRAGMA user_version` on `~/.aisquare/context.db` is non-zero | none — the migration is forward-only |
| 1a Roster | response lists each agent + `publication_id` | re-register; registration is idempotent by name |
| 1b/1c Binding | `/v1/routing/resolve` returns a `studio_id` | unbind in the studio UI |
| 1d Rule book | no `FAIL_OPEN` warning on a traced call | detach the rule book in the UI |
| 2 Secrets | `stat -c %a <env file>` → `600` | `rm` the file |
| 3 Proxy | `/health` → `service=aisquare-proxy`, `mode=claude_code` | `Ctrl-C` (never port 9090) |
| 3 Proxy build | the §3 check prints `IN FORCE` | reinstall `aisquare>=1.1.0` |
| 4 Enable | `status` shows your target, gateway and proxy | `aisquare explainability disable`, then §7 |
| 4b Register | each agent printed with a `publication_id` | none needed — additive and idempotent |
| 5 Green | `doctor --live` → `ingest: test span accepted … (HTTP 202)` | `aisquare explainability disable`, then §7 |
| 5b Insights | `.shipping.gateway` equals your prod URL; `spool:` counts move after `ship` **and keep moving** — a growing `N buffered` means draining stopped | `aisquare init --no-explainability` (spool kept) |

---

## Open items handed to the morning

1. **[blocker]** No agent name resolves to a studio on staging (§1). Prod will
   behave identically unless 1a–1d are done in that order. Until then runs are
   ungoverned — traced, but enforcing nothing.
2. `EXPLAINABILITY_STUDIO_ID=21` should still be removed or corrected — but it
   is **not** the cause of the 403s, and correcting it alone will not fix
   governance. Measured: `GET /v1/studios` with the workspace key SUCCEEDS and
   lists 16 studios (144–169); `21` is not among them and `169` is. Yet **every**
   studio-scoped call 403s for **all sixteen**, `169` included, and unsetting the
   pin changes nothing. The workspace key simply cannot make studio-scoped calls.
   Governance needs a credential class we do not hold, not a config edit.
3. **[CLOSED]** `explainability status` honours `--json`. It used to print
   human text under the flag; it now returns a real payload — `enabled`,
   `target`, `gateway`/`gateway_source`, `key_env`/`key_set` (never the key
   itself), `proxy`, `identity`, `agents`, `probe`, `shipping`,
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
7. **[CLOSED]** The proxy build is pinned. `aisquare>=1.1.0` is released and
   carries the junk-run suppression — `_has_valid_correlation` is byte-identical
   to the `bb88bb5` checkout the overnight receipts used, so that evidence is
   reproducible from PyPI and nobody needs the unreleased branch. `1.0.6`/`1.0.7`
   do **not** have it. §3 carries a check that reads the *running* proxy and was
   verified to discriminate (live proxy `IN FORCE`, fresh `1.0.6` `MISSING`).
   Two caveats: neither SDK PR #362 nor #363 is on `origin/main` (which is at
   #433), so the fix reached the release by some other route — treat the
   RELEASE, not the PR, as the thing to depend on. And **#363 is gateway-side**
   (`gateway/rml/assumption_mining.py`), not shipped by pip at all, so no
   client install can carry it; it is a gateway deploy gate.
