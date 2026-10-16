# Nightly real-user E2E

The suite uses aisquare the way a person does, and keeps the evidence. On a fresh
machine it:

- **types the README one-liner into a real terminal** and answers the installer's
  last question;
- goes through **Welcome** to "Your fleet is up";
- **clicks into an agent's pane** and types to it;
- opens the **Doctor** and **Accounts** pages.

Every step ends with a **real screenshot of the terminal window**, the same screen as
text, and the checks a user would make. The workflow publishes all of that as a
gallery and a PR-ready summary.

| Real | Stand-in (for now) |
|---|---|
| `install.sh` typed as a one-liner; the wheel or PyPI release it installs; the "Open the fleet UI now?" hand-off | the **agent**: `e2e/agent/claude` runs where Claude Code would. No model is called; it echoes what it is sent, so the suite sees keystrokes arrive |
| `asq` in a real PTY (ttyd), drawn by a real terminal emulator (xterm.js 6, the engine behind VS Code's terminal) in Chromium, with real keys and mouse | |
| Welcome, hooks, spawns, worktrees, panes, the fleet's tmux server, the doctor, the Accounts page | |

Not used anywhere: Textual's test pilot, fake tmux servers, re-rendered screenshots,
or in-process imports of aisquare.

## Run it on your machine

```sh
brew install ttyd                      # Linux: e2e/ci/install-ttyd.sh
uv venv --python 3.13 e2e/.venv
uv pip install --python e2e/.venv/bin/python --require-hashes -r e2e/requirements.txt
e2e/.venv/bin/python -m playwright install chromium
e2e/.venv/bin/python -m pytest -c e2e/pytest.ini e2e/journeys      # pass the path explicitly
```

- **Scratch home.** Each run installs aisquare into a fresh scratch HOME in your temp
  directory. Your own `~/.aisquare`, `~/.claude` and `~/.local/bin` are never touched:
  the harness refuses to run with your real home (`e2e/runner/machine.py`).
- **Watch it.** Set `E2E_HEADED=1` to see Chromium drive the terminal.
- **Other options:**
  - `E2E_TRACK=release` installs the PyPI release with the real README URL instead of
    this tree's wheel.
  - `E2E_OUT=…` moves the output directory.

Results go to `out/e2e/local-main/`:

```
run.json                      every journey, step, check and evidence path (e2e/contracts.py)
shots/<J>/<step>.png          the terminal window at the end of each step
text/<J>/<step>.txt           the same screen as text, read from the emulator's buffer
video/core.webm               the whole session, as it happened
logs/                         ttyd, `aisquare --json doctor`, `aisquare --json fleet ls`
report/gallery.html           open this one
report/summary.md             what a PR comment shows
junit.xml
```

The report is built by `python -m e2e.report.build out/e2e/local-main`.

## In CI

- **Where:** `.github/workflows/e2e.yml` runs nightly and on demand, and on PRs labelled
  `e2e`.
- **The machine:** the user machine is a non-root `e2e` account (`e2e/ci/setup-user.sh`)
  with nothing preinstalled. The installer has to fetch uv, tmux, gh and Node itself,
  because that is part of what is tested.
- **Locally:** `e2e/ci/act-run.sh` runs the same workflow with
  [act](https://github.com/nektos/act).

## How it works (one paragraph)

`e2e/runner/terminal.py` starts **ttyd** on a loopback port. ttyd runs the user's login
shell from a clean `env -i`. The runner then opens our own client page
(`e2e/terminal/client/`) in Playwright's Chromium.

- **The client page:** it uses xterm.js 6, vendored and checked against
  `vendor/SHA256SUMS`. It is instrumented: it counts DEC 2026 synchronized-output frames,
  which Textual uses once the terminal reports support. It also reads the shell's OSC 133
  prompt and exit-code marks, and exposes the screen buffer.
- **Waiting:** a step waits for text on that buffer, and then for the screen to stop
  changing.
- **Screenshots:** each one is taken only after that settle, and retaken if the text moved
  while it was being captured. So a PNG always shows the screen its checks read.

## Reading a red run

Open `report/gallery.html` from the artifact. The failed step's card shows:

- the screen the user was looking at;
- the check that failed;
- for timeouts, the text the step was waiting for, set against what was actually there.

Then open `video/core.webm` to watch the steps leading up to it.

The verdict classes are the QA team's release-gate ones:

| Verdict | Meaning |
|---|---|
| `RC_FAILED` | the product broke a user-visible promise |
| `ENV_FAILED` | the machine or network failed |
| `GATE_ERROR` | the harness itself failed, or nothing ran |

See `PLAN.md` for the full design and what comes next.
