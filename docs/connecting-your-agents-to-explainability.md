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

### The proxy sits beside the gateway

`--proxy-url` is the deployment's **proxy**, not its gateway: same host, port
**9443**. It is the one value in this runbook nobody can guess, and getting it
wrong is silent — sessions launch, traffic goes somewhere else, and `doctor`
used to call that green.

Two ways not to get it wrong:

- **In `asq`** — the Explainability tab's **Setup** form fills it in from the
  gateway you type. Leave the proxy field blank. Three things about that form
  worth knowing before the first press:
  - The **deployment** field names the entry these settings belong to. It does
    not move this machine to that deployment unless **make active** is ticked —
    correcting prod's gateway from a machine on stg leaves the machine on stg,
    and the toast says so.
  - The **workspace key** goes to `~/.aisquare/explainability-key`, and that
    file is read only for the default variable, `EXPLAINABILITY_API_KEY`. A
    target that names its own **key variable** reads the shell, so the form
    refuses a key typed beside a custom variable rather than writing it where
    nothing will look. With **this project only** ticked, the key is the
    project's own instead (see *A key per project* below), which every target
    reads first, whatever variable it names.
  - The **prefix** is a name: `nishil` becomes `nishil-{role}`. A prefix with a
    brace in it (`nishil-{role}`, `nishil}`) is refused with the reason, and
    nothing is stored; the role is added for you.
- **On the command line** — pass it explicitly, as the examples above do.

Either way, a gateway or proxy without a scheme (`stg.example`) is **refused**,
not stored. It used to be accepted, after which the proxy lane read green over a
gateway nothing could reach; the one writer both surfaces go through checks it
now, so the CLI and the form give the same answer.

Self-hosting with no proxy tier? Use your own, or the local sidecar at
`http://127.0.0.1:9090`. The form suggests nothing for a loopback gateway,
precisely so it cannot repoint you at a port with nothing on it — and nothing
when a proxy is already configured for the target, its own or a deliberate
top-level `proxy_url`.

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
# (the local sidecar's own port — 9443 is the HOSTED convention and does not apply here)
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
aisquare explainability register                                # once, if that workspace has not seen this machine's agents
aisquare explainability key show                                # the origin, never the value
aisquare explainability key clear
```

The key comes from a named variable (`--from-env`) or from stdin (pipe it in);
it is never an argument, because argv is in every process list and shell
history. A directory nothing has registered yet is registered by `key set`:
attaching a key is a deliberate act, like `team on`.

**Which project** is the one a launch from here joins: `$AISQUARE_TEAM_HUB`
when it is set, else this checkout (a worktree resolves to its principal) —
never the `project switch` pin, which launches ignore. Every surface without a
`--project` asks the same question, so the key `status` and `key show` report
is the key `launch`, `team spawn` and `explainability env` authenticate with.
Name another project with `--project P`.

The key lands in the project's data directory, mode 600
(`~/.aisquare/projects/<id>/explainability-key`); the store records only the
deployment it is for and the path. Resolution, inside the one resolver every
lane uses, is **project key → the target's variable → the machine file**, and a
key attached for `stg` is never handed to a `prod` gateway — the same rule the
machine file follows. `aisquare launch`, `fleet spawn` (through `launch` in the
window) and `explainability env [--project P]` authenticate the proxy with the
project's key. A key for ANOTHER workspace traces nothing until that workspace
knows this machine's agent identities — every span is refused 409
`agent_not_registered` — so run `explainability register` there once;
`register [--project P]` registers under the same project's key. `status`
shows the origin for that project, and each project page's Explainability tab
shows the key its launches use — the project they join from the page's root,
so the hub's under `$AISQUARE_TEAM_HUB` — and a *Register roster* button that
registers under it. The **Setup** form's workspace key field attaches a key to
that project when **this project only** is ticked (the key is pasted, never
echoed), bound to the deployment the form names, or — when the field is blank —
the one the project's destination names (#142), else the one an exported
`$AISQUARE_EXPLAINABILITY_TARGET` names, else the active one, as `key set` and
the project's launches resolve it; typed
beside other settings that would go to a different deployment, it is refused
until the field names one. The client lane — the insights this CLI buffers and `ship` drains — still
ships under the machine key, and `doctor --live` checks the machine's
workspace (`doctor --live --project P` checks P's key instead); per-project
shipping is a follow-up.
`init --explainability` keeps writing the machine key, so a single-workspace
machine is unaffected.

## Choose where traces land with your sign-in

The other half of `aisquare login`: the session knows **who** you are, so it can
also list **where** a project's traces may land and record the choice — no key
pasted, no gateway URL typed (#142).

```bash
aisquare login --api-url https://stg-api.aisquare.studio   # or the default, production
aisquare explainability workspaces                          # what you can see, members first
aisquare explainability studios --workspace acme
aisquare explainability use acme/Frontend                   # for the project a launch here joins (or --project P)
aisquare explainability status                              # "destination: acme / Frontend · stg · …"
aisquare explainability use --clear
```

`use` records the destination **per project** and does four things, each
reported on its own line:

- **target** — the deployment the session belongs to becomes the explainability
  target for **this project only** (`stg-api.aisquare.studio` → `stg`, with the
  staging gateway and hosted proxy filled in; `api.aisquare.studio` → `prod`). It
  is read off the destination and never written to `config.toml`, so no other
  project, the doctor and the shipper resolve exactly what they did before —
  even when the machine's own target has the same name. A
  `[explainability.targets.<name>]` you wrote for that deployment is used, and
  only what it leaves empty is filled in: the hosted proxy only beside the
  deployment's own gateway, never beside another one you set. The machine's
  own target's entry counts only while that target is on the deployment's
  gateway (`enable --gateway-url` with no `--target` writes into it). `use`
  does **not** turn tracing on — that is still `aisquare explainability
  enable`. Unless your entry names
  an `api_key_env` other than the default `EXPLAINABILITY_API_KEY` (which is the
  machine key's, so it is replaced like a missing one), the target names its
  own key variable (`EXPLAINABILITY_PROD_API_KEY`, …), so the machine key —
  issued for whichever deployment set the machine up — never answers for
  another one; the exception is a deployment the machine key already goes to:
  the machine's top-level gateway, or its own target's while that target reads
  the machine key (names no variable of its own). An API host this CLI does not
  know (a self-hosted API, `[::1]`) has no gateway or proxy it can fill in, and
  the machine's never stand in: `use` says `(no gateway known)` and where to set
  them — `gateway_url` and `proxy_url` under
  `[explainability.targets."<host>"]`, not `enable --target`, which would make
  it the whole machine's target, and that entry reads its key from
  `EXPLAINABILITY_<HOST>_API_KEY` unless it names another variable — and until
  then the project's launches go untraced, saying why. The doctor's and
  `status`'s other fixes for a project's deployment name that entry, or `key set
  --project … --from-env <VAR>`, for the same reason. When the machine's own
  target has the deployment's name (its default name is `stg`, whatever gateway
  `init --explainability` gave the machine), that entry is its too and every
  project without a destination reads it, so the fix says to give the machine's
  target a name of its own first. Launches, `fleet spawn` and
  `explainability env` in the project take the proxy **and** the key from this
  target, whatever `AISQUARE_EXPLAINABILITY_TARGET` says: a project's
  destination comes before that variable, which moves only the projects without
  one (`--target` overrides both, and `status` says when the variable is not in
  play). `use` asks about the same target, and once tracing is on it ends with
  the check for what it set up: `aisquare doctor --live --project <id>`, which
  resolves the key the project's launches take (its own first, else the
  machine's) and posts a real span with it, or
  `aisquare explainability key set --project <id>` when there is no key yet.
  `explainability status` probes only the proxy, so it is not that check: a
  revoked key passes it.
- **key** — ingest still needs a workspace key (neither the gateway nor the
  hosted proxy accepts a sign-in token), so the CLI obtains one on your behalf
  unless the project already has its own, scoped to `ingest:write`, named
  `aisquare-cli <host> <project>` in the dashboard's key list, and stores it
  exactly as `key set` would (mode 600, per project). A machine key never stands
  in for that: it only answers meanwhile, and the line says it is not checked to
  be the workspace's. The API does not accept a sign-in token on that endpoint
  yet; until AISquare-Studio-BE#3493 lands the line reads `key: none — …` and
  `aisquare explainability key set --from-env VAR` is the way in — it binds the
  key to the project's destination unless you pass `--target`. A key bound to
  the destination's deployment (minted, or by `key set`) answers only while a
  destination names that deployment: the machine's own target can have the
  same name and be another deployment (its default name is `stg`, whatever
  gateway `init --explainability` gave the machine), so after `use --clear`,
  or a move onto another deployment, the key is kept and not used, the machine
  key applies, and `key show` says why. The other way round too: a key `key set`
  bound to the machine's own target before any `use` does not answer for a
  destination's deployment of that name while the machine's target resolves
  another gateway, so a prod key is never sent to staging. It is kept, answers
  again once the project has no destination, and `key show` says why (`--json`
  carries the binding's `api_url`, null for the machine's target, and whether it
  `serves` the target asked about). Nor does a key bound to a target the
  machine no longer has (renamed, as the fix above says) answer for a
  destination until `key set` attaches it there. A key you
  attached by hand is used as is and never minted over; `key set` over a minted
  key revokes the minted one once the new key is recorded (a `key set` that
  fails leaves the minted key working), and so do `key clear`, `use --clear`, a
  move into another workspace, a new mint over it (when its file is gone), and
  `project forget --purge` or `project prune --purge`. A minted key that cannot
  be revoked yet — you are signed out, signed in to another deployment than the
  one that minted it, offline, or the API refuses — is not forgotten: the
  command says it is still live, and `use`, `logout` and `doctor --live` try
  again until the server confirms (`doctor` lists what is still owed in a
  `minted-keys` row). The key is named `aisquare-cli <host> <project>` in the
  dashboard's key list if you would rather revoke it there.
- **routing** — a span lands in the studio its agent identity is bound to in
  that workspace (unbound identities go to the workspace's *Unassigned* inbox),
  so `use` binds this machine's identities (`aisquare-planner`, `aisquare-coder`,
  …) to the chosen studio with the project's own key — a machine key, unchecked
  to be the workspace's, binds nothing. Binding needs a workspace OWNER/ADMIN
  key or the studio owner's; a refusal is reported per identity, and the
  destination is still recorded.
- `whoami` gains a `traces:` line for the same project; `aisquare logout`
  forgets every key the CLI minted (revoking each on the server when it can, and
  naming any it could not) and leaves hand-attached keys alone.

`status --json` carries the choice under `destination`; `use --json` carries
the destination, the target, the key's standing and the routing result. Only a
workspace you are a member of can be chosen; a pending invitation is listed so
the reason is on screen. The client lane still ships under the machine's target
and key — the per-project shipping follow-up of the section above.

**How much is left.** With a destination chosen, `status` and `whoami` add a
`credits:` line for that workspace — the run and build pools, today and this
month, what is left of each and when it resets, and the server's band when it
is not `ok` (`[low]`, `[exhausted]`); `status --json` carries the numbers under
`credits`. The Accounts page in `aisquare ui` draws the same as bars under the
AISquare card, refreshed on the page's minute tick beside the Claude accounts,
and `doctor --live` adds a `workspace-credits` row that warns before a fleet is
spawned into a low or exhausted workspace. One request per workspace, cached
for a minute, never from a hook or a session path; when the API cannot answer
the line says `credits unavailable` with the reason and nothing else changes.

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
| `proxy … but it ships to <url> while target … is <url>` | **Red.** The proxy is alive and posting to another deployment — your Runs are landing there. Point this CLI at the target's proxy, or restart a local proxy with `EXPLAINABILITY_GATEWAY_URL` set to the target's gateway |
| `proxy … a local proxy ships wherever EXPLAINABILITY_GATEWAY_URL pointed when it was started` | **Amber.** A local sidecar took its destination from the environment it was started with, which need not be your target, and it is too old to say. Restart it with `EXPLAINABILITY_GATEWAY_URL` set to the target's gateway, or point this CLI at the deployment's own proxy — the line spells it out |
| `proxy … does not report a gateway and is not on <gateway>'s host` | **Amber**, and possibly not a fault: a deployment's own proxy behind another hostname looks exactly like another deployment's from here. Confirm on the proxy host what it was started with; a proxy that reports its gateway from `/health` clears this on its own |
| `proxy … no gateway is configured for target` | **Amber.** Tracing is on and there is nothing to compare the proxy against — set `--gateway-url`. If the proxy reports where it ships, the line names that gateway and the command that adopts it |
| `proxy … the gateway configured for target … is unusable` / `explainability config: … gateway needs a scheme` | The stored gateway is not a URL (`stg.example`, no scheme). `enable` and the form refuse this now; a hand-edited config or `EXPLAINABILITY_GATEWAY_URL` can still carry one. Store a full `https://` URL |
| Everything green, nothing on the dashboard | The spool is not being drained — step 10 |
| `… is temporarily unavailable (server error), so auto mode cannot determine the safety of Bash` on every tool call, while the chat itself keeps answering | Auto mode's **classifier** request failing behind the proxy — see the next section |

`aisquare explainability status --json` is the machine-readable view, and the one
to script a check against. Its exit code is non-zero when tracing is on and the
proxy lane is **red** — the proxy would not take a session, or it is alive and
reports that it ships to another deployment; amber exits 0, and
`probe_severity` says which.

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
auto-mode` line whenever a fleet role runs `auto` behind a configured proxy, or
a running agent was launched in `auto` (a restart replays that mode, whatever
the role says now, so the line names the agent): it reads the first-turn size of your recent sessions from their transcripts and
warns when that size is above ~100k tokens, or when a recent session was
refused three times or more (one or two can be a real transient 5xx). `fleet
spawn` puts the same warning on its receipt. A running session that was
launched through the proxy and has been refused three times is put in
**attention** (🔔 on its row) by its Stop hook, with one `auto_mode_blocked`
line on the board — whether tracing is still on or not, because a running
agent keeps the proxy it started with. A session that reaches the three while
`fleet restart` or `fleet switch` is taking it down gets neither — the bell
would cost the replacement its claims, and the line would name a restart
already under way — and the replacement that resumes its transcript is judged
on the refusals it adds, not on the ones it inherits: still refused, it gets
the bell and the line; restarted in a mode that needs no classifier, neither.

**Until the proxy fix, pick one:**

- a permission mode that needs no classifier for the fleet roles:
  `aisquare config set fleet.roles.coder.permission_mode acceptEdits` (per
  spawn: `aisquare fleet spawn coder --permission-mode acceptEdits`; a running
  agent: `aisquare fleet restart <label> --permission-mode acceptEdits` — its
  session resumes, and later restarts keep the mode. A restart replays the mode
  the agent was launched with, so the role's setting alone reaches only the
  agents spawned after it). `config set` only writes a key your config already
  has, and a `config.toml` with its own `[fleet.roles]` has only the roles it
  lists: for a role it leaves out, add a `[fleet.roles.<role>]` table to the
  file with `permission_mode = "acceptEdits"` on the line below it. The doctor
  line and the board line name whichever of the two your config takes;
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
