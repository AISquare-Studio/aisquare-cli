# Handoff: wire `aisquare-cli` to the live CI staging server, end to end

**For:** the agent picking up PR #72 (`feat/collective-intelligence`, draft) after the server went live.
**Written:** 2026-09-02, against `aisquare-cli` `c688e51`, `aisquare-ci` `main` `4cb104b` deployed at
`https://ci-api.aisquare.studio`, and the #cli Slack thread of the same day.
**Owner's standing instruction:** the PR stays a **draft** until Anmol says otherwise. Push to the
branch, tick the checklist, comment — never `gh pr ready`.

The branch already speaks contract v2 and is built against a stub (`docs/ci-contract.md` says what;
`docs/ci-integration-handoff.md` §1 says how far). This document is the delta between that and a
client that talks to the real server, in the order the work should happen, with the things that
will bite stated first.

---

## 0. What is true today, verified from this machine

| Fact | Evidence |
|---|---|
| The server is live and healthy | `GET https://ci-api.aisquare.studio/ready` → 200, postgres/redis/neo4j/migrations all `ok`; `/live` 200; `/metrics` 403 at the ALB (intended) |
| `POST /v1/hook` and `POST /v1/mcp/collective_intelligence_recall` exist and take any experiment token | `app/api/delivery.py`, `app/api/auth.py::ROLE_REQUIREMENTS` on `main` `4cb104b`; Vaibhav's verified `curl` in #cli returned `200 {"status":"empty","action":"noop",…}` |
| Unauthenticated experiment routes answer a proper `error.v1` 401 | `GET /v1/experiment/runs/run_4561e2c4cd5f5318b86d` without a token → `401 {"code":"scope_resolution_failed", "detail":{"credential_present":false}}` |
| **The descriptor still advertises `direct_api` only** | `app/api/runs.py::DIRECT_API_DELIVERY` is a constant `({"kind": "direct_api"},)`, `client_safety_ms` 60 000, `expires_at` = now + 1 h, `retry_policy: none`. See §2 — this is the single fact that decides the shape of the work |
| The seven vendored contracts are byte-identical to the deployed commit | every schema and fixture under `tests/fixtures/ci_contract/v2/` compared against `origin/main` `4cb104b` blobs: all `same`. Nothing to re-vendor; only the recorded commit in the docs is stale |
| A run that works: `run_4561e2c4cd5f5318b86d` | seeded by Vaibhav in `ws_kernel01` with a completed build — the workspace every deployed token resolves to. Do **not** pick a run out of the database; most completed builds belong to fixture workspaces no token reaches and answer `503 dependency_unavailable … has no completed build` |
| Tokens | five in Secrets Manager `aisquare-ci/stg/api-token` (us-east-1), JSON keyed `CITEST_READER_TOKEN`, `CITEST_FIXTURE_WRITER_TOKEN`, `CITEST_RUNNER_TOKEN`, `CITEST_ADMIN_TOKEN`, … Use **`CITEST_READER_TOKEN`**. Never the admin token (`may_attest_on_behalf`, reaches namespace reset). Anmol's IAM user has read via policy `ais-ci-stg-client-access` |
| `main` moved under the branch | PR #72 is `CONFLICTING` (`CHANGELOG.md`); `main` now pins `mcp>=2.1,<3` and ported `services/mcp_server.py` from `FastMCP` to `mcp.server.mcpserver.MCPServer`; a new `ambient` CI job runs the suite on a developer-shaped home |
| Server-side scope mapping is still a stub | `app/api/queries.py::trusted_session_mapping` returns empty project/studio lists, so only workspace-level grants authorize anything. Expect `status: empty` to be the normal healthy answer until the corpus and grants say otherwise |

Not verified from this machine: the token fetch. The `aws secretsmanager get-secret-value` call was
refused by the agent sandbox's permission classifier. The next agent will likely meet the same
refusal — Anmol runs it (`! aws …` in the session) or allowlists it. Nothing below depends on a
value being written down; the token only ever lives in `AISQUARE_CI_KEY`.

---

## 1. The three rules the server states (they already hold in the client — keep them)

1. **Authority is the token, never the body.** Both request contracts close their root; a
   `workspace_id`/`studio_id`/`cypher`/`sql`/`url`/`path` extra is a 422 before any handler runs.
   The client's models are `extra="forbid"` and the id fields are pattern-pinned — do not add a
   field to either.
2. **The hook cannot block.** `action ∈ {inject, noop}`; the client already treats everything that
   is not `inject` as "inject nothing" and the hooks never gate a tool.
3. **Do not learn your arm.** `opaque_config_id` is a handle, `retry_policy` is `none`, the server
   mints `query_id` per call. **No retries anywhere** — a repeat is a new observation and inflates
   the sample. The client has none; do not add one "for robustness".

And two the server team flagged that the client must not code against:
- Both delivery routes take *any* experiment token today. That is a recorded stand-in; the real
  caller is a developer-role principal whose credential does not exist yet. Only `AISQUARE_CI_KEY`'s
  value will change — keep it that way.
- `status: empty` **is a success**. It means auth resolved, scope resolved, the query ran and a
  ledger row was written, and nothing matched. It is never `unavailable`, and the client already
  keeps the two apart (`status` column) — keep the aggregates apart too.

---

## 2. The one thing that decides the work: the descriptor says `direct_api`

The branch is descriptor-gated by design: `services/ci_augment.gate()` fetches
`GET /v1/experiment/runs/{run}` and the hooks call **only** the triggers `delivery[].hook_push`
lists; the recall tool is registered only when `mcp_pull` is listed. Against today's server the
descriptor is valid and says `[{"kind": "direct_api"}]`, so with the branch as-is:

- every prompt records `client_reason = trigger_not_in_descriptor`, and **no hook call is ever made**;
- `aisquare doctor` prints `ci descriptor: run …: direct_api only — the hooks will not call` (that
  line exists and is the correct diagnosis);
- `collective_intelligence_recall` is not registered in `aisquare serve`.

Vaibhav's smoke bypasses the descriptor entirely (he `curl`s `/v1/hook` directly), which is why it
works for him and would not for the CLI. Two ways forward; do the first in any case, and the second
if concrete testing cannot wait for it — **the owner decides whether the second lands at all.**

**A. Server publishes real delivery modes (the fix).** One constant in `app/api/runs.py`:
`DIRECT_API_DELIVERY` → `({"kind":"hook_push","triggers":["session_start","prompt_submit"],"endpoint":"/v1/hook"}, {"kind":"mcp_pull","tool":"collective_intelligence_recall"})`,
plus its fixture/test. Canon `03` §4.1 pinned `direct_api` as the *pre-CLI* mode; the CLI is here.
The schema forbids `direct_api` alongside the others, so it is a replacement, not an addition. Ask
in #cli (Vaibhav) or open an `aisquare-ci` issue citing this section. Until it lands, nothing in the
CLI's hook path can be exercised live.

**B. A loud, recorded staging override in the CLI (interim, owner's call).** Honour an explicit
`AISQUARE_CI_DELIVERY_OVERRIDE=hook_push:session_start,prompt_submit;mcp_pull` **only** when the
fetched descriptor is `direct_api`-only, and make it impossible to mistake for the descriptor's
ruling: a `delivery_source` column on the row (`descriptor | override`, CHECK-constrained, mirrored
in `tests/test_store.py` like the other vocabularies), the join record carrying the same field,
and `doctor` warning on its own line whenever the override is active. It must never apply when the
descriptor lists real modes, and it is removed (or demoted to a test-only seam) once A lands. The
design rejected client-side delivery flags because they are "a second place the experiment's shape
lives" — that argument stands; this is a dated exception for connectivity testing on a host whose
runs are `comparison_eligible: false` anyway, and it must read as one in the code.

---

## 3. Order of work

Each step ends green under `make check` and pushed. Commit per step.

### Step 0 — bring the branch level with `main`
- `git merge origin/main`. Expected conflict: `CHANGELOG.md` only (keep both `[Unreleased]` entries;
  the CI-runs-ambient entry from `main` and the CI test bed entry from the branch).
- `pyproject.toml` auto-merges to `mcp>=2.1,<3` in both extras — re-run
  `.venv/bin/pip install -e ".[dev]"`.
- `services/mcp_server.py` auto-merges, but the branch's recall-tool registration sits inside a
  `build_server()` that now builds an `MCPServer`. `server.add_tool(fn)` still exists on 2.x; check
  the merged file compiles and that the lazy `from aisquare.services import ci_recall` survived.
- `tests/test_ci_recall.py::test_the_mcp_server_registers_the_tool_only_when_available` reaches into
  `server._tool_manager.list_tools()` (a 1.x internal). Port it the way `main`'s
  `tests/test_serve.py` does: `tools = anyio.run(server.list_tools)`.
- The new `ambient` CI job runs the suite on a home with explainability configured and a proxy
  up/down. The CI test bed is unaffected because `tests/conftest.py` clears all four `AISQUARE_CI*`
  knobs — confirm that is still true after the merge, and watch both ambient variants on the PR.

### Step 1 — get a token into the shell and prove reachability with `doctor`

```sh
export AISQUARE_CI_KEY=$(aws secretsmanager get-secret-value --region us-east-1 \
  --secret-id aisquare-ci/stg/api-token --query SecretString --output text \
  | python -c 'import json,sys; print(json.load(sys.stdin)["CITEST_READER_TOKEN"])')
export AISQUARE_CI=1 AISQUARE_CI_URL=https://ci-api.aisquare.studio AISQUARE_CI_RUN=run_4561e2c4cd5f5318b86d
aisquare doctor
```

Expected today: `ci test bed` ✓ (`enabled for https://ci-api.aisquare.studio, run run_4561…`),
`ci endpoint` ✓ (`/ready answered 200 in N ms`), `ci descriptor` ✓ with the detail
`direct_api only — the hooks will not call; ceiling 60000 ms; expires <now+1h>`. That third line is
the §2 fact, seen live. A `token rejected (401)` there means the secret value did not reach
`AISQUARE_CI_KEY` (echo its length, never its value). The token must be in the environment of the
shell that launches the agent; there is no config field for it, on purpose.

If the ALB is ever the problem, the SSM port-forward from the server doc reaches the box directly
(`http://127.0.0.1:8020` is a valid `AISQUARE_CI_URL`; the CLI accepts plain `http://`).

### Step 2 — one live hook call through the client's own models (no Claude Code yet)

Before touching the hooks, prove the CLI's request builder and parser against the real server:

```python
from aisquare.services import ci_client
from aisquare.services.ci_contract import HookRequest, observed_now, wire_session_id
from aisquare.core.ids import new_trace_id

request = HookRequest(
    trigger="prompt_submit", run_id="run_4561e2c4cd5f5318b86d",
    session_id=wire_session_id("live-probe-1"), trace_id=new_trace_id(),
    project_ref="AISquare-Studio/aisquare-cli@feat/collective-intelligence",
    snapshot_ref=None, prompt="agent run", client_safety_ms=60_000,
    client_observed_at=observed_now(),
)
call = ci_client.call(request, url=ci_client.endpoint() + "/v1/hook")
print(call.reason, call.status, call.action, call.server_ms, call.round_trip_ms, call.error_codes)
```

Expected: `ClientReason.none served|empty inject|noop <ms> <ms> []`. `parse_response` returning
`none` on a real body is the proof the vendored contract and the deployed server agree. Anything
else is a finding: `contract_mismatch` means the server moved; `schema_mismatch` names the field;
`http_error` with `status 503` means the run has no completed build (see §0) or the token's
workspace is wrong. Record the response's `query_id`/`briefing_id` when `served` — §3 Step 8 uses
them.

The MCP route is the same shape against the same run and returns a bare `mcp-tool-output.v1`
briefing (status inside), not a hook envelope — `Briefing.model_validate(json.loads(body))`.

### Step 3 — resolve the descriptor gap (§2)

A first. If B is authorised: implement it exactly as §2 describes (env-only, `direct_api`-only
precondition, `delivery_source` on the row and the join record, `doctor` warning, tests for all
four), then re-run Step 1 and confirm `doctor` now warns that the override is active.

### Step 4 — a real Claude Code session against the live server

With the four exports in the shell that launches the agent (Claude Code hooks inherit its
environment), and hooks installed by `aisquare agents connect claude-code` — which now writes
`timeout: 120` on `SessionStart`/`UserPromptSubmit`, needed because the descriptor's ceiling is 60 s:

```sh
aisquare agents connect claude-code       # re-run: refreshes the two hook timeouts
aisquare launch coder                     # or plain `claude`, from the same shell
```

Then, in the session: submit a prompt, let the turn finish, and read back:

```sh
aisquare metrics list --all
aisquare --json metrics show --all
```

What a correct wiring looks like on the rows (`aisquare metrics list --all`):
- one `session_start` row, closed at creation; `client_reason none` if the descriptor (or override)
  lists `session_start`, `trigger_not_in_descriptor` otherwise;
- one `prompt_submit` row per prompt, `client_reason none`, `status empty` (or `served` with
  `action inject`, `query_id qry_hook_…`, `items_count ≥ 1`), `run_id run_4561…`,
  `opaque_config_id cfg_public_…`, `snapshot_ref` a 40-hex object id (the checkout is a repo),
  `redaction_level standard`, and `ended_at` set by `Stop`;
- `deadline_breached False`, `round_trip_ms` a few hundred ms from this region.

The same three hook events can be driven without Claude Code through the console script — this is
what the stub smoke does; the payloads are the ones Claude Code sends:

```sh
SID=$(python -c 'import uuid; print(uuid.uuid4())')
aisquare hook session-start <<< "{\"session_id\":\"$SID\",\"cwd\":\"$PWD\",\"source\":\"startup\"}"
aisquare hook user-prompt-submit <<< "{\"session_id\":\"$SID\",\"cwd\":\"$PWD\",\"prompt\":\"agent run\"}"
aisquare hook stop <<< "{\"session_id\":\"$SID\",\"cwd\":\"$PWD\"}"
```

To see an `inject`, the prompt has to lexically match something in the seeded run's corpus under a
workspace-wide grant. Ask Vaibhav for one prompt that returns `served` against
`run_4561e2c4cd5f5318b86d`, or read what the run holds via `GET /v1/experiment/exports/{run_id}`
(any experiment token). Do not try to ingest your own signals — that needs the runner token and
`tools/http_e2e_loop.py` currently fails on this box (issue #118).

### Step 5 — point the pull path at the server's MCP route

The server exposes the recall tool as **`POST /v1/mcp/collective_intelligence_recall`** taking the
`mcp-tool-input.v1` body and returning the briefing directly. The branch's `ci_recall.forward_recall`
codes seam assumption J7 — forward as `agent_request` through `/v1/hook` — which still works, but
the dedicated route is the one the server means and it **carries `token_budget` and `reason`**
(`token_budget` is read by nothing server-side, by design). Switch the forward to the MCP route:

- URL `{base}/v1/mcp/collective_intelligence_recall`, body `RecallInput.model_dump(exclude_none=True)`
  with `run_id` always filled from the descriptor (the server refuses a missing `run_id` with
  422 `scope_resolution_failed` — no default-run concept);
- response is `mcp-tool-output.v1`; parse with `Briefing.model_validate` behind the same
  http/json/shape ladder (`parse_response` is hook-envelope-specific — add a sibling
  `parse_briefing` in `ci_contract`, total, same reasons);
- drop `NOT_FORWARDED`; the row stays `trigger = agent_request` (that is the CLI's local vocabulary
  for "the agent asked") and gains nothing arm-shaped;
- keep the standing instruction (`INSTRUCTION_VERSION`) — it names the tool and the `ses_` id, both
  unchanged.

Then register and exercise it: `aisquare serve --stdio` with `AISQUARE_CI*` exported and the
descriptor (or override) listing `mcp_pull`; from a Claude Code session with the aisquare MCP server
connected, ask the agent to call `collective_intelligence_recall` — or call `server.call_tool(...)`
in a test the way `tests/test_serve.py` does. Expect a briefing dict that validates against the
vendored `mcp-tool-output.v1` schema, and a row with `trigger agent_request`.

### Step 6 — read the server's `error.v1` bodies instead of bare statuses

Live, a non-200 carries an `error.v1` body with a `code` — `scope_resolution_failed` on 401,
`dependency_unavailable` with "has no completed build" on 503. The client records `http_error` with
detail `status 503`, which loses the sentence that says what to fix. Small enhancement, worth its
own commit: in `ci_client.call`, when the exchange is non-200 and the body parses as `error.v1`
(`ErrorRecord.model_validate`), put `[code]` on the row's `error_codes` and the clipped `message` in
the outcome's `detail`; `doctor`'s descriptor line already special-cases 401/403/404 — extend it to
show `code` too. Never branch behaviour on `retryable` (issue #117: it is not always true).

### Step 7 — join record and the Explainability lane (only if the estate is reachable)

`insights.record_turn` spools a `ci_turn` record when explainability shipping is configured, and the
sweeper emits it as a `ci.turn` span. If this machine has a working explainability target, run one
session with shipping on and confirm the span lands in the session's Run. If not, leave it — the
server's live-estate ingest is not wired either (their doc says so), so the cross-lane join cannot
be observed end to end yet. Record which.

### Step 8 — prove the join from the server side

Every delivery call is a recorded query. With the `query_id` the response carried (Step 2 or a
`served` row), `GET /v1/experiment/queries/{query_id}/grounding` (any experiment token) reads back
the server's record of *our* query. That, next to the local row with the same `trace_id`, is the
"one `prompt_submit` round trip whose server ledger row and CLI metric row share
`(run_id, session_id, trace_id, query_id)`" the seam doc asked for. Do it once for `empty` and once
for `served` if a serving prompt exists, and once with `contract: 1` (a hand-built body; the client
cannot emit one) to see both sides record a mismatch rather than baseline. Paste the pairs in the PR
comment.

### Step 9 — docs, PR, comment (still a draft)

- `docs/ci-contract.md`: status paragraph → "live at `ci-api.aisquare.studio`, verified …"; the
  vendored commit note → "vendored from `fff5646`, verified byte-identical at `4cb104b`"; J7 →
  the MCP route; add the override to the assumptions table if B landed.
- `docs/ci-integration-handoff.md` §1 server table: `/v1/hook` built; MCP built as an HTTP route;
  tokens in Secrets Manager; descriptor still `direct_api` (or fixed, if A landed).
- README test-bed section: replace `https://…` in the example with the real base URL? **No** — the
  README is public; keep the placeholder and point to this doc for staging.
- CHANGELOG `[Unreleased]`: one bullet for live wiring, one for the MCP route, one for B if it exists.
- PR #72 body: tick "Joint smoke" once Step 8 has both pairs; add the override as a follow-up if B
  is meant to be removed. Post a comment with the Step 8 evidence and the Step 4 rows. **Do not
  undraft.**

---

## 4. What must not change

- **No retries**, no client response cache, no `PreToolUse`, no `allow`/`block`/`substitute`.
- **Nothing arm-shaped anywhere** — not a column, not a log line, not a config key. `opaque_config_id`
  is recorded verbatim and never interpreted.
- **The token lives in the environment only.** `tests/test_config.py::test_experiment_has_nowhere_to_put_a_key`
  pins it. Never echo it; `doctor` already strips credentials from URLs.
- **Off costs nothing** and the hooks' output with `AISQUARE_CI` unset stays byte-identical to `main`
  (`tests/test_ci_augment.py`, the two "off returns exactly" tests).
- **Every emitted request validates against the vendored server schema via `jsonschema`**, not a
  Python mirror. If the server's contracts move, re-vendor and let the drift test go red.
- Point at `ci-api.aisquare.studio`, never `ci.aisquare.studio` (reserved for a frontend).
- Staging measures nothing (`comparison_eligible: false`); it is a connectivity and behaviour
  instrument. Do not quote a latency or a hit rate from it.

## 5. Repository guards you will trip

`tests/test_documented_commands.py` sweeps every `.md` for fenced `aisquare …` commands — a new doc
with commands meant to be typed joins `DOCUMENTED` with a `CENSUS` entry (this file did).
`tests/test_every_test_can_fail.py` wants an `assert` in every test body (a helper's assert does not
count). `tests/test_spawn_seams.py` wants a `core.spawn.SEAMS` ruling for every `subprocess` call site.
`tests/test_store.py` holds the SQL CHECK vocabularies equal to the Python enums — a new
`delivery_source` column gets the same test. `tests/test_stubs.py::IMPLEMENTED` and the README
command tree change only if you add a CLI command; you should not need to.

## 6. People and places

- **Vaibhav Bajaj** (`aisquare-ci`, deploy, tokens): the #cli channel. Asks for him: §2 A; a prompt
  that returns `served` for `run_4561e2c4cd5f5318b86d`; whether the developer-role token is coming.
- Server integration doc: `aisquare-ci` branch `deploy/stg-ec2`, `docs/deploy/ci-client-integration.md`
  (also `stg-ec2-handoff.md` for the box and `R1-stg-deploy-and-e2e.md` for the deploy). Deploy from
  `main`, never from `deploy/stg-ec2`.
- Server tickets a client meets: #116 (tokens into the container — fixed in the deploy override),
  #117 (malformed `signal_id` → 503; irrelevant to the hook path, the CLI never sends one),
  #118 (`http_e2e_loop.py` Neo4j password; do not run it), #100 (the deploy runbook).
- The seeded run: `run_4561e2c4cd5f5318b86d`, workspace `ws_kernel01`.
- A sibling clone of `aisquare-ci` at `../aisquare-ci` is what the drift test compares against;
  `git -C ../aisquare-ci pull` brings its working tree to the deployed commit.
