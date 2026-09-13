# Connecting your agents to Explainability

Every Claude Code session becomes a **Run** on the Explainability dashboard —
prompts, responses, tool calls, tokens, cost — and your own prompts, board notes
and task events ship alongside them.

Ten minutes. Nothing to run in the background.

Requires `aisquare-cli >= 0.5.0`.

---

## Two lanes, configured together

They are separate paths and either can be on without the other. Knowing which is
which turns most confusion into a one-line answer.

| Lane | Carries | How it travels |
|---|---|---|
| **Proxy** | Model traffic — prompts, responses, tools, tokens, cost | your agent → proxy → gateway |
| **Client** | Your prompts, board notes, task claims, session events | CLI → local spool → `explainability ship` → gateway |

Both key on the same **pipeline id**, so a board row, a live process and a
dashboard Run share one identifier. Usually that id *is* the session id:
`aisquare launch` mints one and pins it with `--session-id`. When it cannot —
`--continue`, a bare `--resume`, or an agent binary that does not take the flag
— it traces under a fresh id rather than guess, and the board row and the Run no
longer share a key.

The dashboard's Run id is derived from that pipeline id (SHA-256) **when the
launcher owns the Run**, which is the default: it posts the Run's root span
first and hands the proxy a `traceparent` naming it. Then `aisquare launch`
prints the id — `traced as … (pipeline <session>, run <trace_id>)` — and
`~/.aisquare/explainability/joins.jsonl` records it as `trace_id`, so a Run can
be found from its board row without reading anything back. If that root could
not be posted (no gateway, no key, refused) the proxy keys the Run itself: the
launch line says so, `trace_id` is recorded as `null` rather than a derived id
nobody used, and the Run is found by its `agent.run_id` — the pipeline id —
instead.

**Why a proxy at all?** Claude Code emits no telemetry of its own — the only
interception point is `ANTHROPIC_BASE_URL`. So something has to sit in the
request path, record the exchange, and forward it upstream. That something is
hosted for you; see [Self-hosting](#self-hosting-or-keeping-traffic-local) if you
would rather it were not.

---

## 1. Install

```bash
python3 -m venv ~/.aisquare-venv
~/.aisquare-venv/bin/pip install "aisquare-cli[explainability]"
```

Put it on your `PATH` (`~/.aisquare-venv/bin`), then check:

```bash
aisquare --version
aisquare explainability
```

`--version` should be `0.5.0` or later, and `explainability` with no arguments
should list six subcommands. **`--version` alone cannot tell you whether this build has the
integration** — earlier releases report a similar version and have only two of
those subcommands, so check both.

> If you already have `aisquare` installed elsewhere, make sure the one on your
> `PATH` is this one. `which aisquare` settles it.

---

## 2. Get an ingest key

From the dashboard: **your avatar (top right) → Studio → Routing → Ingest key →
create → copy**.

The key is workspace-scoped. It is a write credential — it can send spans and
read nothing.

---

## 3. Store the key

Never in `config.toml`; that file gets pasted into issues and copied between
machines. Two places are read, environment first:

```bash
export EXPLAINABILITY_API_KEY=...
```

...or a file at mode 600, which is what most machines end up using:

```bash
umask 077 && printf '%s' '...' > ~/.aisquare/explainability-key
```

`aisquare explainability status --json` reports which one won, as `key_source`
(`env`, `file` or `unset`) and `key_origin`. **Branch scripts on `key_source`,
not on `key_env`** — the latter is the variable your target *names*, set or not.

### Where the key ends up

A hosted proxy authenticates on this key, so it is sent as a request header —
which means it is exported into the launched agent's environment
(`ANTHROPIC_CUSTOM_HEADERS`). Worth knowing, and a deliberate trade rather than
an accident:

- **The agent and anything it starts can read it.** Subprocesses, MCP servers,
  tools. It is a write-scoped ingest credential — it can send spans and read
  nothing — which is what makes that acceptable, but it is not nothing.
- **`aisquare explainability env <role>` prints it**, by design: the output
  exists to be `eval`'d. Treat that output as secret — it is not safe for
  scrollback, a screen-share, or CI logs.
- It is **not** forwarded upstream: the proxy strips the header before the
  request reaches the model provider.
- aisquare's own internal subprocesses do not receive it — the tracing variables
  are stripped from those environments.
- Over a non-loopback hop it is sent only over `https`; a plaintext URL with a
  key resolved is refused rather than traced.

A local proxy needs no key at all, so if none of that is acceptable, that is the
topology to use.

---

## 4. Point at your deployment

```bash
aisquare init --yes
aisquare explainability enable --target prod --gateway-url https://gateway.example --proxy-url https://gateway.example:9443
```

The gateway and the proxy are **different endpoints** — commonly the same host
on different ports. Both come from your dashboard's integration page.

Repeat with a different `--target` to add a second deployment; the active one is
whichever `target` names, and keys never cross between them.

---

## 5. Register your agent identities

```bash
aisquare explainability register
```

**Spans whose agent name the workspace does not know are rejected**, so a fresh
workspace records nothing until this runs. Idempotent. It registers the identity
template applied to each configured role — by default `aisquare-planner`,
`aisquare-coder`, `aisquare-runner` and the rest of the first-class roles
(`-manager`, `-tester`, `-reviewer`, `-validator`, `-ui-tester`). A machine whose
`config.toml` predates a role is told which ones its roster lacks when it runs
`register`.

---

## 6. Connect your agent

```bash
aisquare agents connect claude-code
```

Installs the lifecycle hooks. Without them the client lane captures nothing.

---

## 7. Turn on insight shipping

```bash
aisquare init --explainability --yes
```

This is the client lane, and it declines unless a gateway and a usable key
already exist — so do steps 3 and 4 first.

What it captures: **your prompts, board notes, task claims and session events —
no file contents, no model traffic**, with credentials scrubbed at
`config.redaction.level` before anything is written to the spool. `standard`
removes vendor token shapes, JWTs, private-key blocks, `Authorization` values and
`user:pass@host`; `strict` also removes email addresses and rewrites your home
directory to `~`. Local capture keeps what you typed — redaction applies to what
leaves the machine.

---

## 8. Verify

```bash
aisquare doctor --live
```

Six explainability rows. These three are the ones that matter:

- `explainability proxy` — the proxy answers and is the right kind of service
- `explainability gateway` — the gateway is reachable
- `explainability ingest` — **a real span was accepted**

**Stop and fix it if `ingest` is not green.** Nothing lands until it is.

`explainability governance: … UNGOVERNED` is expected unless a rule book is
attached to your studio.

---

## 9. Run a traced session

```bash
aisquare launch coder
```

Use this instead of `claude`. It mints the session id, starts the agent on it, and
traces under that same id, so the board row and the dashboard Run share a key.

It stands down loudly rather than guessing: `--continue` or a bare `--resume`
name a session that does not exist yet, so nothing is pinned and the session
traces unjoined. An id you passed yourself is read, never doubled.

Tracing never blocks a launch. An unreachable proxy, a bad config, a dead
gateway — the session starts untraced and says on stderr what was lost.

---

## 10. Drain the client lane on a timer

Nothing drains the spool by itself.

```bash
aisquare explainability ship --strict
```

Put that on a timer every few minutes. **`--strict` is not optional in a timer**
— without it a run that could not ship at all still exits 0, and the queue grows
behind a green cron job forever.

---

## Self-hosting, or keeping traffic local

The proxy does not have to be the hosted one. Point at any `claude_code`-mode
proxy, including one on your own machine:

```bash
aisquare explainability enable --proxy-url http://127.0.0.1:9090
```

Reasons to: model traffic that must not leave the machine, or a self-hosted
deployment with no proxy tier.

The CLI does not manage that process — start it yourself from an environment that
has the SDK and its server dependencies, and stop it where you started it:

```
EXPLAINABILITY_GATEWAY_URL=https://gateway.example \
EXPLAINABILITY_API_KEY=... \
AISQUARE_PROXY_PORT=9090 \
AISQUARE_PROXY_MODE=claude_code \
  aisquare-proxy
```

`aisquare-proxy` needs `fastapi` and `uvicorn`, which the `explainability` extra
does **not** install — it carries the tracing client, not a server. Install them
in that environment yourself.

Two things to know if you go this way. The proxy prints `Application startup
complete` *before* it binds, so its own log is not proof it got the port. And
`doctor`'s proxy row goes green for *any* service answering as a `claude_code`
proxy — it cannot tell yours from one left running against another deployment,
whose Runs land somewhere else.

---

## A key per project

One machine, several workspaces: the key under `~/.aisquare/explainability-key`
(or the target's variable) is the **machine** key, and every project shipped and
proxied under it. A project can carry its own (#141):

```bash
cd ~/work/api
aisquare explainability key set --from-env API_WORKSPACE_KEY   # from a variable (or pipe it on stdin)
aisquare explainability key show                                # the origin, never the value
aisquare explainability key clear
```

The key comes from a named variable (`--from-env`) or from stdin (pipe it in);
it is never an argument, because argv is in every process list and shell
history.

The key lands in the project's data directory, mode 600
(`~/.aisquare/projects/<id>/explainability-key`); the store records only the
deployment it is for and the path. Resolution, inside the one resolver every
lane uses, is **project key → the target's variable → the machine file**, and a
key attached for `stg` is never handed to a `prod` gateway — the same rule the
machine file follows. `aisquare launch`, `fleet spawn` (through `launch` in the
window) and `explainability env [--project P]` authenticate the proxy with the
project's key; `status` and the UI's Explainability view show the origin for the
active project, and the view has an *Attach key* field (the key is pasted,
never echoed). The client lane — the insights this CLI buffers and `ship`
drains — still ships under the machine key; per-project shipping is a follow-up.
`init --explainability` keeps writing the machine key, so a single-workspace
machine is unaffected.

## If something breaks

| Symptom | Cause |
|---|---|
| `doctor` shows no `explainability` rows | Pre-0.5.0 build. `--version` cannot distinguish them; `aisquare explainability` should list six subcommands |
| `ModuleNotFoundError: No module named 'aisquare.cli'` | An **editable** install plus the SDK: they share the `aisquare` import name and the SDK wins. `pip uninstall aisquare`, then reinstall non-editable |
| `ImportError: cannot import name '__version__'` | A pre-0.5.0 build with the SDK installed alongside. Upgrade |
| `CERTIFICATE_VERIFY_FAILED` / hostname mismatch | Wrong gateway hostname |
| `/ready` returns HTML instead of JSON | You are pointed at the dashboard, not the gateway |
| `401 Invalid API key` | Key belongs to a different deployment, or was rotated |
| `409` / `not a registered identity` | Step 5 |
| `explainability proxy: unreachable` | Wrong `--proxy-url`, or a local proxy that is not running |
| Everything green, nothing on the dashboard | The spool is not being drained — step 10 |
| `… is temporarily unavailable (server error), so auto mode cannot determine the safety of Bash` on every tool call, while the chat itself keeps answering | Auto mode's **classifier** request failing behind the proxy — see the next section |

`aisquare explainability status --json` is the machine-readable view, and the one
to script a check against.

### Auto mode refuses every tool call behind the proxy

**The signature.** A session in `auto` permission mode, traced through the
proxy, answers every Bash (and other classified tool call) with

```text
claude-opus-5[1m] is temporarily unavailable (server error), so auto mode cannot
determine the safety of Bash right now. Wait a moment and then try this action again.
```

while its own replies keep arriving and `status.claude.com` is green. Reads
still work (they are not classified). The model named is not your chat model:
it is the **classifier** — auto mode sends a separate, *non-streaming* request
carrying a portion of the transcript plus the pending action to a classifier
model (Sonnet 5 by default, an Opus 1M fallback), and reports a 5xx from it
this way. Chat survives because it streams.

**Why it fails behind the proxy.** The proxy forwards non-streaming calls under
fixed total timeouts and surfaces a timeout as an anonymous 500
([AISquare-Explainability-SDK#1144](https://github.com/AISquare-Studio/AISquare-Explainability-SDK/issues/1144)
— the fix belongs there). A classifier call is at least as large as the
session's first request — its **baseline**: system prompt, tool schemas
including every MCP connector's, skills, memory — and on a machine with a rich
Claude config that is ~140k tokens before anyone has typed. Measured: sessions
starting at ~137k were refused from their first shell command, manager and
coders alike; probes at ~98k passed. `/compact` does not help a fresh agent —
the baseline cannot be compacted.

**What the CLI tells you.** `aisquare doctor` gains an `explainability
auto-mode` line whenever a fleet role runs `auto` behind a configured proxy: it
reads the first-turn size of your recent sessions from their transcripts and
warns when that size is above ~100k tokens, or when a recent session was
refused. `fleet spawn` puts the same warning on its receipt. A running session
that is being refused is put in **attention** (🔔 on its row) by its Stop hook,
with one `auto_mode_blocked` line on the board.

**Until the proxy fix, pick one:**

- a permission mode that needs no classifier for the fleet roles:
  `aisquare config set fleet.roles.coder.permission_mode acceptEdits` (per
  spawn: `aisquare fleet spawn coder --permission-mode acceptEdits`; a running
  agent: set the mode, then `aisquare fleet restart <label>` — its session
  resumes);
- a lighter Claude config dir for the account the fleet launches under — fewer
  MCP connectors; their tool schemas are the bulk of the baseline — and check
  the new size with `aisquare doctor`;
- run agents untraced: `aisquare explainability disable` (you lose the proxy
  lane; the client lane still ships).

---

## Related

- [`docs/explainability-tracing-boundary.md`](explainability-tracing-boundary.md)
  — what a Run does and does not cover. **Read this before comparing numbers
  per sub-agent**: identity travels in process-level environment, so an
  in-process Task subagent inherits its parent's identity and cannot carry its
  own. Per-role and per-session figures are real; per-subagent ones do not exist.
