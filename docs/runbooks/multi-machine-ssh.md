# Runbook: a fleet across two machines, over SSH

A fleet cannot span machines yet. That feature is **postponed**; the design is
in [docs/plans/multi-machine-fleet.md](../plans/multi-machine-fleet.md). Until it
ships, this runbook covers the workaround that works today: a manager on box A
drives a fleet on box B through SSH, with aisquare installed on both. Use it
for a coder that needs B's Docker or GPUs, tests that only run on B, or work
split across two machines' Claude accounts.

Box A is where you and the manager run. Box B is the other machine, where its
own agents run in its own `asq`.

## 1. Set up box B

Install aisquare with the one-line installer from [Install](../install.md),
then, in the repository on B:

```sh
aisquare agents connect claude-code
aisquare project onboard .
aisquare accounts add
```

Run `aisquare accounts add` once per Claude account B should use. Check B with
`aisquare doctor`. Note the project's codename from `aisquare project list`: the
commands below name it, because over SSH there is no current directory to infer
it from.

## 2. Connect A to B

Key-based SSH, with one connection shared by every command the manager runs, so
each `fleet` call does not pay a new handshake. In `~/.ssh/config` on A:

```text
Host boxb
    HostName boxb.example.lan
    User fleet
    IdentityFile ~/.ssh/fleet_boxb
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
    ServerAliveInterval 15
```

`ServerAliveInterval 15` with the default `ServerAliveCountMax 3` drops a dead
link in about 45 seconds instead of hanging the manager's turn.

Non-interactive SSH may not have `~/.local/bin` on `PATH`. Name the binary by
its absolute path, or wrap the command in `bash -lc '…'`.

## 3. Brief the manager

Give the manager the commands it may run against B, in its persona or as a
`CLAUDE.md` line in the project on A. `<codename>` is B's project codename:

```text
ssh boxb -- ~/.local/bin/aisquare --json fleet ls --project <codename>
ssh boxb -- ~/.local/bin/aisquare fleet spawn coder --project <codename> --label coder-docker --task 12 --prompt "<brief>"
ssh boxb -- ~/.local/bin/aisquare team log --since-seq <N> --json
ssh boxb -- ~/.local/bin/aisquare fleet tell coder-docker --project <codename> "<steer>"
```

`team log --since-seq <N>` returns only the board events past the last `seq`
the manager saw, so it can poll B's board without rereading it. A task on B is
B's: B's board and A's board are separate, so the manager reads B's results
from B's log and records what matters on its own board.

## 4. Watch box B

B's agents run in B's `asq`. To see them, open it inside tmux on B, so it keeps
running when the connection drops:

```text
ssh -t boxb tmux new -As asq asq
```

On a flaky link or a laptop that sleeps, use [mosh](https://mosh.org/) with the
same tmux session on B. Mosh survives roaming and sleep but keeps no
scrollback, which tmux on B provides.

## 5. Security: what B gives A

B trusts A's key completely. A manager with a shell on B can do anything B's user
can, and so can anyone who takes that key. Two narrowings, best used together:

1. **A dedicated user on B** (`fleet` above) that owns only the repository and
   B's aisquare home. That user's permissions are the real boundary.
2. **A key restricted to aisquare commands.** In that user's
   `~/.ssh/authorized_keys` on B, the fleet key gets a forced command and
   `restrict` (no port forwarding, no agent forwarding, no terminal):

   ```text
   restrict,command="/home/fleet/bin/aisquare-only" ssh-ed25519 AAAA… manager@boxa
   ```

   `/home/fleet/bin/aisquare-only` (executable, owned by root or by `fleet`
   with no write access for anyone else) runs `aisquare …` and nothing else. It
   splits the command with `shlex`, which honours quotes and never runs what
   it reads, and refuses `--bin`, which would start another program in an agent's
   window:

   ```text
   #!/usr/bin/env python3
   """Forced command for the fleet key: run `aisquare …` and nothing else."""
   import os, shlex, sys
   from pathlib import Path

   words = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
   if not words or Path(words[0]).name != "aisquare":
       sys.exit("refused: this key runs aisquare commands only")
   if any(word == "--bin" or word.startswith("--bin=") for word in words):
       sys.exit("refused: --bin is not allowed over this key")
   binary = str(Path.home() / ".local/bin/aisquare")
   os.execv(binary, [binary, *words[1:]])
   ```

   With `restrict` there is no terminal, so step 4's `ssh -t` needs another
   key: your own, not the manager's.

A forced command narrows the **shell**, not the **agents**. A coder spawned on B
runs with its role's permission mode, and `fleet tell` types whatever the
manager sends into it. So the agents on B can still do what their role
allows. Keep B's roles' permission modes and the dedicated user's access as
tight as the work allows.

## When this is retired

When the multi-machine fleet ships (`fleet spawn --host`), the same verbs run
against B from A's own `asq`, and this runbook goes. Until then it is the
supported way.
