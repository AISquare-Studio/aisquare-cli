# Design sketch: a fleet across machines (#293)

**Status: postponed.** Not in any current train. Until it ships, the workaround
is [docs/runbooks/multi-machine-ssh.md](../runbooks/multi-machine-ssh.md): a
manager drives another box's fleet over SSH.

## The ask

One fleet across machines. A captain on one machine tasks managers on others;
tests and Docker run on one box while coding runs on another.

## What exists (2026-10-23)

- No remote-host fleet, no SSH spawn, no cross-machine messaging.
- `aisquare serve --bind <LAN>` lets a remote Claude client join one board over
  MCP. Its tools read and write the board (`team_board`, `task_add`,
  `task_next`, `task_update`, `note_add`, `team_log`, `verify`, `signal`,
  `recall`, in `services/mcp_server.py`), but they can neither spawn nor steer an
  agent.
- `aisquare remote`'s authenticated HTTP API acts on agents (`agent/tell`,
  `stop`, `restart`, `switch`, in `services/remote_actions.py`). It has no spawn,
  and it is built for a phone.

## Recommended first version: an SSH transport for the fleet verbs

- **Hosts in config.** `[fleet.hosts.<name>] ssh = "boxb"`, an `ssh_config` host,
  so `ControlMaster` and keys stay the operator's.
- **`fleet spawn --host boxb`** runs the same CLI on B over that connection. The
  row is recorded locally with a `host` column, which needs a store migration
  (a schema number taken after the reserved ones). `fleet ls`, `tell`, `stop`,
  `restart` and `switch` follow the row's host.
- **Panes.** asq renders a remote pane through #272's control-mode client over
  `ssh boxb tmux -L asq -C attach …`, so remote panes look like local ones.
- **Events back.** Board events reach A by pulling `team log --since-seq` from B
  on the existing refresh, or through P2's parent-manager routing (#199).
- **Trust.** The same model as the runbook. A key that can run `aisquare` on B
  can run any command as B's user (`fleet spawn` forwards arguments to `claude`;
  `config set` and `team bind --arg` change launches), so the dedicated user on B
  is the boundary; a forced command only removes the plain shell. A `--host`
  transport should not pretend otherwise.

## Alternatives considered

- **Extend `aisquare remote`'s HTTP API** with spawn and a machine token. It
  already authenticates and acts on agents, but it is built for one phone, and
  every verb would need a second, HTTP form.
- **Extend the MCP `serve` tools** with spawn and steer. That puts fleet control
  inside an agent's tool list on a LAN socket, which is a wider surface than an
  SSH key the operator already manages.

## Open questions

- Should the remote rows share A's board, or keep B's and mirror results?
- What does `fleet shutdown` mean across hosts: this host, or every host?
- Is the per-host migration worth it before #199's routing lands?
