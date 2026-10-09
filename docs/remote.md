# Phone control: `aisquare remote`

`aisquare remote` puts the fleet on your phone: what needs you across every
project, each agent's live screen and conversation, the board, and, once you
allow it, the keys and actions to answer a prompt, tell an agent something, or
stop, restart or switch it. It is one small web server on your machine, reached
through your own ngrok tunnel. Nothing is hosted anywhere else, and the page the
phone loads ships inside aisquare-cli.

```text
  phone ──https──▶ ngrok (your tunnel) ──▶ 127.0.0.1:8750/r/<token>/ ──▶ the fleet
  (the page)                                 aisquare remote              (tmux, the board)
       ▲                                           │
       └──── Web Push (FCM · Mozilla · Apple · Windows) ◀── "something needs you"
```

- **One port, loopback only.** The server binds `127.0.0.1:8750` and nothing
  else. ngrok, or a browser on the same machine, is the only way in.
- **A secret URL, then a passphrase.** Every path lives under `/r/<token>/`, a
  32-character random token; a wrong one is a 404 everywhere. The page then asks
  for the four-word passphrase the machine shows, once per browser.
- **Read-only until you say otherwise.** Everything you can see is a read. Every
  change to the fleet is refused until `aisquare remote allow-write on`.

---

## Install

```sh
uv tool install --python 3.13 --with tiktoken 'aisquare-cli[remote]'   # installed by install.sh or uv
pipx inject aisquare-cli starlette uvicorn websockets cryptography     # installed with pipx
pipx install 'aisquare-cli[remote]'                                    # not installed yet, with pipx
pip install 'aisquare-cli[remote]'                                     # in a virtualenv
```

The `remote` extra adds the web server (starlette, uvicorn, websockets) and
`cryptography`, which Web Push needs; without it the page works and says
notifications are unavailable. Use the line for how aisquare-cli was installed.
uv cannot add a package to a tool, so the first line installs it again, and that
keeps only what the line names: tiktoken, as install.sh installs it. A tool
installed with more needs that named too, or it goes: another extra such as
`serve` (`'aisquare-cli[remote,serve]'`), another `--with`. Without the extra,
`aisquare remote serve` and the R panel print the line for this machine, with
everything its tool was installed with. An upgrade through install.sh installs
aisquare-cli again without its extras, and Remote then says its line again.

Then ngrok, which gives the machine an https address a phone can reach. Install
it from ngrok.com, then sign in once:

```sh
ngrok config add-authtoken <your-ngrok-token>
```

**Use a static domain.** ngrok's free plan gives every account one, and it is
what keeps the link, the phone's sign-in and a Home Screen app working across
restarts. Without it the URL changes each time ngrok starts, and the phone has
to open the new link and unlock again. Tell aisquare which one is yours:

```sh
export AISQUARE_REMOTE_NGROK_URL=https://your-name.ngrok-free.app
```

With it set, the fleet UI starts ngrok on that domain (`--url=`), and the server
uses it for the links in notifications.

---

## Start

**From the fleet UI.** Run `aisquare ui` and press `R`. Switch Remote on: the
panel starts the server and ngrok, shows the link, a QR code and the passphrase,
the write switch, the auto-off timer (30, 60 or 120 minutes, or Never) and the
devices that have unlocked. Scan the QR code with the phone. If ngrok stops, the
UI restarts it within half a minute, and a link ngrok announces late (a network
still coming up) is shown, and used for notifications, as soon as it comes. With
the panel closed, a notice says when a Remote that was on could not come back on
as the UI started, when phones cannot reach it (ngrok missing or not up, and once
it is up after all), when ngrok came back on a new link, and when auto-off turned
Remote off. The panel serves on port 8750, or on the one an exported
`AISQUARE_REMOTE_PORT` names, as `serve` does.

**From a shell**, for a machine without the UI open:

```sh
aisquare remote serve --auto-off 120 --public-url https://your-name.ngrok-free.app
ngrok http --url=your-name.ngrok-free.app --inspect=false 8750
```

`serve` prints the local link and the passphrase and runs until Ctrl-C or until
auto-off. A Ctrl-C while a phone's write is still running (a restart or switch
can take 40 seconds) says which, and waits for it: cut short, it can leave the
agent down. A second Ctrl-C quits at once and leaves it unfinished, giving a
notification still on its way, such as auto-off's farewell, two seconds at most.
Quitting the fleet UI waits, and says so, the same way: first for Remote's server
and ngrok to stop, then for the write, and a Ctrl-C in either wait quits at once
(ngrok stopped first). Its options:

| option | default | what it does |
| --- | --- | --- |
| `--port N` | 8750 (`AISQUARE_REMOTE_PORT`) | the local port, 1 to 65535 |
| `--auto-off MINUTES` | 60 (`AISQUARE_REMOTE_AUTO_OFF`) | Remote turns itself off after this long, a week (10080) at most; `0` means never, and the banner says so |
| `--public-url URL` | `AISQUARE_REMOTE_NGROK_URL` | the https address phones use, for the links in notifications; without it a notification opens the page, not its card |
| `--dist DIR` | the page aisquare-cli carries | serve another build of the page |

The page is part of aisquare-cli, so a fresh machine needs no other step.
`aisquare remote install-page <dist>` installs another build over it (it lands in
`~/.aisquare/remote-dist`), and `--dist` overrides both. To go back to the
bundled page:

```sh
rm -rf ~/.aisquare/remote-dist
```

---

## Unlock, and the screens

Open the link. The page asks for the passphrase: four words, typed with spaces,
dashes or capitals as the phone likes (`Amber river, cedar delta` works). The
browser is then a **device**: it stays signed in for 7 days, and is signed out
after 24 hours without use; unlocking again brings the same device back.

On iPhone and iPad, notifications need the page added to the Home Screen
(below), and **the installed app has its own sign-in**: it unlocks once more and
shows as a second device.

The screens, along the bottom bar:

- **Needs** — one feed across every project of what is waiting on you, most
  urgent first. Each item is a card; the feed is empty when nothing needs you.
- **Projects** — each project with its agents' states and how many items need
  you. Inside one: **Fleet** (every agent and its state), **Board** (newest first,
  with a composer for a note, a decision, a question or a result), **Tasks** and
  **Memory**, both read-only.
- **An agent** — **Live** (its pane, colours included, the cursor where the
  program shows one, with Fit width), **Transcript** (the conversation, wrapped
  to the phone, each turn's time in the phone's own time zone, older pages on
  demand) and **Card** (model, tokens and the explainability verdict). Under Live
  and Transcript sit the input bar and the key pad.
- **Devices** — every device that unlocked, which one is this one, last seen, when
  its sign-in ends. Sign out of this one, or revoke another (a write, so only
  while writes are on).
- **Settings** — notifications for this device, the version, sign out.

The strip at the top shows the connection (green: live), **READ-ONLY** while
writes are off (tap it for the reason), and "off in 23 min" with **Extend 1 h**
when an auto-off is set. When nothing has arrived for 25 seconds, the page greys
what it shows and holds every action until the next update: a phone that slept
must not act on a screen that went stale. It then opens a new connection, as a
connection can die without a word (Wi-Fi giving way to mobile data). Waking the
phone reconnects at once, and from the moment a connection is lost an agent's
Live tab stays grey, its keys and Send held, until its pane has come through
again.
If the machine stops checking what needs you while the link is fine, the feed
greys and says when it last looked.

---

## Writes

Every change is refused with `read_only` until you allow it:

```sh
aisquare remote allow-write on
aisquare remote allow-write off
```

The R panel's **Allow write actions** switch sets the same value. There is one
write switch, the server's own, in `~/.aisquare/remote.json`: the shell and the
panel both set it there, whether Remote is on or off, so they always agree and
starting the TUI never changes it. The page's READ-ONLY sentence names both.

The writes, and the routes they use:

| from the page | route |
| --- | --- |
| type into an agent, or press keys on the pad | `POST api/send-keys` |
| post on the board | `POST api/note` |
| a quick answer on a card | `POST api/needs/answer` |
| tell, stop, restart, switch | `POST api/agent/tell`, `…/stop`, `…/restart`, `…/switch` |
| another hour before auto-off | `POST api/remote/extend` |

Revoking another device from **Devices** (`DELETE api/devices/<id>`) is a write
as well, refused while writes are off. Dismissing a card, signing this device out
and turning notifications on or off change only what you are shown, not the
fleet, and need no write switch. Claiming or finishing a task and switching,
adding or removing a project are write routes of the API (`api/task/claim`,
`api/task/done`, `api/project/switch`, `add`, `remove`) that the page itself
does not offer: Tasks is read-only there.

**Retries are safe, and soon or never.** Every write in the table carries a
`request_id`. If the phone loses the answer (a restart can take 40 seconds, long
enough for a phone to sleep), the page asks again with the same id once it
reconnects, and the server answers from what it recorded instead of doing it
twice, even if writes were switched off meanwhile. A request that never reached
the machine runs when the retry does, and a key pressed long before could
answer a prompt that came up since, so the page sends it again only within 15
seconds of the tap, and never after the phone had to unlock again. Past that it
says the write was not sent again, and still shows the result if the machine had
it after all.

**Every write is audited** in `~/.aisquare/remote-audit.log`, owner-only, one
line each:

```text
2026-10-07T10:12:05+00:00 dev_3fa9c2d1 send-keys coder-auth@prj_8c1e… text=12ch keys=0 enter=True
2026-10-07T10:13:40+00:00 dev_3fa9c2d1 agent/tell tell coder-auth@prj_8c1e… mode=prompt delivered=yes text=14ch "yes, commit it"
```

That is the time, the device, the route and what it did. Typed text is recorded
as a length; a tell keeps its first 120 characters, and a switch the start of the
`reason` it types into the replacement's prompt, because they hand an agent
free-form instructions. A write refused after part of it already reached the
agent is on the trail too, with how it ended: keys typed before tmux failed
(`failed`), an Esc sent before the action stopped short (`refused=<error>`), a
restart that stopped the agent and could not start its replacement
(`failed=<error>`).

---

## Needs you

The server scans every project every 3 seconds and keeps one list of what is
waiting on you. The page shows it as cards; `aisquare remote needs` prints it:

```sh
aisquare remote needs
aisquare --json remote needs
```

| kind | means |
| --- | --- |
| permission | an agent waits for you to allow a tool (a Bash command, an edit), or shows one of Claude Code's own dialogs |
| question | an agent asks you a question with options |
| plan | an agent asks you to approve its plan |
| board_question | the manager asks on the board, or a coder asks you (no `--to`, or `--to user`, `human`, `owner`, `all` or `everyone`), or asks a manager that is not there to answer (stopped, or parked on its usage limit) |
| manager_down | the manager crashed; or, while agents still work and before it reported a result, it was killed or lost (no exit status), or a switch or a restart could not start its replacement. A manager that exits cleanly (`fleet stop`, the phone's Stop, its own `/exit`) is taken to be done |
| crashed | an agent exited with an error in the last hour, or was stopped for a switch or a restart that could not start its replacement, its task unfinished, while no manager runs to handle it (one parked on its usage limit does not count) |
| limited | an agent hit its usage limit |
| lost | an agent's pane is gone |
| fleet_down | tmux is not answering for a project |
| asked | an agent ended its turn with a question in plain text |
| board_result | the manager (or a coder with no manager left) reports a result |
| interrupted | you pressed Esc on an agent, or turned its prompt down, and it waits for you |

A card holds what you must read before answering: the exact command a
permission is for, every question with its options, the plan, the text. What is
too long for it is cut, a permission's command at 2,000 characters and all it
shows at 4 KiB, and the card says so and how long the whole is; a call whose
input is over 16 KiB never reaches the phone, and its card says that instead.
Open the agent to read such a call before you answer it. Under a permission, a
question or a plan, the bottom of the agent's live screen is shown too, so the
real option labels are on screen next to the buttons.

**Quick answers** are the card's buttons: `1`, `2` and No for a permission; one
per option, and Cancel, for a single question; `1` to `3` and Keep planning for a
plan. A quick answer is checked against the agent **as it is now**: if the
prompt has already gone, the card says "No longer needs you" and nothing is
typed. Anything else is answered from the agent's key pad.

The other buttons follow the kind: **Tell** for a question asked in text,
**Reply** on the board, **Switch account** for a usage limit, **Restart** for a
crash. **Dismiss** hides a card for good. A tell or a reply dismisses its card
itself once it was delivered.

---

## Agent actions

From an agent's **Actions…** menu, or from a card:

**Tell** sends one message, typed as a single paste:

| mode | what happens |
| --- | --- |
| Tell (from the menu) | typed into the agent's prompt when it is waiting there; otherwise left as a board note it reads at its next prompt |
| Tell (from a card) | typed now, and refused if the agent is working or showing a dialog of its own |
| Interrupt & tell | one Esc stops what the agent is doing; the message is typed once it is back at its prompt |

When an agent is working, a card's Tell offers **Interrupt & tell** instead.

**Stop**, **Restart** and **Switch account** each open a sheet that says in one
sentence what will happen, and send the agent's name as confirmation. Restart
and switch can take 40 seconds; the page waits, and shows the result even if the
phone slept meanwhile.

**The dialog guard.** Stopping types `/exit` and Enter, and an Enter into an open
dialog would answer it: approve a command, pick an option, accept a plan. So
when the agent shows a prompt, stop, restart and switch are refused with
`dialog_open`, and the sheet offers **Press Esc (No) first**, which dismisses the
prompt and then goes on. For its first few seconds a permission prompt cannot be
told from a tool at work, so they are refused the same way while any tool the
agent called has no result yet; there the Esc also stops a running tool. A
card's Tell and Interrupt & tell refuse a dialog the same way; the menu's plain
Tell types only into an agent waiting at its prompt.

Every action is pinned to the agent you looked at: if a manager restarted or
switched it in the meantime, the action is refused as `stale` rather than
applied to the replacement.

On a phone wide enough for eight keys (412 px is, 390 px is not) the key pad is
one row, `Esc 1 2 3 ⏎ ↑ ↓ More` (⏎ being Enter), with the rest under More. On
a narrower phone More takes the line under the seven, and below 360 px the
eight are two rows of four, More last.
Ctrl-C and Ctrl-D ask first, and a second one within 3 seconds asks again,
because Claude Code exits on it. A second Esc within a second and a half asks
too: two in a row open Claude Code's Rewind selector. The pad and the phone's
keyboard never share the screen. Keys reach an agent one at a time, in the order
they were tapped, and a key shows in the accent colour until the machine has
answered it; one that waits behind a key that did not get through, or for longer
than 15 seconds, is not sent, and the page says so.

---

## Notifications

Settings → **Turn on** asks the browser for permission, subscribes, and tells
the machine. **Send test** checks the whole path. A notification goes out when an
item has been there for two scans in a row, with a delay for kinds that often
clear by themselves (a crash: 30 seconds; a lost pane or a stopped manager: a
minute, since they flash during a restart). Several at once come as one
notification, at most one every 20 seconds per phone; a new one takes the place
of the one still shown and sounds all the same. Tapping it opens the card, at
the address the panel's ngrok announced or `serve --public-url` named; a
`serve` told neither opens the page the phone subscribed from.

The machine also sends: a warning 10 minutes before auto-off ("open to extend
it" while writes are on; with writes off, that the phone cannot extend it), a
goodbye when Remote is turned off, an alert when someone is guessing the
passphrase, and a warning a day before a phone's 7-day sign-in ends.

**What a notification holds.** A title and a line built from fixed sentences
(`coder-auth asks you a question`), with every name cut to 40 plain characters,
the link to the card, and the item ids. Never an excerpt, a command, a
question's text or anything else from the agent. The payload is encrypted to the
browser's own key (RFC 8291), so the push service sees only that a message went.

**Platforms.**

- **iPhone and iPad (iOS 16.4+)**: notifications work only from the page added to
  the Home Screen (Share → Add to Home Screen) and opened from there, and the
  permission must be asked from a tap inside it. The installed app has its own
  sign-in: unlock it once more; it shows as a second device.
- **Android, desktop Chrome, Firefox and Edge** need no install.
- **A changing ngrok URL**: an existing subscription keeps working, and its links
  open the new address once the machine knows it. The new address has no
  sign-in, so the phone unlocks again; the Home Screen app is tied to the old
  address. A static domain avoids all of it.
- **ngrok's browser warning page** (free plan): if notifications cannot be turned
  on, reload once after passing it.

---

## Before you leave the desk

- [ ] A static ngrok domain is set (`AISQUARE_REMOTE_NGROK_URL`), so the link and the phone's sign-in survive a restart.
- [ ] Writes are on, if you want to act and not only watch: `aisquare remote allow-write on`.
- [ ] Auto-off is 120 minutes or Never in the R panel, or writes are on, so the phone can extend it an hour at a time.
- [ ] A test notification arrived on the phone (Settings → Send test).
- [ ] The passphrase is in the phone's password manager.

---

## The security model

- **The token** in the path is the first secret: without it every request is a
  404, so the URL leaks nothing about what is behind it. The page sends no
  referrer, so the token never leaves in a link.
- **The passphrase** is four distinct words from a list of 512 (about 36 bits),
  typed once per browser. Unlocking is limited to 5 attempts a minute per client,
  and to 20 failures in 30 minutes across everyone: past that, new unlocks pause
  (the machine and phones that unlocked before can still unlock, and subscribed
  phones get an alert).
- **Devices** are named by public ids (`dev_3fa9c2d1`); the cookie behind each is
  stored only as a digest, so a copy of `~/.aisquare/remote.json` replays no
  existing sign-in. It does hold the link and the passphrase, though, so keep it
  private (it is owner-only); if it leaked, run
  `aisquare remote regenerate-password --new-link`. A device is signed out after
  24 hours unused and removed after 7 days. A phone whose sign-in lapsed unlocks
  back into the same device, so its notifications carry on.
- **Remote off revokes every device**: turning it off in the panel, or auto-off,
  signs every phone out (after a goodbye notification). Closing the UI or
  stopping `serve` with Ctrl-C does not; expiry bounds them.
- **Auto-off** is enforced by the server itself: past the deadline every request
  is a 404, and within half a minute Remote turns off, phones signed out, even
  on a machine that slept through the deadline. With writes on, a phone can
  extend it an hour at a time, up to 8 hours ahead.
- **Origin**: every write and every live connection must come from the page's own
  origin, so another site cannot use your cookie.
- **Where a notification leads** is only the address the panel's ngrok announced,
  or `--public-url` named: never what a request or ngrok's local API says, since
  anyone on the machine can answer on that API's port before your ngrok does.
- **ngrok's inspector is off.** Left on, ngrok keeps every request and answer on
  its local web interface (`127.0.0.1:4040`), which asks for no password: the
  passphrase you unlock with, every device's cookie, the token and the
  transcripts, readable by any user of the machine. The panel starts ngrok with
  `--inspect=false`; start yours with it too.
- **Keys**: the pad sends key names from a fixed list (no `;`, nothing that
  tmux reads as a command); typed text travels as literal text, never as keys.
  Typed text may hold no ASCII control character other than a tab or a
  newline; a tell and a note, which reach a pane only inside a paste, a carriage
  return as well (a finished task's note is a note, and an agent's fresh
  replacement is handed its newest notes); and a switch's `reason` is one line
  with no control character at all. The pad sends Esc, Ctrl-C, Enter and its
  other control keys by name (a carriage return typed is the Enter key itself).
- **Caps**: 64 KiB per request, 2 048 characters per keystroke message, 8 000
  per note or tell and 200 for whom a note is to, 4 live connections per device.
  A device turns its notifications on or off at most 6 times a minute and sends
  one test every 10 seconds; a subscription sent again unchanged, or an
  unsubscribe with nothing to remove, writes no audit line.

From the machine:

```sh
aisquare remote status
aisquare remote revoke dev_3fa9c2d1
aisquare remote revoke --all
aisquare remote regenerate-password --new-link
```

`status` lists every device (`--json` too) and any lockout. `revoke --all` signs
every device out but keeps Remote on. `regenerate-password` makes a new
passphrase and signs every device out; with `--new-link` it also makes a new
token, so a leaked link stops working everywhere. The TUI shows the new link
after Remote is turned off and on. The link `status` and `--new-link` print is
for port 8750: when `serve` runs on another, give them its `--port` too. An
exported `AISQUARE_REMOTE_PORT` sets the port for all of them, and for the R
panel, whose server and ngrok use it as well.

---

## HTTP and WebSocket reference

Everything is under `/r/<token>/`. `api/*` needs the device cookie (`asq_remote`,
from `POST api/unlock`) except unlock itself; every non-GET request needs an
`Origin` header equal to the page's origin; bodies are JSON objects of at most
64 KiB. A refusal is always `{"error": "<code>", "message": "<sentence>"}`.

| method | path | what |
| --- | --- | --- |
| GET | `/` | the page |
| POST | `api/unlock` | `{"password"}` → the device cookie |
| GET | `api/remote` | `{allow_write, auto_off_at, version}` |
| POST | `api/remote/extend` | another hour before auto-off |
| GET | `api/projects`, `api/fleet`, `api/board`, `api/tasks`, `api/memory` | what `aisquare --json` prints for each (the board with its newest 200 events, not 5), `?project=` for one project |
| GET | `api/panes/<agent>`, `api/transcript/<agent>`, `api/explainability/<agent>` | one agent's screen, conversation (`?width=`, `?before=`) and card |
| GET | `api/needs` | `{"items", "scanned_at"}` |
| POST | `api/needs/answer`, `api/needs/dismiss` | a quick answer; hide a card |
| GET, POST, DELETE | `api/push`, `api/push/subscribe`, `api/push/subscription`, `api/push/test` | notifications for this device |
| GET, DELETE | `api/devices`, `api/devices/<id>` | the devices; sign out (own id), or revoke another (a write) |
| GET | `api/actions/recent` | this device's recent writes and how they ended |
| POST | `api/send-keys`, `api/note`, `api/agent/{tell,stop,restart,switch}`, `api/task/…`, `api/project/…` | the writes |
| WS | `ws` | the live stream |

The stream sends `{"type", "payload", "ts"}` frames: `fleet` and `remote` when they
change, `board` too once asked for (`{"subscribe_board": "<id>"}`, `false` to stop;
the board's events and the sessions they name), `needs_you`, `action` (this device's
write results), a `heartbeat` every 10 seconds, and `pane` for each pane the page
subscribed to (`{"subscribe": "<agent>", "project": "<id>"}`). It closes with 4401
for a device that is no longer signed in, 4409 when the same device opened a fifth
connection, and 4410 when Remote is turned off.

With curl, unlock once and keep the cookie:

```sh
BASE=http://127.0.0.1:8750/r/<token>
curl -c jar -H "Origin: http://127.0.0.1:8750" -H "content-type: application/json" \
  -d '{"password": "amber-birch-cedar-delta"}' "$BASE/api/unlock"
curl -b jar "$BASE/api/needs"
curl -b jar -H "Origin: http://127.0.0.1:8750" -H "content-type: application/json" \
  -d '{"agent": "coder-auth", "keys": ["Escape"], "request_id": "esc-1"}' "$BASE/api/send-keys"
```

A write's `request_id` is optional. Sent again with the same request, it is
answered with what the first one did instead of running twice; give every other
write an id of its own, since for 15 minutes an id sent with another endpoint or
body is refused with `request_id_reused`.

The code is `src/aisquare/services/remote_server.py` (the server and its gates)
and `src/aisquare/services/remote_page.py` (the bundled page, whose files are in
`src/aisquare/web/remote/`); `tests/test_remote_page.py` holds the page to the
rules above.

---

## Troubleshooting

**The phone shows ngrok's warning page, or notifications will not turn on.**
The free plan shows a warning before a tunnel's first page. Pass it, then reload
once: the page sends the header that skips it on every request it makes.

**Every write says `bad_origin`.** Something between the phone and the server
rewrote the `Host` header; ngrok's `--host-header=rewrite` does exactly that.
Start ngrok without it (the R panel never uses it).

**`serve`, or the R panel, says another Remote is on.** One `~/.aisquare` serves
one Remote: the fleet UI's panel, or a `serve` in another shell, has it. Turn that
one off, or use it. Two would share one link, one passphrase, one auto-off and
one list of phones, and either going off would sign the other's phones out. One
turned off while a phone's restart or switch was still running keeps the home
until that is done, since its notifications go on until then; a restart or
switch can take 40 seconds.

**`serve` says the port is in use.** Something else took 8750. Pass `--port` and
give ngrok (and `status`) the same port.

**ngrok says `--url` is an unknown flag.** That ngrok is too old for static
domains; run `ngrok update`.

**The page is a build you installed, and you want the bundled one back.**
Remove what `install-page` installed: `rm -rf ~/.aisquare/remote-dist`.

**"Remote is off on the machine, or the link changed".** Remote was turned off,
auto-off passed, ngrok came back on a new address, or
`regenerate-password --new-link` replaced the link (a phone still on the old one
is told so when it tries to unlock). Turn it on again, or open the link the
machine shows now.
