# Runbook — explainability cutover, staging → production

**Audience:** one operator, at a keyboard, aiming to have the team generating
traced data against explainability **prod** in about 15 minutes.

**Written:** 2026-08-17, night shift, by the runner session (`d124bc26`), against
train `rc/v2026.08.18` @ `0b5cfd1`.

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

Reinstall the CLI so the binary on your `PATH` is the train, not a stale copy:

```bash
python3 -m pip install -e '.[dev]'
which aisquare && aisquare --version
```

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
AISQUARE_AGENT_NAME=aisquare-runner
# EXPLAINABILITY_STUDIO_ID intentionally NOT set — see §1e.
```

Load it **per shell**, never globally:

```bash
set -a; source /home/work/.config/aisquare/explainability-prod.env; set +a
```

Rules that are not negotiable: the path is never baked into source, the contents
never go into a repo, a board note, or a ticket. Use an `ingest:write` key —
writes traces, cannot read or rebind. Rotation is new key → deploy → revoke old.

---

## 3. Start the proxy (2 min)

```bash
set -a; source /home/work/.config/aisquare/explainability-prod.env; set +a
export AISQUARE_PROXY_PORT=9190
cd /home/work/work/AISquare-Explainability-SDK
./.venv/bin/python -m aisquare.explainability.claude_proxy
```

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

## 4. Turn tracing on for the team (2 min)

**[verified-train]** Default is off, and `proxy_url` defaults to
`http://127.0.0.1:9090` — set it explicitly or `status` reads red for the wrong
reason.

```bash
aisquare config set explainability.proxy_url http://127.0.0.1:9190
aisquare config set explainability.enabled true
```

---

## 5. The one command that proves it green (1 min)

**[verified-train]**

```bash
aisquare explainability status; echo "exit=$?"
```

Green looks exactly like this:

```
enabled:  True
proxy:    http://127.0.0.1:9190
identity: aisquare-{role}
probe:    healthy
exit=0
```

`status` exits non-zero **only** when tracing is enabled *and* the probe fails —
the precise state in which launches would silently fall back to untraced. That
is what makes it the right single check.

> **[verified-train]** `status` ignores the global `--json` flag (it prints the
> same human text), so do not script against it expecting JSON. `aisquare --json
> explainability env <role>` *does* emit JSON. Filed against the #51 lane.

Then make one real traced call and watch the proxy log:

```bash
eval "$(aisquare explainability env runner --session-id "$SESSION_ID")"
claude -p "reply with the word OK and nothing else"
```

**Pass `--session-id`.** **[verified-train]** Without it the pipeline id is a
fresh random UUID on every invocation — two consecutive calls produced
`6fa4fd37-…` then `66e7ee90-…`, i.e. two separate Runs. With
`--session-id d124bc26` the header is exactly `X-Pipeline-Id: d124bc26`. One
session = one Run only if every seam passes it.

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
aisquare config set explainability.enabled false
```

**[verified-train]** After this, `aisquare explainability env <role>` exits `1`
with `✗ explainability is disabled (config default)` and emits no exports, so
every session launches untraced. Re-enabling with `true` restores `probe:
healthy`. Reversible in both directions, no other behaviour change.

To also stop the proxy: `Ctrl-C` the process from §3. **Not** the one on 9090.

---

## Per-step verification and rollback, at a glance

| Step | Verify | Rollback |
|---|---|---|
| 0 Preflight | `grep -c resolve_binary …/cli/launch.py` ≥ 1 | reinstall previous version |
| 1a Roster | response lists each agent + `publication_id` | re-register; registration is idempotent by name |
| 1b/1c Binding | `/v1/routing/resolve` returns a `studio_id` | unbind in the studio UI |
| 1d Rule book | no `FAIL_OPEN` warning on a traced call | detach the rule book in the UI |
| 2 Secrets | `stat -c %a <env file>` → `600` | `rm` the file |
| 3 Proxy | `/health` → `service=aisquare-proxy`, `mode=claude_code` | `Ctrl-C` (never port 9090) |
| 4 Config | `aisquare explainability status` shows your URL | `aisquare config set explainability.enabled false` |
| 5 Green | `status` → `probe: healthy`, `exit=0` | as above |

---

## Open items handed to the morning

1. **[blocker]** No agent name resolves to a studio on staging (§1). Prod will
   behave identically unless 1a–1d are done in that order. Until then runs are
   ungoverned — traced, but enforcing nothing.
2. `EXPLAINABILITY_STUDIO_ID=21` in the staging env file is a publication id and
   should be removed or corrected; it is the direct cause of the 403s.
3. `explainability status` does not honour `--json` — filed against #51.
4. `POST /v1/agents/register-roster` was **not** executed by me against staging.
   It mutates shared state and I left that call to a human.
5. Prod gateway URL and key are **[unverified-prod]** throughout. Every
   *mechanism* here is verified against staging; the prod *values* are not.
