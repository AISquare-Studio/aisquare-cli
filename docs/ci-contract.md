# CI hook contract v2 — what this CLI speaks, and what it assumes

The wire protocol between the `aisquare` CLI and the Collective Intelligence
server is **hook contract v2**, owned by the server repository (`aisquare-ci`,
`contracts/jsonschema/delivery/`). This file is not a second copy of it. It says
where the authoritative bytes live in this repository, what the CLI does with
each surface, and which decisions the CLI has coded a *default assumption* for
while the two sides settle them (the joint list is
[`ci-integration-handoff.md`](ci-integration-handoff.md) §6).

> **Status.** The CLI side is built and tested against the server's schemas and
> a local stub. `POST /v1/hook` is not yet served by `aisquare-ci`, so nothing
> here has exchanged a real turn. When it does, integration is a config change
> plus the smoke at the end of this file.

## The authoritative bytes, vendored

| What | Where in this repository | Source |
| --- | --- | --- |
| The seven schemas the CLI consumes | `tests/fixtures/ci_contract/v2/schemas/*.schema.json` | `aisquare-ci/contracts/jsonschema/{delivery,kernel}/` |
| One valid and one invalid fixture per schema | `tests/fixtures/ci_contract/v2/*.{valid,invalid}.json` | `aisquare-ci/contracts/fixtures/{valid,invalid}/` |

Copied byte for byte from `aisquare-ci` `main` @ `fff5646`. `tests/test_ci_contract.py`
validates the fixtures against the schemas with `jsonschema` (proving the `$ref`
resolver, not just the files), round-trips every fixture through the CLI's
pydantic models unchanged, and validates **every request this build can emit**
against `hook-request.experimental-v2` — via the server's schema, never via a
Python reading of it. When the server repository is cloned beside this one, a
further test fails if any vendored file differs from the server's; re-vendor
rather than edit.

The pydantic mirror is [`src/aisquare/services/ci_contract.py`](../src/aisquare/services/ci_contract.py):
`additionalProperties: false` at every level, frozen, strict (no coercion), with
every pattern and cross-field rule as a validator. One deliberate deviation,
tested as such: `error.v1.code` is an opaque string rather than the closed
catalog, because the catalog is the server's and a code this build has never
seen is data to record verbatim, not a reason to discard an otherwise valid
response.

## The surfaces, and what the CLI does with each

**Descriptor — `GET /v1/experiment/runs/{run_id}`** (`client-delivery-descriptor.v1`).
Fetched at `SessionStart`, cached under `~/.aisquare/cache/ci/` until
`expires_at`, refetched on expiry. It is the only run document the CLI reads and
it decides everything: `delivery[].hook_push.triggers` says which hooks call the
server and `.endpoint` where; `mcp_pull` says whether the recall tool is exposed;
`client_safety_ms` is the wall-clock ceiling for every call; `retry_policy: none`
is honoured literally. The descriptor carries no architecture, source, reader or
arm field, so the CLI is structurally unable to know its arm.

**Push — `POST {endpoint}`** (`hook-request.experimental-v2` → `hook-response.experimental-v2`).
Sent on `session_start` and `prompt_submit` when the descriptor lists them. All
ten request fields, nulls included; `prompt` null on `session_start`, required
otherwise. On `action: inject` the CLI frames `briefing.rendered_context` and
appends it **after** any team delta; on `noop` it injects nothing. Every field a
ledger join needs is recorded on the local row (below).

**Pull — `collective_intelligence_recall`** (`mcp-tool-input.v1` → `mcp-tool-output.v1`).
Registered in `aisquare serve`'s MCP server only when the descriptor lists
`mcp_pull`. A standing instruction naming the tool and the exact `ses_…` to pass
is injected at `SessionStart`. The tool forwards as `trigger: agent_request`
through the hook endpoint (assumption J7, below) and returns the `briefing`
object as its result.

**Observation.** Tool activity does not go through the hook. Sessions are traced
into Explainability by the proxy lane; the CLI's part of the join is the
`ci_turn` record it spools through the client lane, keyed by pipeline id.

## The client-side reason, beside the server's status

Every turn writes one row (`aisquare metrics list`) whether or not the server
was called. `status`/`action` are the server's words; `client_reason` is the
CLI's, in three groups that aggregates never mix:

| Group | Values | Meaning |
| --- | --- | --- |
| baseline | `disabled` `not_configured` `no_run` | the client never asked — control data |
| by design | `trigger_not_in_descriptor` `no_prompt` `no_session` | the experiment was on and the client chose not to call |
| failures | `descriptor_unavailable` `transport_error` `deadline_exceeded` `http_error` `malformed_body` `contract_mismatch` `schema_mismatch` | the client tried; treated like the server's `unavailable`, never as "nothing to add" |

`none` means the server answered and this build understood it. Round-trip
percentiles are taken over `none` rows only.

## Assumptions coded as defaults (joint decisions still open)

Each is one constant or one function so the settlement is a small change.

| Seam | Assumption in this build | Where |
| --- | --- | --- |
| J2 ids | `ses_` + the Claude Code session id; `trc_` + ULID per turn, minted here; the CLI never mints `run_` or `qry_` | `ci_contract.wire_session_id`, `core.ids.new_trace_id` |
| J3 snapshot | 40-hex object id from `git stash create` (dirty) or `HEAD` (clean); the object is kept alive under `refs/aisquare/wip/<trace_id>`; untracked files are excluded and the row says so | `services/ci_snapshot.py` |
| J4 ceiling | `client_safety_ms` from the descriptor, enforced as wall clock; the installed `SessionStart`/`UserPromptSubmit` hooks carry `timeout: 120` so Claude Code does not discard the hook first | `ci_client.exchange`, `core.agents.CONTEXT_HOOK_TIMEOUT_SECONDS` |
| J7 pull | recall forwards as `agent_request` via the hook endpoint; `token_budget` and `reason` have no field on that request and are reported as `not_forwarded` | `ci_recall.forward_recall` |
| J10 config | `AISQUARE_CI`, `_URL`, `_KEY`, `_RUN` (and `[experiment].enabled/url/run`); nothing about arms anywhere | `ci_client` |
| J12 `run_kind` | not sent; recorded locally as `live` on every row, `replay` reserved for the runner | `ci_augment.RUN_KIND` |
| J13 redaction | the configured `redaction.level` scrubs the prompt before it leaves; the level is recorded on the row | `ci_augment.outbound_prompt` |
| J14 frame | on, `aisquare-ci-frame/1`: caveat before and after, delimited region the payload cannot close, 16 KB cap, both sizes recorded | `core.injection.build_retrieved_block` |
| J15 error codes | recorded verbatim as an opaque list on the row | `ErrorRecord.code` |
| J16 health | `doctor` probes `GET /ready`, then fetches the descriptor without caching it | `services/diagnostics.py` |

Also assumed, not a joint item: a `session_start` or `agent_request` row is
closed at creation (it is a call, not a turn); an open prompt row older than 24 h
is left open rather than closed by a late `Stop`.

## The stub, and the smoke

`tests/stub_ci_server.py` speaks v2 — `GET /ready`, the descriptor route, and a
programmable `POST /v1/hook` (status, body, a delay before headers, a drip that
sends the body in slow pieces). It is what every client test runs against, and
a human can run it too:

```sh
python -m tests.stub_ci_server --port 8765
```

In another shell, with the exports it prints:

```sh
export AISQUARE_CI=1 AISQUARE_CI_URL=http://127.0.0.1:8765 AISQUARE_CI_KEY=x AISQUARE_CI_RUN=run_kernel0001
aisquare doctor            # three green "ci …" lines
aisquare metrics list      # one row per hook event, once a Claude Code session has run
```

The same commands against the real server are the joint smoke: one
`prompt_submit` round trip whose server ledger row and CLI metric row share
`(run_id, session_id, trace_id, query_id)`, and one deliberately mismatched
request recorded on both sides as a mismatch rather than as baseline.
