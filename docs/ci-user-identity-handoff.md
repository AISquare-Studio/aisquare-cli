# Handoff: the CLI's signed-in user becomes the Collective Intelligence principal

**For:** whoever wires the CI test bed to `aisquare login` once PR #77 lands.
**Written:** 2026-09-03, on top of `feat/collective-intelligence` (PR #72, `f7fd7fb`), against
`docs/plans/aisquare-login.md` (PR #77, the sign-in contract) and `AISquare-Studio-BE` PR #3419 (the
identity provider). The server side of this plan is `docs/handoff-2026-09-03-user-identity.md` in
`aisquare-ci`; the identity-provider side is `docs/CI_RESOURCE_SERVER_IDENTITY.md` in
`AISquare-Studio-BE`. This document is the CLI's share, and it is small on purpose: the CLI already
sends a bearer on every call and never puts authority in a body.

This is a reference, not a script. Commands appear as inline code and nothing here is meant to be
pasted, so `test_documented_commands.py` leaves it alone.

---

> **Review round 8, 2026-09-12.** The sweep is scoped to this module's own
> `me-*.json` files in the CI cache directory (the descriptor cache shares it
> and carries the same `until` key), and it runs from `forget` and from a
> refusal write as well as from a document write - the quiet cases after a
> rotation. `signed_in_display` returns email, subject and source apart, and
> the doctor lines branch on the email being known rather than on a substring
> test of a collapsed string.
>
> **Review round 7, 2026-09-12.** Writing a `GET /v1/me` document sweeps every
> cached document and refusal whose own expiry has passed, so a token refresh
> no longer leaves its predecessor's identity document behind for the life of
> the machine; and `doctor`'s "signed in as" note is built from the memoised
> session read that chose the bearer (`ci_client.signed_in_display`), never a
> second parse of the credentials file.
>
> **Review round 6, 2026-09-12.** When `GET /v1/me` is a 404 and `/ready` did
> not answer either, the `ci identity` line states the one conclusion - this
> endpoint is not answering as a CI server - with `AISQUARE_CI_URL` and its
> current value as the fix; the test-bed line points at it and the endpoint
> line keeps its fact about `/ready` without a competing "turn the test bed
> off". One cause, one diagnosis, one fix.
>
> **Review round 5, 2026-09-11.** A 404 from `GET /v1/me` is read as "a CI
> server that predates the route" only when `/ready` answered; otherwise it
> is a warning about the URL, run exported or not, since a stale host or a
> proxy that 404s the unknown produces the same status. The no-run warning's
> fix names only the lines that were printed. The session memo's key is the
> credentials file's RESOLVED path, so a symlinked home is one entry and a
> relative `AISQUARE_HOME` under a chdir is not the previous directory's.
>
> **Review round 4, 2026-09-11.** `available()` and `forward_recall` take no
> `cwd` again: an MCP tool call carries none, so the server process's working
> directory is the resolution for registration and every pull, and the
> docstring says so instead of describing a per-pull re-resolution nothing
> could supply. `doctor` treats a 404 from `GET /v1/me` as a server that
> predates the route - informational when a run is exported (the hooks never
> ask it then), a warning whose fix is the export when none is - rather than
> as a server that is down. The session memo is keyed by the credentials
> file's path and a digest of its bytes, so two homes without the file are not
> one entry and a same-length rewrite inside one timestamp tick is seen. The
> constant `has_bearer` condition is gone from the branch that could not
> express it.
>
> **Review round 3, 2026-09-11.** `doctor` asks `GET /v1/me` for ANY bearer,
> as the hooks do - an experiment token with no run exported resolves the
> fixture workspace's run, and the `ci identity` line names harness bearers
> too (the workspace line is skipped when a run is exported, since the export
> wins). The session memo is keyed by the credentials file's mtime and size as
> well as the environment token, so a sign-in in another terminal is seen by a
> running `fleet ui` or `serve`; `available()` takes the agent's `cwd`; and the
> fix table is indexed safely.
>
> **Review round 2, 2026-09-11.** The MCP recall path now passes the project
> to the gate (its argument is required, so a caller cannot forget it again);
> `bind-workspace` and `doctor` resolve the project through `active_project`,
> exactly as the hooks do, so a stale pin cannot file a binding no hook reads;
> an expired stored login is withheld with "sign in again" rather than sent;
> `run_for` returns a reason code that `doctor` switches on instead of matching
> words; the https-or-loopback rule lives once, in `iam.safe_transport`; and
> the credentials file is read once per process.
>
> **Progress, 2026-09-10.** Everything in this document is built. The client
> half (C1–C4) landed first; C3's two identity lines and C2a's hidden
> `aisquare ci bind-workspace` are in as of this revision, and the server half
> (the identity-provider adapter, the `aisq_` branch, memberships as the trusted
> session mapping, the run-scoped workspace check, `CITEST_IDP_*`) shipped in
> `aisquare-ci` #141 together with `CITEST_WORKSPACE_ID`, the variable that lets
> a controller publish a run in a workspace a real developer is a member of.
> The identity provider's share (`AISquare-Studio-BE` #3420: the `aisquare-ci`
> service client, `sub` on introspection, `aisq_` on both authorize views, the
> bulk `GET /api/v2/iam/me/access/`) is in too. What remains is deployment
> (none of the three is deployed yet) and **C5**, which waits on server item
> S10 exactly as before.
>
> **Verified locally, end to end, over real HTTP.** The provider (BE #3420) and
> the CI server (#141) running locally in Docker and on the host, this CLI
> signed in through `AISQUARE_TOKEN` with no `AISQUARE_CI_KEY` and no
> `AISQUARE_CI_RUN`: `doctor` printed the identity lines (two workspaces, none
> bound, the fix naming `aisquare ci bind-workspace`); `bind-workspace` with no
> argument refused and listed both with their runs; bound to the team, `doctor`
> was five green lines with the run taken from `GET /v1/me` and the descriptor
> fetched as the developer; the hooks then ran and correctly recorded
> `trigger_not_in_descriptor`, because the server's descriptor still publishes
> `direct_api` only — which is S10, and the reason C5 waits. Nothing under
> `AISQUARE_HOME` contained the token afterwards. The earlier progress note
> follows for the record.
>
> **Progress, 2026-09-08.** The joint contract and the client half of C1–C4 are
> built and green; the identity-provider adapter is the remaining server work.
>
> **`me.v1` is settled and implemented** (`aisquare-ci` #141, `cf92959`):
> `GET /v1/me` returns the principal plus, per workspace, the run published
> there. Closed at the root and inside every `workspaces[]` member, because this
> is the second document a client fetches and the descriptor's blinding argument
> holds only while this one stays identity and routing. The role enum is
> **inlined rather than `$ref`-ed into `principal.v1`** — the first draft
> `$ref`-ed it and the CLI's own conformance suite refused it in one run, because
> a `$ref` by absolute `$id` resolves only for a consumer that also holds the
> referenced schema and the CLI has no business vendoring a server kernel record
> it never sees. The two copies are held equal by a server test.
> `active_run_id` is nullable rather than omitted, so "no run here" is a fact
> the client reads instead of an absence it infers.
>
> **C1 (bearer precedence) is done.** `ci_client.api_key()` is
> `AISQUARE_CI_KEY`, else the signed-in session (`iam.current_session()`, which
> itself prefers `AISQUARE_TOKEN` over the stored token). The experiment token
> keeps precedence, so the harness and the joint smoke are untouched. `iam` is
> imported inside the function, so "off costs nothing" still holds and `iam`
> stays the one reader of the `iam_*` keys.
>
> **C4 (scrubbing) is done, and wider than the plan said.** `scrub_secret`
> replaces every fragment of *every* candidate bearer, not only the one
> precedence picked: a detail being scrubbed may have been produced while a
> different source was winning, and a scrubber that tracked the winner would
> leak the loser.
>
> **C3 (doctor) is done.** The `ci test bed` line names which credential is in
> use — "experiment token from `AISQUARE_CI_KEY`" or "signed in as <email>
> (aisquare login)" — never its value. Signed in with nothing exported, the run
> comes from `GET /v1/me` and two lines follow: `ci identity` (the principal the
> server resolved, the workspace count) and `ci workspace` (bound workspace,
> role, run), each warning with the command that fixes it. Both probes are
> bounded to three seconds and cache nothing.
>
> **C2 (`GET /v1/me` at session start) is done, with the bound the plan did not
> ask for.** `services/ci_me.py` fetches once per bearer, caches for five
> minutes, and **caches a refusal for sixty seconds** — this call sits in front
> of the descriptor fetch on the synchronous session-start path, so an
> unbounded, uncached one would have cost every session its own ceiling and then
> the descriptor's on top. That was a review finding against this document, not
> something it planned. The cache is keyed by a hash of the bearer, so signing
> in as somebody else cannot serve the previous identity's routing.
>
> **C2a (workspace binding) is done.** `[experiment].bindings` maps each
> PROJECT (the CLI's `prj_…` id, the same one every metrics row carries) to one
> workspace, so binding repo A never re-tenants repo B on the same machine —
> the review caught that the first draft was a single machine-wide field, which
> contradicted its own "a property of the checkout" argument. A developer in a
> single workspace needs nothing. Several workspaces and none bound **refuses to
> guess** and names the choice. The hidden `aisquare ci bind-workspace [ws_…]`
> sets this checkout's entry from the list `GET /v1/me` returns (uncached),
> refuses a workspace the user is not in, binds the only one without being told,
> and `--clear` forgets this project's entry alone. It is the one config write on
> the CI surface, and it lives in `cli/ci.py` where the call-graph guard can see
> it.
>
> **C5 (retire the override) is unchanged and still waiting on server item S10.**
>
> Verified locally: `ruff format` · `ruff check` · `mypy --strict` (252 files) ·
> **3142 passed, 2 skipped**, including 24 new tests over the `/v1/me` client and
> a stub `GET /v1/me` route the suite drives end to end. The server side is green
> too: **3674 passed / 148 skipped**, and its new SQL was run against a
> throwaway Postgres 15 with all 18 migrations applied.
>
> **What is still blocked, and on what.** Everything identity-shaped needs
> `AISquare-Studio-BE` #3419 (the provider) and `aisquare-cli` #77 (`aisquare
> login`) to merge — both are open. Server items S1–S3, S5, S6 and S8 (the
> introspection adapter, the `aisq_` branch, memberships as the trusted session
> mapping, the run-scoped workspace check, the three `CITEST_IDP_*` names) are
> not built: they cannot be exercised until the provider seeds a `service`
> client for `aisquare-ci` with the `introspection` scope, which is the ask in
> `AISquare-Studio-BE` #3420.

---

## 1. What changes for a user, in one sentence

After `aisquare login`, the hooks and the recall tool talk to the CI server **as that user**: the
bearer they send is the user's `aisq_` token instead of an experiment token, the server maps it to
`usr_<uid>` in the workspace the run belongs to, and every metrics row the CLI writes joins a ledger
row that names the same person.

## 2. What the CLI already does that this relies on

- The bearer is read from the environment only (`AISQUARE_CI_KEY`), sent as `Authorization: Bearer`
  on the descriptor fetch, `POST /v1/hook` and the recall route, and never written to a row or a log
  (`ci_client.api_key`, `scrub_secret`).
- The request bodies are closed and carry no workspace, studio or user id; `project_ref` and the
  `ses_` session id are selectors (`ci_contract.HookRequest`, `RecallInput`).
- The descriptor is the only run document the client reads, and it decides delivery.
- `doctor` asks the questions in the order the hooks hit them and never echoes a credential.

None of that changes. The token's *value* changes, and the CLI learns two things it did not need
before: which workspace the user is acting in, and which run applies there.

## 3. What PR #77 gives us

- `iam_token` (`aisq_…`, 90 days, no refresh), `iam_api_url`, `iam_sub`, `iam_email` in the
  credentials file, read only through the `iam` helper module that PR adds (`access_token()`, `request()`).
- `AISQUARE_TOKEN` as a read-only environment override.
- An `aisq_` rule in `core/redaction.py`.
- `session_expired` and `not_authenticated` error codes with their messages.

## 4. Work items (after PR #77 merges)

### C1. Bearer precedence

`ci_client.api_key()` becomes: `AISQUARE_CI_KEY` when set (the experiment token, unchanged
semantics), else `AISQUARE_TOKEN`, else the stored `iam_token`, else `""`. The `iam` module stays
the single reader of `iam_*` keys, so `ci_client` calls it rather than the credentials store. The
multi-line and scrubbing rules apply to whichever value is in use.

`doctor`'s `ci test bed` line says which source the bearer came from: "experiment token from
`AISQUARE_CI_KEY`" or "signed in as anmol@… (aisquare login)" or "no bearer: run `aisquare login` or
export `AISQUARE_CI_KEY`". The token value never appears; the email is fine, it is what `whoami`
prints.

### C2. `GET /v1/me` at session start

The server gains `GET /v1/me` (`me.v1`): the principal it resolved for this bearer and, per
workspace the user belongs to, the run the controller has published there (`active_run_id`, or
null). At `SessionStart`, before the descriptor fetch, the CLI calls it once and caches the answer
beside the descriptor (`~/.aisquare/cache/ci/me-<sha256(bearer)[:16]>.json`, until the descriptor
expires). Then:

- the run is `AISQUARE_CI_RUN` when set (unchanged, wins), else the `active_run_id` of the workspace
  this project is bound to (C2a), else `no_run` as today;
- the descriptor fetch proceeds exactly as now with that run.

`GET /v1/me` is identity and routing only; it carries nothing about delivery or configuration, so it
adds nothing the blinding argument has to defend.

### C2a. Workspace binding per project

A user may belong to several workspaces, and CI's tenant is the workspace. The run names the
workspace on the server side; the CLI needs to say which of the user's workspaces it means when it
asks for a run. One config field, `[experiment].workspace = "ws_…"` in the project's configuration,
set by a new hidden command `aisquare ci bind-workspace` that lists the workspaces `/v1/me` returned
and stores the chosen id. Unset means: if `/v1/me` lists exactly one workspace with an active run,
use it; otherwise `no_run` with a `doctor` line naming the choice to make. The binding is a
selector: the server refuses a run in a workspace the user is not a member of, whatever the binding
says.

### C3. `doctor` lines for identity

Two lines after `ci test bed`, only when the bearer is an `aisq_` token:

- `ci identity`: `signed in as <email> — CI resolves usr_<uid> in <n> workspace(s)`; warn with the
  fix `aisquare login` on a 401 from `/v1/me` (`session_expired`).
- `ci workspace`: `<workspace id> (<role>), run <run_id>`; warn when the binding names a workspace
  the user is not in, or when no run is published there.

Both probes go through the existing bounded transport; neither caches anything.

### C4. Redaction and the row

- The CI transport's `scrub_secret` already replaces the configured bearer in every detail. Extend
  it to whichever source C1 chose, so a stored `iam_token` is scrubbed exactly like an exported key.
- The `aisq_` redaction rule from PR #77 covers prompts and details that quote one.
- No token, hash of a token, `sub` or email is added to the metric row. The row keeps
  `run_id`, `session_id`, `trace_id`, `query_id` as its join keys; the server's ledger row carries the
  principal. `metrics show --json` therefore stays free of personal data.

### C5. Retire the staging override

`AISQUARE_CI_DELIVERY_OVERRIDE` exists only because the staging descriptor still says `direct_api`.
When the server publishes real delivery modes (server item S10), delete `services/ci_override.py`,
the `delivery_source` override branch in the gate, the doctor line and the tests that pin them; the
`delivery_source` column stays and reads `descriptor` on every new row.

## 5. Tests to add

- A CI stub route for `GET /v1/me` in `tests/stub_ci_server.py`, programmable like the descriptor.
- `ci_client.api_key()` precedence: env key beats `AISQUARE_TOKEN` beats stored token beats nothing;
  the scrubber covers each; a multi-line stored token is unusable and named by `doctor`.
- Session start with a signed-in user: `/v1/me` fetched once, run taken from the bound workspace,
  descriptor fetched with it, rows recorded as today; `AISQUARE_CI_RUN` still wins when set.
- `doctor`: the three identity states (experiment token, signed in, nothing), the 401 path, the
  unbound-workspace path.
- `metrics --json` output contains no `aisq_`, no email, no `sub` (extend the existing
  credentials-never-reach-the-output tests).
- `conftest.isolated_home` clears `AISQUARE_TOKEN` (PR #77 adds this) alongside the `AISQUARE_CI*`
  knobs.

## 6. What must not change

- **Authority never in the body.** No workspace, studio, user or run selector is added to
  `hook-request.experimental-v2` or `mcp-tool-input.v1`. The workspace binding is CLI-side routing
  that ends up as the `run_id` the contracts already carry.
- **Off costs nothing.** With `AISQUARE_CI` unset the hooks read no credentials file and make no
  call; the `iam` module is imported lazily on the CI path.
- **No retries, no client cache of briefings, no `PreToolUse`.**
- **The experiment token keeps working** and keeps precedence, so the staging harness and the
  joint smoke are unchanged.

## 7. Acceptance

One real Claude Code session, signed in with `aisquare login`, with no `AISQUARE_CI_KEY` and no
`AISQUARE_CI_RUN` exported: `doctor` shows the identity lines, the session start injects or records
`empty`, the prompt row is closed by `Stop`, and the server's grounding record for the row's
`query_id` names `usr_<uid>` for the signed-in user. That is the acceptance for the CLI half of
Slice 13's "server-resolved authorization".
