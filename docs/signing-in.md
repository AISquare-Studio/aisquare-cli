# Signing in

`aisquare login` connects this machine to your AISquare account. It is optional:
everything local (memory, teams, the board) works without it. Sign in when a
command needs to act as you on the AISquare API.

## How it works

The CLI uses the OAuth 2.0 device flow, the same mechanism as `gh auth login`.
The terminal shows a short code and a link. You open the link, check that the
page shows the same code, and press Authorize. The terminal notices within a few
seconds and stores a session token in `~/.aisquare/credentials` (readable by
your user only). Nothing else is written; no password ever touches the CLI.

```sh
aisquare login
```

```text
! First, note your one-time code: WDJB-MJHT
  Check that the browser shows the same code before you authorize.
  Open this URL to continue in your browser:
  https://home.aisquare.studio/cli?code=WDJB-MJHT
  You have 15 minutes to approve this request.

  Opening your browser at home.aisquare.studio...
⠋ Waiting for approval in the browser · next check in 3s · code expires in 14:21
  Press Esc or Ctrl-C to cancel

✓ Signed in as you@example.com (your-laptop)
  This session expires Dec 2, 2026. Sign out any time with aisquare logout.
```

The session lasts 90 days and stops working after 30 days without use. When it
expires or is revoked, the next command that needs it tells you to run
`aisquare login` again. There is no silent refresh.

## Over SSH, in a container, or without a browser

The link works from any device, including your phone. When the CLI cannot open a
browser on this machine it says so and keeps waiting; copy the link and open it
anywhere. To never attempt a browser:

```sh
aisquare login --no-browser
```

Setting `BROWSER` in your shell picks the browser; the values `echo`, `true`
and `:` mean "print the link only".

## Checking and using the session

```sh
aisquare whoami
aisquare auth status --live
aisquare auth token
```

`whoami` answers from the file without touching the network. `auth status
--live` also asks the server whether the session still works. `auth token`
prints the token for scripts; it grants full access to your account, so treat
it like a password. Every command accepts `--json` for machine-readable output.

For CI and other machines without a browser, set `AISQUARE_TOKEN` in the
environment. It is used read-only and wins over the file; `aisquare login`
refuses to run while it is set, so a shell cannot end up with two identities.

To sign in with a token obtained elsewhere instead of the browser:

```sh
aisquare login --with-token < token.txt
```

## Another environment

`--api-url` points a sign-in at a different server, for example staging. The
session remembers which server it belongs to, and a command aimed at a different
server fails with `api_url_mismatch` rather than sending the token to the wrong
place. `AISQUARE_API_URL` sets the same thing from the environment.

```sh
aisquare login --api-url https://stg-api.aisquare.studio
```

## Signing out

```sh
aisquare logout
```

This revokes the session on the server and removes it from this machine. If the
server cannot be reached, the local copy is still removed and the message tells
you to revoke the session from Settings > Security in the web app, where every
signed-in device is listed.

## Security notes

- Approve a code only when you ran `aisquare login` yourself, moments ago, and
  the code on the page matches the one in your terminal. Anyone who gets a code
  approved by you gets a session as you.
- The token never appears in the terminal unless you ask for it with
  `aisquare auth token`, and the CLI's redaction rules scrub it from anything
  it ships to the explainability gateway.
- The CLI refuses to send credentials over plain `http` to anything but
  `localhost`.
