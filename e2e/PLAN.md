# Nightly real-user E2E: working brief for the fleet that builds it

> **Status:** working brief. Move the design into `docs/nightly-e2e.md` and drop this file before any PR.
> **Branch:** `feat/e2e-nightly`. Nothing leaves this machine without the human's go-ahead.

## 0. Status (updated 2026-10-16)
A first slice is built and green locally (macOS):

- **Harness:** a TerminalSession driving xterm.js 6 on a ttyd PTY; the Machine (scratch HOME and
  CI user modes); the evidence recorder; `run.json` (`e2e/contracts.py`).
- **Journeys:** J01 install via the one-liner, J02 Welcome → "Your fleet is up", J04 agent pane,
  J06 Doctor and Accounts.
- **Agent:** `e2e/agent/claude`, a plain stand-in (echo).
- **Being wired now:** the report/gallery (`e2e/report`), `.github/workflows/e2e.yml` and the act
  runner (`e2e/ci`).

**Still open for the fleet**, by package below:
- the scripted agent (§6, hooks, tasks, contracts);
- J03, J05, J07–J10 and the watchlist;
- the container machine mode;
- macOS CI legs, baselines and diffs, change-only Slack, review.

## 1. Goal
A nightly job in this repo that uses aisquare **the way a person does** and keeps the evidence. On a fresh Linux
machine and a fresh Mac it:
- installs it from the README one-liner;
- drives `asq` and the CLI in a **real terminal emulator** with real keystrokes and mouse;
- takes **real pixel screenshots**, video and traces of every step;
- asserts what a user would expect;
- publishes a gallery and a `verdict.json`.

**Now:** no Claude token and no model usage. Everything aisquare does is real; only the agent's *thinking* is scripted.
**Later:** real Claude Code drops into the same journeys behind a switch.

**Non-goals:** replacing unit tests, pixel-perfect gating (the UI changes daily), testing Claude itself.

## 2. Ground rules (every agent)
1. Read `CONTRIBUTING.md` first.
   - Every change comes with a test that fails without it; prove that it can fail.
   - Strict mypy and ruff.
   - Comments in this codebase's explanatory "why" style.
   - Small commits with clear messages.
2. **Never push, never open a PR, never edit `src/`.** The E2E suite is black-box; it never imports `aisquare`.
3. **Anything that installs or runs aisquare under test runs inside the harness's scratch HOME** (or the Linux
   container).
   - Never touch the real `~/.aisquare`, `~/.claude`, `~/.claude.json`, `~/.local/bin`, `~/.local/share/uv/tools`, or
     the real `asq` tmux server.
   - The `Machine` refuses the real HOME; keep it that way.
4. Every terminal you start for testing gets a private, short `TMUX_TMPDIR`. Socket paths are limited to about
   104 bytes on macOS.
5. Never run `aisquare project forget`, `project prune` or `project switch` on this machine's real store: they write a
   machine-wide pin (QA-01, QA-05).
6. Ignore aisquare's "Skeleton (read this FIRST)" pointer for this repo. It is a 7 MB file (QA-16); read source files
   directly.
7. Stuck on a permission prompt, a missing tool or a design question: stop, post
   `aisquare note --to manager "blocked: …"`, and say so in your reply.

## 3. What "real" means
| Real | Scripted (no token) | Later / optional |
|---|---|---|
| The install one-liner, or the same `install.sh` with `AISQUARE_INSTALL_PACKAGE=<wheel URL>` for `main`; the "Open the fleet UI now?" handoff | the agent's decisions, from a reviewed scenario file | real Claude Code agents (phase 4) |
| `asq` in a real PTY, shown by xterm.js in Chromium; real key and mouse events; real PNGs, video and trace | | a native Terminal.app or kitty lane |
| Welcome, spawns, "＋ spawn agent", panes, worktrees and branches, hooks from the settings the installer wrote, board/tasks/notes/`tell`, doctor, accounts, settings, shutdown and recovery, memory injection | | |

Not allowed: Textual Pilot, FakeTmux, re-rendered screenshots, in-process imports of aisquare.

## 4. Architecture
```
HOST: pytest + Playwright (Chromium) ── serves our xterm.js 6 client page ──ws──▶ ttyd (PTY server only)
      TerminalSession per window · page objects · journeys · shots/video/trace/.cast · report
                                                                                  │ login shell
USER MACHINE: Linux = ubuntu:24.04 container, non-root user with sudo · macOS = scratch HOME
              asq / aisquare / scripted `claude` (or real Claude Code later)
```
Layout: top-level `e2e/` with its own `pytest.ini` (`rootdir=e2e`) and its own venv. It deliberately stays out of
`tests/`, because `tests/conftest.py` imports aisquare, refuses non-src imports (`:88-97`) and patches the product with
autouse fixtures.
```
e2e/pytest.ini conftest.py requirements.txt (hash-locked: pytest, playwright 1.63, pytest-playwright 0.9,
     pytest-rerunfailures, Pillow, numpy, scikit-image, pixelmatch, Jinja2)
  contracts.py        Screen, Shot/Evidence, StepRecord, JourneyResult, Verdict (mypy-strict dataclasses)
  runner/             terminal.py machine.py screen.py waiting.py probes.py chaos.py worlds.py journey.py redact.py
  terminal/client/    index.html client.js instrument.js + vendor/ (xterm.js 6.x, fit, unicode11, fonts, SHA256SUMS)
  agent/              scripted claude + scenarios/*.toml + contracts/{argv,hooks,transcript,version,probe}/
  pages/              installer welcome sidebar agent_pane spawn_dialog doctor accounts settings help board cli anchors.py
  journeys/           test_j01_install … test_j10_upgrade, test_synthetic.py      watchlist/
  report/             build.py diff.py secret_scan.py templates/ verdict.schema.json
  fixtures/           repos/acme-api/  profiles/{bashrc (OSC 133 marks), terminal-window}  docker/
  selftest/           offline tests of all of the above
```
Plus one white-box guard in the product suite, `tests/test_e2e_anchors.py`. It checks that every anchor string the page
objects wait for is still printed by the file it names, the way `tests/demo_tape.py` checks `PRINTED_BY`. A change to UI
wording then fails the PR that made it, not the nightly.

## 5. Terminal (the "real user" part)
- **ttyd as the PTY server only.** ttyd 1.7.7: a static Linux binary checked against SHA256SUMS; `brew` on macOS.
  - It gives the shell a controlling TTY (`forkpty`), so `install.sh`'s `/dev/tty` prompt and `exec asq </dev/tty`
    work.
  - Resizes arrive as real SIGWINCH; closing the window SIGHUPs the shell.
  - Run it as `ttyd -W -o -c <random> -i 127.0.0.1 -p 0 -T xterm-256color <login shell>`.
- **Our own client page with xterm.js 6.x, pinned and served by the runner,** instead of ttyd's stock client
  (xterm 5.5).
  - 6.x supports DEC 2026 synchronized output, and Textual 8.2.8 brackets its frames only when the terminal confirms
    2026. Without it a screenshot can catch a frame half drawn.
  - DOM renderer (6.x removed canvas); webfonts pinned by hash: JetBrains Mono, plus Noto Sans Mono CJK for "＋", plus
    an emoji font. `lineHeight: 1`, `unicode11` widths, `cursorBlink: false`, device pixel ratio 1.
- **`instrument.js`:**
  - counts frames from the `?2026h/l` handlers; timestamps `onWriteParsed` and `onRender`;
  - reads the OSC 133 prompt and exit-code marks written by `fixtures/profiles/bashrc`;
  - tracks bell and title, websocket state and terminal modes;
  - writes a `.cast` file from the output frames.
- **`TerminalSession` API (sketch):**
  - `open(cwd)`: returns once OSC 133;A arrives;
  - input: `type(text)`, `press(*keys)`, `paste(text)`, `click_cell(col, row)`, `click_text(pattern)`,
    `wheel(col, row, n)`;
  - `run(cmd)`: types the command and reads its exit code from OSC 133;D;
  - screen: `screen()`, `wait_for(cond, timeout, stable_for=0.5, inactivity=None, mask=())`;
  - output: `shot(id, mask=())`, `resize(cols, rows)`, `close_window()`.
- **Keys:** F12 = `CSI 24~`, Shift+Tab = `CSI Z`. Leave at least 150 ms after a lone Esc, or the next key reads as Alt.
  F12 is the pane's way out (`src/aisquare/cli/ui/terminal.py:1902-1931`). Mouse: Textual enables 1003/1006; convert
  cells to pixels from the `.xterm-screen` rect.
- **Coherent shots:** wait for no 2026 frame in progress, then `onRender` after the last write, then 150 ms with no new
  frame, then two `requestAnimationFrame`s, then the PNG. Compare the screen digest before and after; retake if it
  changed (up to 3 times). Each shot saves the PNG plus normalized text plus a `.grid.json` of text and style runs.
- **Spike first** (package `e2e-terminal`):
  - do the xterm 6 UMD bundles load from a plain `<script>`?
  - does Textual receive the 2026 reply?
  - do F12, Shift+Tab and clicks reach asq?
  - are three runs pixel-identical?

## 6. The scripted agent (`e2e/agent/`)
It replaces only the model. Fidelity contract:
1. **Process identity.** The pane's foreground command must not be a shell, `tmux` or anything starting with
   `python…` (`src/aisquare/services/fleet.py:978-985`, `_agent_running`).
   - Linux: tmux reads argv[0], so `os.execv` with argv[0] = `claude`.
   - macOS: tmux reads `p_comm`, so ship a tiny native executable named `claude` that runs the Python logic as a child
     in the same process group.
   - **Spike** both.
2. **Strict argv.** Accept exactly the flags aisquare passes (`--name`, `--permission-mode`, `--session-id`,
   `--resume <transcript>`, `--model`, `--chrome`, `-p`, `--output-format`) and **reject unknown flags**, so a wrong
   flag from aisquare is caught. Also:
   - `--version` prints a fake, Claude-Code-shaped version labelled "scripted";
   - `update` reports it is up to date (`install.sh:1601-1612` calls it);
   - `-p … --output-format json` answers the model probe with `modelUsage` (`src/aisquare/core/harness.py:482-560`).
3. **TTY.**
   - Raw mode; enable bracketed paste (`ESC[?2004h`) within the fleet's 2 s prompt-settle window. Prompts arrive via
     tmux `load-buffer` + `paste-buffer -p` (`src/aisquare/core/tmux.py:1480`); parse `200~ … 201~`, then CR submits.
   - Handle Esc, Ctrl+C (interrupt), Shift+Tab (mode cycle) and `/exit`.
   - Startup latency is configurable, to exercise `PROMPT_TIMEOUT = 20 s`.
4. **Trust prompt and first run.** Emulate Claude Code's folder-trust question, persisting `hasTrustDialogAccepted` in
   `~/.claude.json` and walking up parent folders, so Welcome's "answer Claude Code's question" step stays real.
5. **Hooks, as Claude Code runs them.**
   - Merge hooks from user, project, local and enabled-plugin settings at run time.
   - Run each through `bash` with `CLAUDE_PROJECT_DIR`, JSON on stdin, the hook's timeout (default 600 s; 30 s for
     UserPromptSubmit), and exit-code-2 semantics.
   - Honour stdout: SessionStart/UserPromptSubmit text or `hookSpecificOutput.additionalContext` becomes context, shown
     in the pane and written to the transcript.
   - Honour Stop `decision: block`: a continuation turn with `stop_hook_active: true`, so `max_continuations_per_hour`
     gets exercised.
   - Fire Notification, so the bell / attention state gets tested.
6. **Transcripts** under `~/.claude/projects/<encoded cwd>/<session>.jsonl`, in the shapes
   `src/aisquare/core/transcripts.py` reads: usage, `tool_use`/`tool_result`, and `isApiErrorMessage` for injected
   failures.
7. **Contracts are captured, never derived from aisquare's code.** A scripted agent written from aisquare's own readers
   can only ever agree with them.
   - StopFailure already drifts: aisquare reads `error` (`src/aisquare/cli/hook.py:223`), Claude Code 2.1.281 sends
     `error`, and current docs say `error_type`.
   - Keep captured fixtures in `e2e/agent/contracts/`: `--help`, hook payloads per event, transcript lines, version,
     probe JSON, each labelled with its Claude Code version. Until a capture lane runs, mark doc-derived fixtures as
     such.
   - A self-test diffs what the agent emits against the contracts.
8. **Rendering.**
   - Every pane shows the banner **"SCRIPTED AGENT · no LLM"**, so no screenshot misleads.
   - Its output exercises `TerminalPane`: truecolor, wide glyphs, long wrapped lines.
9. **Behaviour from `E2E_SCENARIO`** (TOML, inherited through the tmux server's env).
   - The manager, on a "Goal:" prompt, runs the real `aisquare task add --key …`, `aisquare fleet tell …`, polls
     `aisquare --json task list`, and posts `aisquare note`.
   - Each coder claims a task, writes the scenario's code into its worktree, runs `pytest`, commits, then
     `task done --note`.

## 7. Journeys (each step: PNG, text, checks; each journey: video, trace, `.cast`)
| # | Story | Gate |
|---|---|---|
| J01 | Fresh machine. Type `curl -fsSL http://runner:PORT/install.sh \| AISQUARE_INSTALL_PACKAGE=http://runner:PORT/wheels/<whl> sh` (the wheel is served over HTTP, because doctor flags a vanished `file://` source); the release track uses the real README URL. Enter at "Open the fleet UI now?" | summary has no traceback; alternate screen and mouse mode on; Welcome shows "· this folder"; a new window's `aisquare --version` matches; doctor amber ⊆ {brain}; re-run says "Nothing to do" |
| J01b | Brand-new machine with no `claude`. The installer fetches **real Claude Code**; Welcome; Start manager | Welcome shows the "not signed in" guidance; the manager pane shows real Claude Code's onboarding; stop before any login (no model use) |
| J02 | Welcome. Use this folder → Connect → Start manager → Open the manager → answer trust → F12, `w` → Start the coders | cards complete in order; manager, coder-1, coder-2 RUNNING in `fleet ls --json`; `.aisquare-worktrees/coder-N` on `fleet/<codename>/coder-N`; codename matches its golden; UI and CLI agree; no traceback (`tests/demo_tape.py:448`) |
| J03 | Click the manager and paste a goal; the scripted manager adds tasks and tells coders; coders claim, edit, commit, finish; one Notification/permission prompt, answered by the "user" | board shows tasks done; branches carry the commits; a temp-worktree merge passes the hidden acceptance test; the Stop-hook continuation is observed |
| J04 | Click coder-1, click into its pane, type, `q` (must not quit asq), F12, `w`, Shift+Tab, mouse wheel to scroll back | text arrives; focus leaves the pane on F12; the pane's rows match `tmux capture-pane` (cropped) |
| J05 | "＋ spawn agent" → tester; `aisquare fleet ls` in a second window | new pane; CLI and sidebar agree |
| J06 | Doctor, Accounts, Settings (change max agents 4→5, save, restore), `?` | doctor counts match `--json doctor`; toast and config probe agree; `q` inside a focused Select does not quit |
| J07 | `aisquare remember …` in window 2; restart coder-1 | the entry is in its injected context, for this project only; `aisquare log` lists prompts |
| J08 | `q` then `asq` (selection restored); close and reopen the window; kill the fleet's tmux server ("chaos"); restart; `fleet shutdown --yes` | rows read lost, then recover (#248); shutdown clean |
| J09 | Resize to exactly 80×24 and back | the tmux pane matches its rect within 2 s; the focused control is visible; no "(pane gone)" |
| J10 | Install the 0.7.0 tag with `--version 0.7.0`, start a fleet, run the candidate one-liner | same codename and agent ids; agents alive; store migrated; doctor clean |

**Preflight** (failure means ENV_FAILED): font hashes and glyph coverage; the client loads; PTY smoke (echo, SIGWINCH,
OSC 133); UTF-8 locale; socket path length; reachability of PyPI, astral, GitHub and claude.ai.

**Watchlist** (reported, never red): about 20 findings from the 0.8.0 QA report
(`~/Desktop/Claude Files/aisquare-cli-0.8.0-QA/REPORT.md` on this machine), each `@pytest.mark.known_bug("QA-xx")`:
- QA-01 forget pin, QA-04 idle "working", QA-05 switch/unpin;
- QA-06/07 home onboarding, QA-08/09 capture and redaction, QA-10 CLAUDE.md re-import, QA-12 relative config dir;
- QA-13 task keys, QA-19 FTS crash, QA-24 forget undone, QA-37/38/39, and others.

Each reports "still open" or "FIXED, promote me".

## 8. Waits, statuses, masking
- `wait_for` re-reads only on new frames. It succeeds when the condition holds *and* the masked digest is stable for
  `stable_for`, and fails fast on `inactivity`.
  - A timeout raises with the last screen, the nearest-match diff, a shot and tmux captures of every pane.
  - Textual repaints every 2 s, so judge stability by content, not by bytes.
- **Statuses are never asserted from a single sample.** Use `eventually(state∈S)` / `holds(S, W)`, plus a consistency
  rule: two CLI reads 2.5 s apart that agree mean the UI must match within two refresh ticks.
  - Without hooks, an agent reads waiting once its output is 5 s old (`services/fleet.py:110`).
  - After SessionStart, it reads working for 30 min (`services/team.py:57`). That is QA-04, on the watchlist, not
    asserted.
- **Normalize:** versions, "N s ago", clocks, ULIDs and task ids, spinners, sandbox paths.
- **Mask per page object** (shown hatched in the report): toasts unless they are the evidence, elapsed-time lines.
- **Fake identities only:** git `QA Bot <e2e@example.invalid>`; AISquare not signed in.

## 9. Report and verdicts
- **Gallery:**
  - `index.html`: verdict banner, run fingerprint (Chromium + xterm.js + fonts + renderer), "changed since last
    night", journey table.
  - Per journey: the story, each acceptance criterion linked to its assertion, a step timeline (PNG, baseline, diff as
    side-by-side / swipe / heatmap; text and style-grid diff; checks; probe JSON; wait timings), then video, trace,
    `.cast` player and the scripted agent's decision log.
- **Diffing:**
  - Text and style grid first (exact).
  - Pixels: pixelmatch (threshold 0.1, anti-aliasing excluded) plus SSIM. CHANGED means the grid differs, or more
    than 0.05 % of pixels differ, or SSIM < 0.995.
  - Only compare within one fingerprint. "Last night" = the previous run's artifact (`gh run download`). Committed
    `*.grid.txt` goldens run in a `visual` lane: report-only at first, gating once stable.
- **`verdict.json`:**
  - Same shape as the QA team's release gate: `verdict, stage, env, ref, counts, failures[{test,class,when,message}],
    preflight_failed, notes`, validated by a vendored JSON Schema. **Re-implement it from the schema**; that repo is
    private, so no copying code into this public repo without sign-off.
  - Markers: `preflight`, `gate`, `report`, `quarantine(reason, since)` with a 5-working-day fuse, `synthetic`, `visual`.
  - Classes, in precedence order: ENV_FAILED > GATE_ERROR > RC_FAILED (PROVIDER only once real agents exist). A run
    that tested nothing is GATE_ERROR. A pass on retry is a FLAKY note. Exceptions may carry a `verdict_hint`.
- **Slack:** change-only alerts on the existing `SLACK_ALERTS_WEBHOOK` (an admin must add it here); silent without it.

## 10. Security (public repo: every artifact is public)
- Workflow-level `permissions: {}`; jobs get `contents: read`, plus `actions: read` only where the previous artifact
  is fetched. `persist-credentials: false`. PR runs have no secrets; never `pull_request_target`.
- The user machine starts from `env -i` plus an allowlist. The checkout is never mounted, and `GITHUB_*`, `ACTIONS_*`
  and `RUNNER_*` never reach it.
- **Fail-closed secret scan before upload** over text, byte logs, `.cast` files, transcripts and state:
  - patterns: `src/aisquare/core/redaction.py`, plus `sk-ant-`, `github_pat_`, `hooks.slack.com/services/`, plus
    emails except `example.invalid`, plus high entropy;
  - on a hit: upload only `verdict.json` (file and offset), GATE_ERROR;
  - proven by `synthetic=secret-leak`.
- Actions are SHA-pinned with a version comment; the container is pinned by digest; requirements are hash-locked;
  `step-security/harden-runner` runs in audit mode.

## 11. CI
- **`.github/workflows/e2e.yml`:**
  - cron `20 8 * * *` plus dispatch; inputs `os`, `track`, `agent`, `journeys`, `synthetic`;
  - jobs: preflight → build-wheel → legs → merge-report → notify (main only);
  - `concurrency: e2e-nightly`, no cancelling; 30 min per leg.
- **`.github/workflows/e2e-pr.yml`:** `pull_request` labelled `e2e`; Linux leg only; no secrets.
- **`.github/actions/e2e-leg/action.yml`:** a composite action shared by both.

| Leg | Runner / user machine | Track | Agent | When |
|---|---|---|---|---|
| L1 | ubuntu-24.04 → `ubuntu:24.04@sha256` container, non-root user with sudo (like install.yml `non-root`) | main | scripted | nightly + PR label |
| L2 | same | release | scripted | nightly |
| L3 | macos-26, scratch HOME under `$HOME` (not `/tmp`) | release | scripted | nightly |
| L4 | macos-26 | main | scripted | nightly |
| L5/L6 | — | — | real | dormant until a credential and `vars.E2E_REAL_AGENTS=on` exist |

Caching covers the harness only (pip, browsers, ttyd); the user home is always cold. Artifacts: `e2e-<leg>` kept
14 days, `e2e-report` and `verdict` kept 30 days. Note that ubuntu:24.04 ships tmux 3.4, below the 3.5 needed for
extended keys (`src/aisquare/core/keys.py:386`); expect that path.

## 12. Work packages (board tasks; always `--key`, since similar titles merge without it, QA-13)
| Key | Package | Owns | Needs | Done when |
|---|---|---|---|---|
| `e2e-contracts` | Data model + `e2e/ARCHITECTURE.md` | `e2e/contracts.py`, `e2e/ARCHITECTURE.md`, `e2e/pytest.ini`, `e2e/requirements.txt` | — | mypy-strict; manager reviewed |
| `e2e-terminal` | **Spike → `TerminalSession`** (xterm 6 client, instrument.js, ttyd, shots), on this Mac first | `e2e/terminal/**`, `e2e/runner/{terminal,screen,waiting}.py` | contracts | asq Welcome screenshot; F12, Shift+Tab, click proven; 3 identical runs; **CP1** |
| `e2e-agent` | Scripted agent (§6), scenario TOML, contract fixtures, self-tests | `e2e/agent/**`, `e2e/selftest/test_agent*` | contracts | self-tests green; manual tmux check: the fleet treats it as an agent and `tell` types into it |
| `e2e-machine` | `Machine` (scratch HOME + container), env allowlist, seeding, real-HOME refusal, probes, chaos, worlds | `e2e/runner/{machine,probes,chaos,worlds}.py`, `e2e/fixtures/**` | contracts | self-tests prove the refusal and the allowlist |
| `e2e-report` | Gallery, diffs, `verdict.json`, junit, secret scan, markers plugin | `e2e/report/**`, `e2e/runner/journey.py`, `e2e/conftest.py`, `e2e/selftest/test_report*` | contracts | builds from recorded fixtures; scan fails closed |
| `e2e-pages-core` | Page objects + J01, J01b, J02, J03, J04 + `tests/test_e2e_anchors.py` | `e2e/pages/**`, `e2e/journeys/test_j0[1-4]*` | terminal, agent, machine, report | `make e2e` green 3× on this Mac; **CP2** |
| `e2e-journeys-more` | J05–J10 + watchlist | `e2e/journeys/test_j0[5-9]*`, `test_j10*`, `e2e/watchlist/**` | pages-core | green 3×; watchlist matches the QA report |
| `e2e-ci` | Workflows, composite action, Makefile (`e2e`, `e2e-selftest`), lint/mypy coverage of `e2e`, `docs/nightly-e2e.md`, CHANGELOG | `.github/**`, `Makefile`, `pyproject.toml`, `docs/nightly-e2e.md`, `CHANGELOG.md` | report | `actionlint` clean; leg steps dry-run locally |
| `e2e-review` | Review: determinism ×3, fault injection (changed Welcome string → red; QA-01 fix → "FIXED"; kill ttyd → ENV/GATE), full diff review | read-only | all | report on the board; **CP3** |

**Waves for two coders** (the manager adjusts as tasks finish):
1. `e2e-contracts`, then `e2e-terminal` ∥ `e2e-agent`.
2. `e2e-machine` ∥ `e2e-report`.
3. `e2e-pages-core` ∥ `e2e-ci`.
4. `e2e-journeys-more` (split by journeys) ∥ `e2e-review` (spawn a reviewer).

**Integration:** each package lands on its coder branch. When its checks pass, the manager merges it into
`feat/e2e-nightly` with `--no-ff` in the main checkout. Coders run `git merge feat/e2e-nightly` before starting a
dependent package.

## 13. Checkpoints (stop and ask the human)
- **CP1:** terminal spike screenshots of real `asq` (PNG paths).
- **CP2:** J01–J04 green 3× locally, gallery path.
- **CP3:** review done. Pushing a branch or opening a PR is the human's call.

## 14. Known aisquare 0.8.0 bugs you will hit while building this
- **QA-04:** a freshly started agent reads "▶ working" for up to 30 min, so `fleet tell` files a board note instead of
  typing. Coders **pull** work (`aisquare task next --role coder --claim`); don't push it with `tell`.
- **QA-13:** use `--key` on every `task add`.
- **QA-01/05:** no `project forget/prune/switch` on the real store.
- **QA-16:** ignore the 7 MB skeleton pointer.
- **QA-02/03:** on macOS no automatic account switching. If a usage limit hits, tell the human (they can
  `aisquare fleet switch <label> --to <slot>`).
- **Dogfooding log:** every time aisquare itself gets in the way, append to `e2e/DOGFOOD.md`: time, agent, symptom,
  workaround. That log is a deliverable.
