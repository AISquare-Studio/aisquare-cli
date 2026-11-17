# Running more than one rlx on one home

You can run rlx at the Mac and, at the same time, over SSH into that same Mac. Both use the same
home (`~/.aisquare`) and the same fleet. This page explains what the two share, what only one of
them holds at a time, and what rlx does to keep two releases from running against one home.
Below, each running rlx on a home is an instance, and each one's messages call it rlx.

## Seeing them

- **The chip.** While more than one rlx is open on the home, a line under the sidebar's status
  line says how many there are and where each runs. It marks this rlx's place with `(here)`:

  ```text
  2 rlx · Mac + ssh (here)
  ```

  `Mac` (`local` on Linux) is an rlx at this machine, `ssh` is one reached from another machine,
  and a host name is an rlx on another machine that shares the home. Hover the chip to see each
  rlx's version, host, pid, terminal and start time. With one rlx open, the chip is hidden.
- **The command.** `rlx instances` lists the live rlx instances: their kind, host, pid,
  version, when each started and how long ago it last checked in. `--json` gives the same list
  as an object, `{"instances": [...]}`. Each entry is the rlx's record plus `heartbeat_age_s`
  and `this`.

  ```sh
  rlx instances
  rlx instances --json
  ```

- **The doctor.** `rlx doctor` (and rlx's Doctor page) has an `asq instances` row. It is ok
  when every live rlx runs this release and store schema, and warns when one doesn't.

Every rlx writes a small record to `~/.aisquare/instances/` when it starts, refreshes it every
5 seconds, and removes it when it quits. A crashed rlx's record is ignored at once, and deleted
by the next rlx that starts or by the next heartbeat of any rlx still running. Looking (the chip,
the command, the doctor) never changes the folder.

## What they share

Every rlx on a home works on the same things:

- the store (`context.db`), the config, the boards, the projects and their groups;
- the fleet's tmux server, so every agent is visible and drivable from each rlx;
- the Claude Code accounts, and the usage readings recorded for them.

## What only one rlx holds

Some things can only have one holder. Whichever rlx takes it first keeps it until it quits:

- **Remote.** Only one process serves Remote for a home. While another rlx serves it, this rlx's
  Remote panel says so instead of starting a second server.
- **The captain's ui socket.** The captain's `ui` actions (open a dialog, select a row) go to the
  first rlx that listens on the socket. Another rlx that starts later leaves it alone.
- **A shared pane's size.** When two rlx instances show the same agent's pane, the rlx you last
  typed, pasted or clicked in sets its size (a drag to select text doesn't count). The other one
  shows `[sized by the other rlx (ssh)]` (or `(Mac)`) in the pane's corner instead of resizing it
  back, and sizes the pane again once that rlx quits.
- **Usage over SSH.** An rlx over SSH can't read the login Keychain, so it doesn't fetch account
  usage itself. It shows the readings that the rlx at the Mac recorded for the same login, marked
  as recorded elsewhere, never another login's. A sign-out or an expired login that the Mac saw
  shows the same way over SSH. To refresh the readings, open rlx on the Mac, or run
  `security unlock-keychain` in the SSH session.
- The transcript sweep for *Usage & stats*, and `rlx watch`, each run in one process at a
  time, as before.

## SSH and the fleet's tmux server (macOS)

On macOS a tmux server belongs to the login that started it, and so does every pane it opens.
Agents in a server started from an SSH login can't read their Claude Code login from the Keychain.
So rlx refuses to start the fleet's tmux server from an SSH login for a Claude Code agent:

```text
not started: start rlx on the Mac first: agents started from an SSH login can't read the Keychain
```

Start the first agent in rlx at the Mac. After that, a fleet server is running and agents spawn
into it from SSH as usual. Only starting the server is refused. A server that the Mac started
stays the Mac's, whoever attaches to it later.

A Codex agent keeps its login in a file in its home (`auth.json`), so it can start the server from
SSH. rlx marks a server started that way, and opens no Claude Code agent in it, from SSH or at the
Mac: `rlx fleet shutdown --all` ends it, and the Mac starts the next one.

## One release per home: the version gate and the quit offer

Two releases on one home would race on the store's migrations. So when rlx starts, it first reads
the records of the other live rlx instances, before it opens the store or touches tmux:

- If every live rlx runs the same release and store schema, it starts as usual.
- If one runs another release, the new rlx starts nothing and asks:

  ```text
  Quit the other rlx? [y/N]
  ```

  With `y`, the other rlx quits the way `q` would. Its terminal says which rlx asked it to. The new
  one then starts. With `n`, Ctrl-C or the end of input, the new rlx doesn't start, and it names
  the other rlx so you can quit it yourself (`q` in it). Without a terminal to ask in, it gives
  that same refusal. If the other rlx hasn't quit within 15 seconds (it may be finishing a
  restart of the fleet), the new rlx says so and doesn't start.

Some refusals come without a question, because there is nothing this rlx can ask:

- **An rlx that can't be asked to quit.** Its record has no quit channel. The new rlx names its pid
  and says what `kill <pid>` loses against quitting it in its own terminal. It never kills it.
- **An rlx from before the registry** (0.9.1 and older), which writes no record. The new rlx finds
  it with one `ps`, names its pid, terminal and command, and says what `kill <pid>` loses. It never
  kills it. An rlx started in the last 10 seconds, or one on another home, isn't counted.
- **A record this release can't read.** Most likely a later release's rlx. The new rlx names the
  record's file. A record whose rlx is gone (no lock held) is ignored once it is a minute old.
- **Another rlx is starting at the same moment.** If it has held the home's gate for over
  2 seconds, the new rlx doesn't start unchecked: it says so, and you start it again a moment
  later.

The one case rlx starts without the check is a home it can't write to. It says so once, and the
other rlx instances won't see it.

`rlx upgrade` and `rlx uninstall` refuse while another rlx runs on the home (rlx instances of
0.9.2 or later; quit a 0.9.1 rlx yourself), and rlx's own Update and Uninstall buttons do too.
Quit the other rlx instances first.
