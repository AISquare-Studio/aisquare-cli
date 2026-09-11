# Team OS peer fixtures — P11

Recorded response bodies for the three peer endpoints P11 is allowed to call,
plus the three error bodies it must map. They drive
`tests/office/test_team_os_adapter.py` through an in-process fake transport.

**No value here was observed on the wire.** The Team OS peer is stopped on this
machine and starting it was not authorised, so every shape below was derived
from the source-mined inventory at `docs/research/teamos-fixtures/` in the
AISquare-Office repository (branch `prep/teamos-fixtures`), which read the peer
repository at revision `83ed1a2`, entry point `app/server.js`.

All values are **synthetic**. Trust the keys, the types, the nullability and the
status codes; do not trust any name, email, identifier, date or commit. No
token, key, account identifier or personal filesystem path appears in this
directory, and none may be added to it.

`peer_down` has no fixture on purpose: a stopped peer produces no HTTP exchange
at all — the TCP connect is refused and there is no status code and no body. The
fake transport models it by raising, not by returning a file.

| File | What it is |
| --- | --- |
| `meta_200.json` | `GET /api/meta`, ordinary success. |
| `meta_200_degenerate.json` | `GET /api/meta` from a non-repository root: empty strings, every key still present. |
| `roster_200.json` | `GET /api/roster`, two rows. |
| `roster_200_empty.json` | `GET /api/roster` with no roster table — a 200 with `roster: []`, which is valid data and never an outage. |
| `roster_200_missing_slack.json` | A row whose `slack_id` key is **absent**, not null: `JSON.stringify` drops undefined values. |
| `view_board_200.json` | `GET /api/views/board`. |
| `view_team_pending_200.json` | `GET /api/views/team-pending` — the same builder, echoing the other name. |
| `view_roadmap_200.json` | `GET /api/views/roadmap`. |
| `view_customers_200.json` | `GET /api/views/customers` — object keys are lowercased Markdown headers, so they are dynamic. |
| `view_calendar_200.json` | `GET /api/views/calendar` — hardcoded month, and a trailing release event with no `status` key. |
| `view_cockpit_200.json` | `GET /api/views/cockpit`. Off the default allow-list; recorded so the decoder can be tested if an operator ever enables it. |
| `error_401.json` | Missing **and** wrong token are indistinguishable; both produce this body. |
| `error_403.json` | The origin/host gate, which runs *before* the token gate. |
| `error_404_unknown_view.json` | Unknown view name. Both fields echo the caller's input, which is why the adapter never forwards peer error text. |
| `manifest.json` | Inventory, with the provenance of each entry. |
