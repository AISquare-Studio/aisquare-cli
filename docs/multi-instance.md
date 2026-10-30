# Running more than one asq on one home

You can run asq at the Mac and, at the same time, over SSH into that same Mac. Both use the same
home (`~/.aisquare`) and the same fleet. This page explains what the two share, what only one of
them holds at a time, and what asq does to keep two releases from running against one home.

## Seeing them

- **The chip.** While more than one asq is open on the home, a line under the sidebar's status
  line says how many there are and where each runs. It marks this asq's place with `(here)`:

  ```text
  2 asq · Mac + ssh (here)
  ```

  `Mac` (`local` on Linux) is an asq at this machine, `ssh` is one reached from another machine,
  and a host name is an asq on another machine that shares the home. Hover the chip to see each
  asq's version, host, pid, terminal and start time. With one asq open, the chip is hidden.
- **The command.** `aisquare instances` lists the live asqs: their kind, host, pid, version, when
  each started and how long ago it last checked in. `--json` gives the same list as an object,
  `{"instances": [...]}`. Each entry is the asq's record plus `heartbeat_age_s` and `this`.

  ```sh
  aisquare instances
  aisquare instances --json
  ```

- **The doctor.** `aisquare doctor` (and asq's Doctor page) has an `asq instances` row. It is ok
  when every live asq runs this aisquare's release and store schema, and warns when one doesn't.

Every asq writes a small record to `~/.aisquare/instances/` when it starts, refreshes it every
5 seconds, and removes it when it quits. A crashed asq's record is ignored and cleaned up by the
next asq that starts. Looking (the chip, the command, the doctor) never changes the folder.

## What they share

Every asq on a home works on the same things:

- the store (`context.db`), the config, the boards, the projects and their groups;
- the fleet's tmux server, so every agent is visible and drivable from each asq;
- the Claude Code accounts, and the usage readings recorded for them.

## What only one asq holds

Some things can only have one holder. Whichever asq takes it first keeps it until it quits:

- **Remote.** Only one process serves Remote for a home. While another asq serves it, this asq's
  Remote panel says so instead of starting a second server.
- **The captain's ui socket.** The captain's `ui` actions (open a dialog, select a row) go to the
  first asq that listens on the socket. Another asq that starts later leaves it alone.
- **A shared pane's size.** When two asqs show the same agent's pane, the asq you last typed or
  clicked in sets its size. The other one shows a short hint instead of resizing it back.
- **Usage over SSH.** An asq over SSH can't read the login Keychain, so it doesn't fetch account
  usage itself. It shows the readings that the asq at the Mac recorded, marked with where they
  came from and how old they are. To refresh them, open asq on the Mac, or run
  `security unlock-keychain` in the SSH session.
- The transcript sweep for *Usage & stats*, and `aisquare watch`, each run in one process at a
  time, as before.

## One release per home: the version gate and the quit offer

Two releases on one home would race on the store's migrations. So when asq starts, it first reads
the records of the other live asqs, before it opens the store or touches tmux:

- If every live asq runs the same release and store schema, it starts as usual.
- If one runs another release, the new asq starts nothing and asks:

  ```text
  Quit the other asq? [y/N]
  ```

  With `y`, the other asq quits the way `q` would. Its terminal says which asq asked it to. The new
  one then starts. With `n`, Ctrl-C or the end of input, the new asq doesn't start, and it names
  the other asq so you can quit it yourself (`q` in it). Without a terminal to ask in, it gives
  that same refusal.

`aisquare upgrade` and `aisquare uninstall` refuse while another asq runs on the home, and asq's
own Update and Uninstall buttons do too. Quit the other asqs first.
