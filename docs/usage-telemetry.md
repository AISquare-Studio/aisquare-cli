# Usage sharing: what is sent, what never is, and the wire contract

aisquare can send AISquare **anonymous daily counts of how it is used**, so that the people who
build it know what to improve. It is **off by default**. Nothing leaves your machine until you
turn it on, you can see what would be sent first, and you can turn it off with one click.

Turn it on or off, either way:

- the one-time question the UI asks after you have set up a project, or the button at the top of
  *Settings ▸ Usage & stats* (*Turn sharing on* / *Turn sharing off*, and *What's shared*);
- `aisquare metrics share on`, `aisquare metrics share off`, `aisquare metrics share status`;
- see the body of the next shipment: `aisquare metrics share preview`. With sharing on it is
  exactly that body (built by the same code, the transcripts read first). Otherwise (sharing off,
  forced off, or switched on by hand with `config set sharing.enabled true` before this
  installation has an id) it is captioned as what it is: a **sample** of your last 31 days with
  placeholder ids, to show the shape; the days before you turn sharing on are never sent. With
  `--json` the answer is `{"sample": bool, "sample_reason": str|null, "body": {…}}`, so a script
  can tell a sample from the body of a shipment.

## What we collect

Counts, per local calendar day, and nothing else:

- **Tokens** by kind (input, cache write, cache read, output) and per model name, the list-price
  cost of them, and how many sessions and turns there were.
- **Accounts**: how many Claude Code accounts you have and used that day, the plan of each
  (`pro`, `max`, `team`, …) as a count, and how much of the day's tokens the busiest one spent.
  No email, no alias, no slot.
- **Repositories**: how many were active, and for the 25 busiest of the day a **salted hash** of
  the repository (see below) with its tokens, sessions and turns, and four coarse traits: where
  it is hosted (`github`, `gitlab`, `bitbucket`, `other`, `none`), its main language, a size
  bucket (`xs` to `xl`) and whether it is a monorepo.
- **Which features you use**: how often each command (`cli.fleet.spawn`) and each page of the UI
  (`tui.view.usage_stats`) was used, by name. Commands run by tools on your behalf (hooks, the MCP
  server) are not counted.
- **The fleet**: how many agents were started, by built-in role (`coder`, `tester`, …; a role you
  invented is `other`), and how many tasks were marked done.
- **Your installation**: aisquare's version, your operating system and its version, the CPU
  architecture, the Python version and how aisquare was installed (`brew`, `pipx`, `uv`, `pip`,
  `script`, `unknown`), and a **random id** made the first time you turn sharing on (a UUID that
  is not derived from you or your machine).

## What we never collect

Repository **names**, **paths** and **remotes**; **prompts**, replies, file names or any text you
or an agent wrote; **emails**, account aliases or any name of a person; **command arguments**
or their values; your machine's name or IP address (the server does not store it).
A repository is sent only as `HMAC-SHA256(a random salt kept on your machine, its normalised git
remote or its path)`, cut to 16 characters: the salt is yours alone, so nobody can check a guess
against it and the same repository looks different to every user.

## Who it is linked to

If you are signed in to AISquare, a shipment carries your sign-in token and the server links the
counts to your AISquare account. If you are not (or the server does not accept the token) it is
sent anonymously, and is only tied to the random installation id.

## When it is sent

Never while you are waiting: a command you run starts at most one background shipment every 6
hours, and the UI sends from a background thread; a shipment has 5-second timeouts and does not
touch the command or hook that started it. Sharing starts from the day you turn it on: the days
before it were not agreed to and are never sent. Turning it on *again* while it is on changes
nothing (the first day, what was sent and the schedule stay): only a change from off to on starts
afresh. Each day is sent again whenever its counts
change, and today's every time.

A shipment that cannot be delivered (offline, a server that is not there yet, a refusal)
**says nothing**: no message, no error and no change to a command's output. The days stay unsent
and go the next time the server answers, oldest first, in batches of at most 31 days, however
long that takes; nothing is lost and nothing is given up on.

## Switching it off

`aisquare metrics share off` (or the button) turns it off at once, deletes what was kept about what
was sent, and sends **one** notice that you opted out (only if anything was ever sent). A shipment
already under way is stopped: it looks at the switch again before every request, so at most the
request that is already out finishes (5 seconds at most), and nothing it would have recorded is
written after the deletion. Nothing is sent after the notice until you turn sharing on again. Saying
*off* is also the answer to the one-time question, which is not asked afterwards. These force it off whatever the switch says, and
suppress the question: `DO_NOT_TRACK=1`, `AISQUARE_NO_TELEMETRY=1`, and any `CI` run.
`aisquare metrics share reset-id` gives the installation a new id and salt.

## Where it lives in the code

`src/aisquare/services/usage_share.py` builds and sends the batch, `src/aisquare/core/sharing.py`
holds the rules (forced off, the state, the backoff), and the local figures it is made of are
`src/aisquare/services/usage_stats.py` (see [Usage & stats](usage-stats.md)).

---

# CLI usage telemetry — wire contract v1.1 (aisquare-cli ⇄ AISquare-Studio-BE)

The contract both ends implement (the endpoint is AISquare-Studio-BE's task #3). A change to it
is agreed by both first. **v1.1** is v1 with the amendments of the team board (seq 3631), marked
*(v1.1)* below; the `schema_version` of the body stays `1`.

## Principles
- Local stats are always recorded and shown; NOTHING leaves the machine unless
  the user opted in. Default OFF. `DO_NOT_TRACK=1` or `AISQUARE_NO_TELEMETRY=1`
  forces off and suppresses the consent prompt; so does CI (`CI` env set).
- Repos are shared ONLY as a salted hash + coarse traits. Never names, paths,
  remote URLs, emails, prompts, file names, command arguments or any text.
- Consent: one-time TUI dialog after onboarding (Share / No thanks / See what
  would be sent) + one-click toggle on Settings ▸ Usage & stats +
  `aisquare metrics share on|off|status|preview`.

## Endpoint
`POST {api_url}/api/v1/cli/metrics/` — `api_url` is the same base the CLI's
AISquare sign-in already uses. JSON body, `Content-Type: application/json`.

Auth is OPTIONAL:
- Signed in → CLI sends `Authorization: Bearer aisq_…` (its existing device-flow
  token). BE links the rows to that user.
- Not signed in → no header. Accepted, stored unlinked.
- Invalid / expired / revoked / wrong-scope token → BE MUST still accept the
  batch as anonymous (200, `linked: false`), never 401. (DRF's default auth
  raises before AllowAny; BE uses a lenient auth class.) Belt and braces: the
  CLI retries once without the header if it ever gets a 401.

Throttle: per-IP named rate (env-backed, default 120/hour). Body cap 256 KB.

Redirects *(v1.1)*: the endpoint answers where it is asked. The CLI **never follows a redirect**
on this POST (so the bearer token cannot reach another origin); a 3xx is a retryable failure, and
the CLI posts to the path **with** its trailing slash.

## Request body (schema_version 1)
```json
{
  "schema_version": 1,
  "install_id": "8c1f…-uuid4",
  "sent_at": "2026-10-26T04:00:00Z",
  "opt_out": false,
  "client": {
    "cli_version": "0.9.1", "os": "darwin", "os_version": "27.0",
    "arch": "arm64", "python": "3.12.3",
    "install_method": "brew|pipx|uv|pip|script|unknown"
  },
  "days": [
    {
      "date": "2026-10-25",
      "tz_offset_minutes": 330,
      "active": true,
      "signed_in": true,
      "tokens": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "total": 0},
      "cost_usd_list": 0.0,
      "sessions": 0,
      "turns": 0,
      "models": {"claude-opus-5-5": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}},
      "accounts": {"active": 2, "registered": 3, "by_type": {"max": 2, "pro": 1}, "top_account_share": 0.71},
      "repos_active": 4,
      "repos": [
        {"repo_hash": "3fa9c0d1e2b4a6f8", "tokens_total": 0, "sessions": 0, "turns": 0,
         "traits": {"host": "github|gitlab|bitbucket|other|none", "language": "python",
                    "size_bucket": "xs|s|m|l|xl", "monorepo": false}}
      ],
      "features": {"cli.fleet.spawn": 3, "tui.view.usage_stats": 2},
      "fleet": {"agents_spawned": {"coder": 3, "tester": 1}, "tasks_done": 2}
    }
  ]
}
```
Field rules:
- `install_id`: random uuid4 minted at first opt-in, stored in CLI state; NOT
  derived from hardware/user. Kept across opt-out/in unless the user resets it.
- `date` is the user's LOCAL calendar day; `tz_offset_minutes` its offset. *(v1.1)* Any integer
  in [-840, 840]: west of UTC is negative, and is not an error.
- `active` = at least one CLI command, TUI launch or agent turn that day. DAU on
  the BE = distinct `install_id` with `active=true` for a date.
- `tokens.total` = input + cache_write + cache_read + output and MUST equal what
  `aisquare metrics tokens` / the local dashboard report for the same day.
- `accounts.by_type` keys = Claude subscription type as the CLI reads it
  (`core/claude_accounts.py` subscription_type; "api" for API-key accounts; "unknown"
  otherwise). No emails, aliases or slot ids. *(v1.1)* A closed set: `pro`, `max`, `team`,
  `enterprise`, `api`, `unknown`, `other`. The CLI maps anything else to `other` (counts summed);
  the BE stores a key it does not know under `other` and never answers 400 for it.
- `models` keys *(v1.1)*: the CLI sends only ids matching `^claude-[a-z0-9.-]{1,60}$`; every other
  model name is summed under the key `other`. (A model name is text from a transcript.)
- `repo_hash` = first 16 hex of HMAC-SHA256(key = per-install random salt kept
  in CLI state, msg = normalised git remote URL, else the repo root path).
  Per-install salt ⇒ the same repo is NOT correlatable across users.
- `repos`: top 25 by tokens_total for that day; `repos_active` is the full count.
  Clones of one remote are one repo.
- `features`: counts keyed by command path (`cli.<group>.<verb>`) or TUI view
  (`tui.view.<name>`), ≤ 200 keys, names only — never arguments or values.
- Batch: ≤ 31 days, oldest first. The CLI resends TODAY on every ship (it grows)
  and every day since opt-in not yet acknowledged, in successive batches.
- `opt_out: true` with `days: []` is sent once, best-effort, when the user turns
  sharing off; the BE stamps `opted_out_at` on the installation. Nothing is
  sent after that until the user opts in again.

## BE semantics
- Upsert keyed by (install_id, date): the newest batch for a day REPLACES that
  day's row (idempotent; resends are safe).
- Installation row keeps latest `client` fields, first_seen/last_seen, and a
  nullable user FK (latest linked user); each daily row also snapshots the
  nullable user FK at receipt time. `on_delete=SET_NULL`.
- Do not store the client IP.
- Responses: `200 {"accepted_days": <int>, "linked": <bool>}`;
  `400 {"errors": …}` on validation (unknown schema_version, caps exceeded,
  date outside [today−400d, today+1d], negative numbers); `429` throttled.
  *(v1.1)* "Negative numbers → 400" covers token, cost and count fields only; `tz_offset_minutes`
  may be any integer in [-840, 840]. The 200 body is **exactly** those two keys (`accepted_days`
  an integer, `linked` a boolean); the CLI treats any other 200 body as a retryable failure and
  does not mark the days as sent.
- Unknown extra keys inside a day are ignored (forward-compatible), not 400.

## What the client does that the contract leaves open
- A shipment is acknowledged **only** by a 200 whose body is the contract's reply: an object with
  `accepted_days` an integer of at least 1 and `linked` a boolean (extra keys are ignored). An
  empty object, an error with a 200, a gateway's page, a string or 0 for `accepted_days`, a missing
  `linked` — none of it is an answer of the endpoint; the days stay unsent and go next time.
- The CLI checks the switch before every request of a shipment and again before it records the
  answer, so `share off` stops a shipment under way (see *Switching it off*).
- `signed_in` is the sign-in's state when the batch is built, for every day in it.
- `install_method`: `brew` (Homebrew), `pipx`, `uv` (uvx), `script` (the one-line installer's
  uv tool), `pip` (a virtual environment or system pip), `unknown` (a checkout).
- `language` is `unknown` when no tracked file is in a language the CLI knows.
- A failure of any kind (no network, 404 because the endpoint is not deployed, 4xx, 5xx, 429, a
  reply that is not JSON) is silent on the client and retried after a backoff of 5 minutes
  doubling to 6 hours, for ever; the days are rebuilt from the local record, so none is lost.
