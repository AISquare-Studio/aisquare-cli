# The AISquare platform (optional)

It's a single CLI, local-first, backed by one SQLite file. No daemon, no
account, no cloud dependency. Sign in with `aisquare login` only when a command
needs to act as you on AISquare (see [Signing in](signing-in.md)).

## Explainability

**Optionally, on top of memory or orchestration:** send your sessions to an
[AISquare Explainability](https://aisquare.studio) workspace, so every session
becomes a Run you can read back — prompts, tool calls, tokens and cost, plus your
own prompts and board events. Off unless you ask for it, and it never blocks a
launch: if anything in the path is down the session starts untraced and says so.
See **[Connecting your agents to Explainability](connecting-your-agents-to-explainability.md)**.

## Collective Intelligence test bed (experimental, off by default)

An opt-in experiment: when you submit a prompt, aisquare can ask a Collective
Intelligence server whether this workspace already knows something relevant and
put it in front of the agent **before** it starts exploring. The hypothesis is
that an agent which starts better informed explores less.

**It is off unless you turn it on**, and off costs nothing — no request, no
connection, no measurable latency. Nothing below runs for a normal install.

```sh
pip install 'aisquare-cli[experiment]'   # the extra adds no dependencies today
export AISQUARE_CI=1
export AISQUARE_CI_URL=https://…          # the server's base URL
export AISQUARE_CI_KEY=…                  # the experiment token
export AISQUARE_CI_RUN=run_…              # the run the controller published
aisquare doctor                           # the switch, the endpoint, and the run's descriptor
aisquare metrics show                     # what was recorded, per turn, for this project
```

| Knob | Default | Meaning |
| --- | --- | --- |
| `AISQUARE_CI` | off | Master switch; overrides `[experiment].enabled` both ways. **Any unrecognised value is off** |
| `AISQUARE_CI_URL` | unset | The server's base URL, `http(s)://` only. Or `[experiment].url` |
| `AISQUARE_CI_KEY` | unset | Bearer token. **Environment only** — never read from `config.toml` |
| `AISQUARE_CI_RUN` | unset | The `run_…` whose delivery descriptor drives this machine. Or `[experiment].run`. No run ⇒ no calls |
| `AISQUARE_CI_DELIVERY_OVERRIDE` | unset | **Staging only, dated.** Stands in for the server's delivery list while a run's descriptor still says `direct_api`; ignored otherwise. Every row it produces says `override` and measures nothing. See `docs/ci-live-wiring-handoff.md` |

Each prompt against a dirty working tree also keeps a snapshot of that tree
alive for replay, as a commit object behind a ref under `refs/aisquare/wip/`
(outside branches and tags, never pushed by a default refspec). Refs older than
seven days are dropped the next time a snapshot is taken.

What the CLI does with a run is decided by the server, not by a flag here (the
one dated exception is the staging override above, which applies only to a
`direct_api`-only descriptor and marks every row it touches). At
session start it fetches the run's **delivery descriptor** (cached until it
expires) and honours only what that lists: which hooks call the server, where,
under what ceiling, and whether the `collective_intelligence_recall` tool is
exposed in `aisquare serve`. The descriptor names no architecture or arm, so the
CLI cannot know which arm it is running — by design.

Four things worth knowing before you enable it:

- **The prompt hook is synchronous.** It waits up to the descriptor's ceiling
  (60 s today) for a slow server, as wall clock — a server dribbling bytes cannot
  hold it past that — and every breach is recorded.
- **Retrieved material is framed as candidate reference, not fact**, inside a
  delimited region the payload cannot close, capped at 16 384 characters, with the caveat
  repeated after it, so a bad retrieval is visible in the transcript rather than
  silently absorbed. `aisquare why` names what was shown.
- **Every turn is recorded whether or not the server answered**, with *why* it
  did not kept apart from what the server said. A switched-off machine, a
  timeout and a server with nothing to add are three different rows.
- **What leaves the machine:** the prompt (scrubbed at the configured `redaction`
  level), a `project_ref` selector, and a git object id of the working tree —
  kept under `refs/aisquare/wip/<trace_id>` so a turn can be replayed later;
  untracked files are not in it. Nothing about scope, and no credentials.

Token counts are **not** recorded yet — hook payloads do not carry them — so
`metrics show` says plainly that token savings cannot be read from it. The wire
contract and the CLI's standing assumptions are in
[`docs/ci-contract.md`](ci-contract.md); the server-side seam is
[`docs/ci-integration-handoff.md`](ci-integration-handoff.md).
