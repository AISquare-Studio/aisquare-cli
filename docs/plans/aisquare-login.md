# `aisquare login`: browser sign-in through the AISquare identity provider

Status: contract frozen 2026-09-03. All three parts are implemented and were
verified together end to end on 2026-09-04: backend AISquare-Studio-BE PR #3419
(branch `feat/oauth-oidc-provider`), CLI in this repo's PR #77 (section 4), web
approval page and Settings sessions card in aisquare-studio-unified PR #970
(section 5). Merge order: backend, then the web app, then the CLI release.

This document is a reference, not a script: commands appear as inline code, and
the transcript blocks are illustrations of terminal output. The user guide is
`docs/signing-in.md`.

## 1. Decision

AISquare-Studio-BE becomes an OAuth 2.0 and OpenID Connect authorization server
(django-oauth-toolkit 3.4.1, which ships the RFC 8628 device grant, OIDC
discovery, JWKS, userinfo, RFC 7009 revocation and RFC 7662 introspection). The
CLI is the first registered client and signs in with the device authorization
grant. Every future client, whether a desktop app, an editor extension or
another backend, registers a client row and uses the same standard endpoints
with an off-the-shelf OAuth library. Nobody writes login endpoints per client
again.

- The CLI holds one opaque access token, valid 90 days, no refresh token. When
  it expires or is revoked the CLI asks the user to sign in again.
- Tokens are database rows, so revocation is immediate and the web app's
  Settings page lists sessions per client with a Revoke button.
- Completion is detected by polling the standard token endpoint every 5
  seconds. No localhost listener, no WebSocket. prod and stg deploy only the
  WSGI api tasks, and the same HTTPS path serves laptops, SSH boxes, WSL,
  containers and phones.
- The web app keeps its current SimpleJWT login untouched. The OAuth server
  sits beside it inside the same Django process. Extracting it into its own
  service later is a deployment change, not an API change, because consumers
  only ever see the standard endpoints.

Why not a separate auth microservice today: the user table has foreign keys
from every app, two frontends and the explainability gateway depend on the
current JWT and cookie shape, and social login, password reset and email
verification all live in the IAM app. That extraction is months of migration
and does not help the CLI. The explainability gateway already treats IAM as
the identity authority by calling the token-verify and authorize endpoints.
The standard endpoints formalise that.

## 2. Architecture

```text
 aisquare-cli                      AISquare-Studio-BE (Django)                 unified web app
 ------------                      ---------------------------                 ---------------
 GET  /o/.well-known/openid-configuration    -> endpoint URLs
 POST /o/device-authorization/               -> device_code, user_code, URLs
 open verification_uri_complete ---------------------------------------------> /cli?code=WDJB-MJHT
                                             lookup / approve / deny <-------- (Bearer JWT of the signed-in user)
 POST /o/token/ (grant device_code) every 5s -> authorization_pending ... -> access_token
 GET  /o/userinfo/ (Bearer aisq_...)         -> sub, email, name
 any API call (Bearer aisq_...)              -> request.user, token row bumps last_used
 POST /o/revoke_token/ on logout             -> row deleted, immediate
```

Issuer per environment. Discovery lives at `{issuer}/.well-known/openid-configuration`.

| Environment | API host | Issuer | verification_uri |
|---|---|---|---|
| prod | `https://api.aisquare.studio` | `https://api.aisquare.studio/o` | `https://home.aisquare.studio/cli` |
| stg | `https://stg-api.aisquare.studio` | `https://stg-api.aisquare.studio/o` | stg unified host + `/cli` |
| dev | `https://studio-api-dev.aisquare.com` | `https://studio-api-dev.aisquare.com/o` | dev unified host + `/cli` |
| local compose | `http://localhost` | `http://localhost/o` | `http://localhost:3005/cli` |

The backend derives the issuer from its own public origin setting and the
verification URIs from `UI_HOST`, so no client hardcodes a path. Clients read
discovery and follow it.

## 3. Identity provider contract (backend)

### 3.1 Standard endpoints (django-oauth-toolkit, mounted at `/o/`)

| Endpoint | Purpose | Notes |
|---|---|---|
| `GET /o/.well-known/openid-configuration` | discovery | advertises every endpoint below, `grant_types_supported`, `scopes_supported` |
| `GET /o/.well-known/jwks.json` | RS256 public keys | verifies `id_token`; other services can verify locally |
| `POST /o/device-authorization/` | start a device sign-in | form body `client_id`, `scope`; headers `User-Agent`, `X-Device-Name` (see 3.4) |
| `POST /o/token/` | poll for the token | `grant_type=urn:ietf:params:oauth:grant-type:device_code`, `device_code`, `client_id` |
| `GET /o/userinfo/` | who the token belongs to | `Authorization: Bearer aisq_...` |
| `POST /o/revoke_token/` | sign out | form body `token`, `client_id`; always 200 |
| `POST /o/introspect/` | resource servers validate tokens | requires a client with the `introspection` scope; for the gateway later |
| `GET /o/authorize/` | authorization code + PKCE | mounted but no client is allowed that grant yet; needs a web consent page (follow-up) |
| `GET /.well-known/oauth-authorization-server/o` | RFC 8414 metadata at the origin root | same document as discovery |

Device authorization response:

```text
POST /o/device-authorization/
Content-Type: application/x-www-form-urlencoded
User-Agent: aisquare-cli/0.6.0 (Linux; x86_64)
X-Device-Name: anmol-laptop

client_id=aisquare-cli&scope=openid+profile+email+aisquare

200 {"device_code": "<opaque, 30 characters>",
     "user_code": "WDJB-MJHT",
     "verification_uri": "https://home.aisquare.studio/cli",
     "verification_uri_complete": "https://home.aisquare.studio/cli?code=WDJB-MJHT",
     "expires_in": 900,
     "interval": 5}
```

Token endpoint while polling:

```text
POST /o/token/
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code=...&client_id=aisquare-cli

400 {"error": "authorization_pending"}      keep polling
400 {"error": "slow_down"}                  add 5 seconds to the interval, keep polling
400 {"error": "access_denied"}              the user chose Deny, stop
400 {"error": "expired_token"}              the code expired or is unknown, stop
400 {"error": "invalid_grant"}              malformed request, stop
429 Retry-After: <seconds>                  throttled, wait that long (cap 30 s), keep polling
200 {"access_token": "aisq_...",
     "token_type": "Bearer",
     "expires_in": 7776000,
     "scope": "openid profile email aisquare"}
```

No `refresh_token` is issued to the CLI client, and the device grant carries no
`id_token`: identity comes from the userinfo call. `expires_in` is 90 days.

Userinfo:

```text
GET /o/userinfo/
Authorization: Bearer aisq_...

200 {"sub": "<user uid>", "email": "anmol@aisquare.studio", "email_verified": true,
     "name": "Anmol Majithia", "preferred_username": "anmol"}
```

`sub` is the user's stable `uid`, never the integer primary key.

Revocation:

```text
POST /o/revoke_token/
Content-Type: application/x-www-form-urlencoded

token=aisq_...&client_id=aisquare-cli

200 (empty)     also 200 for an unknown token, per RFC 7009
```

### 3.2 First-party device approval API (for the web app, Bearer JWT of the signed-in user)

All under `/api/v2/iam/oauth/device/`, JSON bodies, header-only JWT
authentication, `IsAuthenticated` plus the default email-verified permission.
Errors are 400s in the house envelope: `{"error": {...}, "code": "CODE_NOT_FOUND"}`;
branch on `code`.

| Endpoint | Body | Success | Error codes |
|---|---|---|---|
| `POST lookup/` | `{user_code}` | 200 `{user_code, status, client: {client_id, name, kind}, scopes: [{name, description}], device: {name, os, arch, version}, request_ip, requested_at, expires_at, now}` | `CODE_NOT_FOUND`, `CODE_EXPIRED`, `CODE_ALREADY_USED`, `CODE_CLAIMED_BY_OTHER`, `OAUTH_DEVICE_FLOW_DISABLED` |
| `POST approve/` | `{user_code}` | 200 `{status: "authorized"}` | `NOT_CLAIMANT` plus the lookup codes |
| `POST deny/` | `{user_code}` | 200 `{status: "denied"}` | `NOT_CLAIMANT` plus the lookup codes |
| `POST release/` | `{user_code}` | 204 | `NOT_CLAIMANT`, `CODE_NOT_FOUND` |

Semantics:

- `lookup` normalises the code (uppercase, drop anything that is not a letter
  or digit, re-insert the hyphen after four characters), then claims the
  pending grant for the calling user. A second lookup by the same user is
  idempotent. A lookup by a different user marks the grant denied and returns
  `CODE_CLAIMED_BY_OTHER`, so the polling CLI receives `access_denied`.
- `approve` and `deny` require that the caller is the claimant and the grant
  is still pending and unexpired. `approve` sets the grant to authorized; the
  next poll mints the token. `deny` sets it to denied.
- `release` frees the claim so a different account can sign in ("Not you?
  Switch account").
- Device fields come from the `User-Agent` and `X-Device-Name` headers the CLI
  sent at start. They are labelled "reported by the device" in the UI. Time
  and IP are observed by the server.
- The waffle flag `oauth_device_flow` gates `lookup`, `approve`, `deny` and
  `release`. It is created staff-only. It never gates the standard endpoints,
  because an anonymous device-authorization call cannot satisfy a staff flag.
  When the flag is off, `lookup` returns `OAUTH_DEVICE_FLOW_DISABLED` and marks
  the grant denied so the CLI stops promptly instead of timing out.

### 3.3 Sessions API (Bearer JWT or `aisq_` token)

Under `/api/v2/iam/oauth/sessions/`:

| Endpoint | Purpose |
|---|---|
| `GET /` | the caller's live tokens: `[{id, client: {client_id, name, kind}, device: {name, os, arch, version}, created, last_used_at, last_used_ip, expires, scope, current}]` |
| `POST /<id>/revoke/` | revoke one token; 204, or 404 when it is not the caller's |
| `POST /revoke-all/` | revoke every token of the caller; 204 |

`current` is true for the token that made the request.

### 3.4 Token and client facts

- Access token: `aisq_` followed by 43 URL-safe characters (256 bits). Stored
  as a row with a SHA-256 checksum index. The device code is a 30-character
  opaque string from oauthlib. Lifetime per client, 90 days for
  `aisquare-cli`. Idle expiry: a token unused for 30 days stops working.
  `last_used_at` and `last_used_ip` are updated at most once a minute.
- Scopes: `openid`, `profile`, `email`, `aisquare` (full API access as the user;
  first-party clients only), `introspection` (resource servers). A token
  without the `aisquare` scope authenticates only at `/o/userinfo/`, never on
  the API.
- Authentication order on the API: the OAuth class runs first and only when
  the bearer value starts with `aisq_`; everything else falls through to the
  existing `CookieJWTAuthentication`. Web sessions are unaffected.
- Client registry: the swapped `Application` model gains `kind` (`cli`,
  `desktop`, `web`, `mobile`, `service`, `integration`),
  `access_token_lifetime_seconds`, `issue_refresh_token`, `is_first_party` and
  `description`. One row per grant type per client. Adding a client is a data
  row through the admin or the `seed_oauth_clients` command, not a PR.
- Seeded client: `client_id=aisquare-cli`, public, grant `device_code`, kind
  `cli`, lifetime 90 days, no refresh token, first-party, allowed scopes
  `openid profile email aisquare`.
- User code: 8 characters from `BCDFGHJKLMNPQRSTVWXZ` (no vowels, no
  look-alikes) shown as `XXXX-XXXX`. Device code and user code expire after
  900 seconds. Poll interval 5 seconds; polling faster than the interval
  returns `slow_down` and adds 5 seconds.
- ID tokens are RS256. The private key comes from the `OIDC_RSA_PRIVATE_KEY`
  setting (PEM, base64 in the environment). Local and test runs generate an
  ephemeral key when it is unset.
- Trusted proxy: `NUM_TRUSTED_PROXIES` (1 on prod and stg) so every per-IP
  throttle and every displayed IP uses the client address the load balancer
  saw, not a spoofable `X-Forwarded-For` prefix.
- Throttles: device-authorization 60/hour per IP; token endpoint 600/hour per
  IP plus per-device-code pacing; revoke 60/hour per IP; lookup, approve, deny
  and release 30/hour per user. Rates are environment knobs.
- Kill switch: `OAUTH_PROVIDER_ENABLED=false` makes every `aisq_` token fail
  authentication and device-authorization return 503, within one request.
- Nothing about the CLI credential is logged or sent to Sentry: `device_code`,
  `user_code`, `access_token`, `token` and `id_token` are on the redaction
  list.

## 4. CLI handoff (aisquare-cli)

Branch from `main`. The auth stubs already exist and are hidden: `login`,
`logout`, `whoami` in `src/aisquare/cli/root.py`, the `auth` group in
`src/aisquare/cli/auth.py`, and `src/aisquare/services/auth.py` calling
`stub()`. Replace the stubs, un-hide the commands, and delete `auth rotate`
(tokens do not rotate).

### 4.1 Commands

- `aisquare login [--no-browser] [--with-token] [--api-url URL]`
- `aisquare logout`
- `aisquare whoami` (offline, reads the credentials file)
- `aisquare auth status [--live]` (`--live` calls userinfo)
- `aisquare auth token` (prints the token for scripting; warns on stderr when stdout is a TTY)

Base URL resolution: `--api-url`, then `AISQUARE_API_URL`, then
`AppConfig.api_url` from `~/.aisquare/config.toml` (default
`https://api.aisquare.studio`). Refuse a non-https URL unless the host is
`localhost` or `127.0.0.1`.

### 4.2 The login algorithm

1. If `AISQUARE_TOKEN` is set, exit 1 with the `env_token_set` message. If a
   credential for this API URL already exists, print the "Already signed in"
   line and continue; the old token is revoked after the new one is stored.
2. Fetch `{api_url}/o/.well-known/openid-configuration`. Cache it for 24 hours
   at `~/.aisquare/cache/oidc/<host>.json`. Read `device_authorization_endpoint`,
   `token_endpoint`, `userinfo_endpoint`, `revocation_endpoint`. A 404 means
   the server does not support device sign-in: exit 1 with `unsupported_server`.
3. POST the device authorization request (section 3.1) with
   `User-Agent: aisquare-cli/<version> (<platform.system()>; <platform.machine()>)`
   and `X-Device-Name: <socket.gethostname()>` truncated to 63 characters.
4. Print the code and both URLs. Unless headless or `--no-browser`, open
   `verification_uri_complete` with the `webbrowser` module in a daemon thread.
   Headless means any of `SSH_CONNECTION`, `SSH_TTY`, `CI`, `CODESPACES` set,
   stdout not a TTY, or Linux without `DISPLAY` and `WAYLAND_DISPLAY`. If
   `BROWSER` is set, honour it regardless (`echo`, `true` and `:` mean print
   only). Never launch a text-mode browser (lynx, w3m, links, elinks,
   www-browser). The URL is always printed because `webbrowser.open` returns
   true unreliably.
5. Poll the token endpoint every `interval` seconds plus 20 percent and up to
   one second of jitter. `slow_down` adds 5 seconds. 429 sleeps
   `min(Retry-After, 30)`. A network error doubles the wait up to 60 seconds.
   Stop on `access_denied`, `expired_token`, `invalid_grant`, or after
   `expires_in + 60` seconds.
6. On 200, call userinfo with the new token, then store the credential
   (section 4.3) and print the success line. If an older token for the same
   host was stored, POST it to the revocation endpoint (ignore failures).
7. Ctrl-C at any point prints the cancel line and exits 130. Esc does the same
   when stdin is a TTY (raw-mode key read: `termios` on POSIX, `msvcrt` on
   Windows), best effort. Nothing is stored on cancel.

Every HTTP call uses `urllib.request` with a 10 second timeout and
form-encoded bodies. No new dependencies. Network imports stay inside function
bodies: `tests/test_import_cost_of_the_integration.py` asserts that importing
the CLI never pulls `ssl` into the base closure.

### 4.3 Storage

Use the existing single writer `aisquare.core.credentials.store(**values)` and
reader `load_all()`. All values are strings. Add a `drop(*keys)` function at
the end of that module and change nothing else in it: PR #65 rewrites the same
file and a signature change would conflict.

| Key | Value |
|---|---|
| `iam_api_url` | the API base URL the token belongs to |
| `iam_token` | `aisq_...` |
| `iam_token_expires_at` | ISO 8601 UTC, computed from `expires_in` at receipt |
| `iam_scope` | the granted scope string |
| `iam_sub` | userinfo `sub` |
| `iam_email` | userinfo `email` |
| `iam_name` | userinfo `name` |
| `iam_client_id` | `aisquare-cli` |

`AISQUARE_TOKEN` overrides the file read-only. `conftest.isolated_home` must
clear `AISQUARE_TOKEN` and `AISQUARE_API_URL`. Add an `aisq_` rule to
`src/aisquare/core/redaction.py`.

One helper module, `src/aisquare/services/iam.py`, is the only reader of the
`iam_*` keys: `access_token()` (env, then file), and
`request(path, body=None, *, workspace=None)` which sends
`Authorization: Bearer` and optional `X-Workspace-Id`, and maps 401 to the
`session_expired` error, 429 to `rate_limited`. There is no refresh path.
Other commands call this module and never touch the file. Pin that with an
AST test in the style of `tests/test_one_key_resolver.py`.

### 4.4 Terminal experience

```text
$ aisquare login
! First, note your one-time code: WDJB-MJHT
  Check that the browser shows the same code before you authorize.
  Open this URL to continue in your browser:
  https://home.aisquare.studio/cli?code=WDJB-MJHT
  You have 15 minutes to approve this request.

  Opening your browser at home.aisquare.studio...
⠋ Waiting for approval in the browser · next check in 3s · code expires in 14:21
  Press Esc or Ctrl-C to cancel

✓ Signed in as anmol@aisquare.studio (anmol-laptop)
  This session expires Dec 2, 2026. Sign out any time with aisquare logout.
```

The live line uses `rich.live.Live` (rich is already a dependency) and
refreshes once a second. Under `--json`, a non-TTY, or `--quiet` there is no
live line, just the static lines. Headless replaces the "Opening" line with
`Couldn't open a browser here. Visit the URL above on any device.` A browser
launch failure prints `⚠ Failed to open a browser. Visit the URL above
manually.` and continues.

Exit 1 messages through the existing `fail()` envelope, keyed by error code:

| Code | Message |
|---|---|
| `expired` | ✗ The code expired before it was approved. Run aisquare login again. |
| `access_denied` | ✗ The request was denied in the browser. Nothing was stored. |
| `rate_limited` | ✗ Too many sign-in attempts from this network. Try again in N minutes. |
| `unreachable` | ✗ Could not reach https://api.aisquare.studio: <detail>. |
| `unsupported_server` | ✗ https://api.aisquare.studio does not offer device sign-in (no OpenID discovery document). |
| `env_token_set` | ✗ AISQUARE_TOKEN is set, so aisquare is using that token. Unset it to sign in with the browser. |
| `api_url_not_https` | ✗ Refusing to send credentials over plain http to <url>. Use https, or localhost for development. |
| `session_expired` | ✗ Your AISquare session has expired or was revoked. Run aisquare login. |
| `not_authenticated` | ✗ Not signed in. Run aisquare login. |
| `api_url_mismatch` | ✗ You are signed in to <stored url> but this command targets <resolved url>. Run aisquare login --api-url <url>, or unset AISQUARE_API_URL. |

Ctrl-C or Esc: `✗ Sign-in cancelled. Nothing was stored.` exit 130.
`logout`: `✓ Signed out. The session was revoked on the server.`; when the
server is unreachable: `✓ Signed out on this machine. Couldn't reach the
server. Revoke this session from Settings > Security.` `whoami`:
`anmol@aisquare.studio · https://api.aisquare.studio · expires in 89 days`.

### 4.5 `--json`

Stdout is exactly one object per command. `login --json` first writes one
line to stderr, `{"event": "verification", "verification_uri_complete": ...,
"verification_uri": ..., "user_code": ..., "expires_in": 900}`, then a plain
line `If you are an agent, ask the user to visit the URL above.`, then waits.

| Command | Object |
|---|---|
| `login`, `whoami` | `{"user": {"sub", "email", "name"}, "api_url", "expires_at", "source": "env" or "file"}` |
| `auth status` | `{"signed_in", "source", "api_url", "user", "expires_at", "live"}`; exit 1 when not signed in but still print it |
| `auth token` | `{"token"}` |
| `logout` | `{"signed_out", "server_revoked"}` |

### 4.6 Tests

- A loopback identity-provider stub in the `tests/test_explainability_ops.py`
  gateway pattern serving discovery, device-authorization, token (pending,
  slow_down, denied, expired, 429, success), userinfo and revoke.
- Storage: keys written, 0600 asserted with `tests/fsperms.py`, `drop()`.
- Headless detection and the `BROWSER` branches with `webbrowser.register`
  fakes; never a real browser in tests.
- `--json` shapes and the stderr event line, pinned in `test_json_mode.py`.
- The AST single-reader test for `iam_*` keys.
- `test_documented_commands.py`: add `docs/signing-in.md` (the user-facing
  page you write) to `DOCUMENTED` and its row to `CENSUS`. Leave this plan
  out of `DOCUMENTED`; it has no fenced shell blocks by design.
- Un-hiding the commands changes the README command census; re-measure.

### 4.7 Files and conflict avoidance

Touch: `src/aisquare/services/auth.py` (replace stubs), new
`src/aisquare/services/iam.py`, new `src/aisquare/core/browser.py`,
`src/aisquare/cli/root.py` and `src/aisquare/cli/auth.py` (un-hide, remove
`rotate`), `src/aisquare/core/credentials.py` (append `drop()` only),
`src/aisquare/core/redaction.py`, `tests/conftest.py`, README command tree,
CHANGELOG, `docs/signing-in.md`.

Do not touch `src/aisquare/core/config.py`, `src/aisquare/cli/app.py`,
`src/aisquare/cli/common.py`, `src/aisquare/core/spawn.py`,
`src/aisquare/services/diagnostics.py`: PRs #65, #71 and #72 all rewrite
them. A doctor row and the TUI presence line are follow-ups after those land.
The credentials file gains a 90-day secret, so once #65 merges, switch the
write to its `restrict_to_owner` helper.

### 4.8 Testing against a local backend

Bring up the workspace stack with `make go` from `aisquare-workspace`, check
out the backend branch `feat/oauth-oidc-provider`, run migrations, run
`python manage.py seed_oauth_clients`, and turn the `oauth_device_flow` flag
on for your user in the Django admin. Point the CLI at `http://localhost`
with `--api-url`. Discovery is at `http://localhost/o/.well-known/openid-configuration`.
Until the web page in section 5 exists, approve through the Django admin or
by calling the approve endpoint with a JWT from the web app.

### 4.9 Acceptance

- Happy path, headless path, denied path, expired path, Ctrl-C path all
  produce the copy above and the right exit code.
- A second `aisquare login` replaces the session and revokes the old token.
- `whoami` works offline. `auth token` prints only the token on stdout.
- Every network import is inside a function. `ruff`, `ruff format --check`,
  `mypy` and `pytest` are green on Python 3.11, 3.12 and 3.13.

## 5. Web approval page (aisquare-studio-unified, separate handoff)

Route `/cli`, mounted inside `Protected` outside the shell like `/onboarding`,
so a signed-out visit goes through login and returns via the existing
`postAuthRedirect` stash. States: framed refusal when `window.top !==
window.self`; `enter_code` when `?code=` is missing or invalid; `review` with
the code as per-character spans, the client name, the device card, "Requested
N minutes ago from <ip>", the expiry countdown, "Signing in as <email>. Not
you? Switch account", and Authorize and Deny at equal weight with no autofocus;
`approved` ("Done. Your terminal should now say Signed in. You can close this
tab."); `denied`; `not_found`; `claimed_by_other`; `disabled`; `throttled`;
`email_not_verified` with resend. Settings > Security gains a "Connected apps
and devices" card over the sessions API with Revoke and Revoke all. The card is
required before the flag widens beyond staff.

## 6. Rollout and follow-ups

1. Backend PR merges behind the staff-only `oauth_device_flow` flag; the
   standard endpoints are live but only staff can approve.
2. CLI PR merges with `login` un-hidden; the release note says staff only
   until the flag widens.
3. Web page PR, then the sessions card. Flag widens to everyone.
4. Follow-ups: the web app moves to authorization code + PKCE against the same
   server; the explainability gateway validates through introspection or JWKS
   instead of the custom authorize endpoints; internal services use the
   client-credentials grant instead of shared secrets; access tokens are
   hashed at rest (the toolkit stores the value plus a checksum today).
