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

Both key on the same session id, so a board row, a live process and a dashboard
Run share one identifier.

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
`aisquare-coder`, `aisquare-runner`.

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

`aisquare explainability status --json` is the machine-readable view, and the one
to script a check against.

---

## Related

- [`docs/explainability-tracing-boundary.md`](explainability-tracing-boundary.md)
  — what a Run does and does not cover. **Read this before comparing numbers
  per sub-agent**: identity travels in process-level environment, so an
  in-process Task subagent inherits its parent's identity and cannot carry its
  own. Per-role and per-session figures are real; per-subagent ones do not exist.
