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
