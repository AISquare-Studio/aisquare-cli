# One-line install — `curl … | sh` to a green doctor and the fleet UI

> **Status: planned.** Nothing in this document exists yet. Branch
> `plan/one-line-install`. Written against `main` @ `f4c3387` (0.6.0).
>
> Every measurement below was taken on 2026-09-04 on Fedora 44, Python 3.14.7
> local / 3.13 via uv, tmux 3.7c, Claude Code 2.1.260, uv 0.12.3, Node 26.7.0,
> gh 2.97.0. Numbers attributed to a run were produced by that run, not
> estimated.
>
> Fences in this file are tagged `text` on purpose.
> `tests/test_documented_commands.py` sweeps every `.md` in the repo and treats a
> shell-tagged fence as a script whose every `aisquare …` line must resolve
> against the live command tree. The commands here are *planned*, so they are
> shown as references. Keep them `text` until the script exists, then list
> `install.sh`'s own documentation in `DOCUMENTED` and let the guard have it.

---

## 0. The ask, restated as acceptance criteria

From the owner's brief of 2026-09-04. Each line is something the finished work
must do.

1. **One command.** A person who has never heard of this project runs one line
   in a terminal and ends up with a working install.
2. **It detects the OS** and takes the right path for it — no "pick your
   platform" step, no reading a table first.
3. **It installs and sets up every dependency**, not just the CLI, so that
   `aisquare doctor` is green for everything the script installed.
4. **Except gbrain.** The brain layer is explicitly out of scope; its doctor
   line stays a warning and the script says so rather than hiding it.
5. **It ends by asking.** When it finishes, it prompts to open the CLI; `Y`
   drops the user straight into the fleet UI.
6. **The owner is told whether one script or several are needed** — §2 answers
   this, with the reasoning rather than just the verdict.
7. **The one-line command is stated**, `curl`/`wget` piped to a shell.

Two criteria are added here because the brief implies them and leaving them
implicit is how installers become unsafe:

8. **Re-running it is safe.** An install script that is not idempotent is a
   script nobody dares run twice, which means nobody dares run it once.
9. **It works unattended.** CI, a Dockerfile and a provisioning tool must be
   able to run it with no terminal at all, and get a non-zero exit when
   something genuinely failed.

---

## 1. The measured baseline — what `doctor` actually says

This is the part that decides the whole design, so it was measured rather than
reasoned about. `aisquare doctor` runs **17 checks** (`services/diagnostics.py`
`doctor()`); `--json` was parsed rather than the rendered table read.

### 1.1 A fresh machine, nothing set up

`AISQUARE_HOME` pointed at a directory that did not exist, in a git repo that
was not registered:

| Verdict | Checks |
| --- | --- |
| `fail` | `home` |
| `warn` | `tiktoken`, `claude-code` |
| `ok` | the other 14 |

`home` is the only check in the whole set that can **fail**, and it is what
makes `aisquare doctor` exit 1. Seven checks — `database`, `snapshot`, `brain`,
`harness`, `fleet` and friends — answer `not created yet` at `ok` status by
design (`_uncreated_home`): diagnosis must not create the home it is
diagnosing, and `home` already owns that verdict.

### 1.2 After `init`, before anything else

`aisquare init --local --yes --no-onboard` in that repo:

| Verdict | Checks |
| --- | --- |
| `warn` | `tiktoken`, `claude-code`, `snapshot`, `brain` |
| `ok` | the other 13 |

Exit 0 — no failures. **Four warnings is the real target surface**, and each has
a different owner:

| Check | Why it warns | Who can fix it |
| --- | --- | --- |
| `tiktoken` | not importable from the CLI's own environment, so snapshot token counts are estimated | the **installer** — it chooses how the CLI is installed, so it chooses what is in that environment |
| `claude-code` | Claude Code is present but aisquare's five lifecycle hooks are not in its `settings.json` | the **installer** — `agents connect claude-code` |
| `snapshot` | no Repomix pack for the active project | the **installer**, but only once it knows which directory the user means |
| `brain` | `gbrain` is not on PATH | **nobody, on purpose** — out of scope per §0.4 |

### 1.3 The end state, proven

The two commands below were run against a scratch `AISQUARE_HOME`, a scratch
`CLAUDE_CONFIG_DIR` and a throwaway git repo:

```text
uv tool install --python 3.13 --with tiktoken aisquare-cli
aisquare init --local --yes --agent claude-code
```

Result, from `aisquare --json doctor`:

```text
total=17 ok=16 not-ok=1
  warn  brain        gbrain not found — team decisions/results are not distilled
```

**16 of 17 green, gbrain the only warning.** That is §0.3 and §0.4 satisfied
exactly, and it is worth being blunt about what it means: the hard part of this
work is *not* getting the doctor green. Two commands already do that. The hard
part is everything around them — getting a Python and a `uv` onto a machine that
has neither, getting `tmux`, `gh` and Node from four different package managers,
and doing it all from inside a pipe without a terminal to read from.

### 1.4 A gap this measurement exposed

`_check_repomix` reports `ok` when `npx` merely **exists**:

```text
if shutil.which("repomix"): ok
if shutil.which("npx"):     ok — "available on demand via npx"
else:                       warn
```

Repomix 1.18.0 declares `"node": ">=22.0.0"`. Debian 12 ships Node 18 and
Ubuntu 22.04 ships 12. On those machines `npx` exists, the check is green, and
the first `project onboard` fails at runtime. The installer must therefore check
the Node **version**, not `npx`'s presence — and the check should be tightened
to match (§6.3). Left alone, this is the one way the script could report success
and hand over a machine that cannot pack a snapshot.

---

## 2. One script, or one per OS?

**One POSIX `sh` script for macOS, Linux and WSL2, plus a ~20-line PowerShell
shim for Windows-native.** Recommended, and the reasoning matters more than the
verdict.

The instinct behind the question is right — OS detection *is* where installers
rot. But it is worth measuring how much of this script actually varies by OS
before splitting it:

| Step | Varies by OS? |
| --- | --- |
| Detect OS / arch / WSL | the detection itself, ~15 lines |
| Install `uv` | **no** — one curl installer, macOS + every Linux |
| Install Python | **no** — `uv` does it, from its own managed builds |
| Install `aisquare-cli` | **no** — `uv tool install` |
| Install Claude Code | **no** — one curl installer, macOS + Linux + WSL |
| Install `tmux`, `gh`, Node | **yes** — the package manager, ~8 lines of `case` |
| `aisquare init`, hooks, doctor, the prompt | **no** |

So roughly **85% of the script is identical across every Unix platform**, and
the varying part is one `case` statement selecting a package-manager command.
Two scripts would duplicate the 85% and drift in it — and drift in an installer
is not a cosmetic problem, because the failure mode is "works on my laptop,
bricks a colleague's".

The reason this ratio is so favourable is a deliberate choice, not luck: **every
dependency that *can* be installed by a platform-independent curl installer,
is** (§3.1). `uv` and Claude Code both ship exactly that. What is left needing a
package manager is only the three system tools that genuinely have no other
sane source.

Windows-native is a real fork rather than a `case` branch, because the fleet
needs tmux and there is no tmux on Windows. It gets its own tiny `install.ps1`
whose entire job is to detect WSL2, and either delegate into it or print the one
command that installs it. That mirrors what the code already does — `asq` on
Windows prints "the fleet needs tmux — run inside WSL2" rather than a traceback
(`docs/plans/fleet-tui.md` §3.9).

**Rejected: one script per distro.** Four scripts to maintain, and the honest
version of the matrix is not four but a dozen. The `case` statement is the same
logic with one copy.

**Rejected: a Python-based installer.** It would need a Python to run, which is
the problem it was meant to solve.

**Rejected: Homebrew as the macOS path.** A formula is a good thing to have, but
it is a *different* deliverable with its own review cycle, and it does not
answer §0.1 for Linux. It belongs on the roadmap, not in this script.

---

## 3. Design decisions, with the alternatives rejected

### 3.1 `uv` is the bootstrap, not `pipx` and not the system Python

This is the load-bearing decision, and it is what makes §2's 85% possible.

The naive script starts "ensure Python 3.11+, then `pipx install aisquare-cli`".
That premise is where installers of Python tools go wrong. It requires a Python
that is new enough, a `pip` that works, a `pipx` installed through *something*,
and it inherits every distro's opinion about externally-managed environments
(PEP 668, which turns a plain `pip install --user` into an error on Debian and
Fedora both).

`uv` collapses all of it:

- It is a **single static binary** with its own curl installer, on macOS and
  every Linux, needing no Python at all.
- It **installs Python itself** from managed builds. Measured: on a machine
  whose newest system Python is 3.14, `uv tool install --python 3.13` fetched a
  managed 3.13 and used it. So the script never has to care what Python the OS
  ships, or whether it is too old.
- `uv tool install` produces exactly the install shape the `install` doctor
  check wants: a real executable on PATH at `~/.local/bin/aisquare`, outside any
  virtualenv. That check specifically warns when the binary's path contains
  `.venv` or `venv`, so "install into a venv and add it to PATH" is not an
  option — it would ship a permanent amber line.
- `--with` puts extra packages in the tool's own environment, which is how
  `tiktoken` gets fixed in the same breath as the install (§1.2).

Pinning Python to **3.13**: the highest version CI actually tests
(`.github/workflows/ci.yml` matrix is 3.11–3.13). Deliberately not "whatever is
newest" — 3.14 works locally today but no CI job proves it, and an installer is
the wrong place to find out.

**One trap, measured.** This machine's distro-packaged `uv` has
`python-downloads = manual`, and the install failed with:

```text
error: No interpreter found for Python 3.13 in search path or managed installations
hint: A managed Python download is available for Python 3.13, but Python
      downloads are set to 'manual', use `uv python install 3.13` to install
```

The script must therefore **force the behaviour it relies on** —
`UV_PYTHON_DOWNLOADS=automatic` in its own environment — rather than assume
uv's default. A user who already has `uv` is the common case, not the rare one,
and their config is not ours to guess at. With that exported, the same command
succeeded and installed 0.6.0 with tiktoken.

**Rejected: `pipx`.** Needs a Python and a bootstrap of its own; `pipx inject`
would then be a second step for tiktoken. It stays *supported* — nothing about
this plan breaks a `pipx install aisquare-cli` — it is just not what the script
uses.

**Rejected: vendoring a Python.** uv already solved this, better.

### 3.2 Four classes of dependency, four strategies

Every dependency is put in exactly one class, and the class decides how the
script treats it. This table is the script's actual specification.

| Class | Members | Strategy | If it fails |
| --- | --- | --- | --- |
| **Bootstrap** | `uv` | its own curl installer, to `~/.local/bin` | **fatal** — nothing else can proceed |
| **Ours** | Python 3.13, `aisquare-cli`, `tiktoken` | `uv tool install --python 3.13 --with tiktoken aisquare-cli` | **fatal** |
| **System** | `tmux`, `gh`, Node | the platform package manager (§5) | **warn and continue** — each degrades one feature, and `doctor` will say which |
| **Agent** | Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` | **warn and continue** — the CLI works; the fleet has nothing to spawn |

The fatal/non-fatal split is not squeamishness, it mirrors what the code already
does. `_check_tmux` warns rather than fails, with the comment "the fleet is the
one feature that needs it, and a machine that runs every other command is not
unhealthy". An installer that aborts on a missing `tmux` would be stricter than
the product.

**Node is the awkward one** and gets stated rules rather than a guess: install
it only when absent *or* older than 22 (§1.4), preferring the platform package
when it is new enough, and `fnm` — another single static binary — when it is
not. Never `sudo npm install -g`, which the Claude Code docs call out as a
security risk and which is how `~/.npm` ends up root-owned.

### 3.3 `curl | sh` means stdin is the script, not the terminal

The most important implementation detail in this plan, and the one most likely
to be got wrong, because it is invisible until a human tries it.

When the script runs as `curl … | sh`, **stdin is the pipe carrying the script's
own bytes**. So §0.5's prompt cannot be `read -r answer`. That reads the rest of
the script as the answer — which either consumes the script, or reads EOF and
returns instantly, so the prompt appears to answer itself and the script exits.

The fix is to read the terminal explicitly:

```text
if [ -r /dev/tty ]; then
    printf 'Open the aisquare fleet UI now? [Y/n] '
    read -r answer < /dev/tty || answer=n
else
    answer=n            # no terminal: CI, a Dockerfile, a provisioner
fi
```

Verified that the guard is the right shape: a script piped to `sh` with stdin
closed still finds `/dev/tty` readable when a terminal exists, so the test
distinguishes "piped, but a human is here" — the exact case this whole feature
is for — from "no human anywhere".

The same applies to **launching the UI**. Only stdin is the pipe; stdout and
stderr are already the terminal. So handing over is:

```text
exec asq < /dev/tty
```

`exec` and not a plain call, so the UI replaces the shell rather than running as
its child with a pipe still attached to it.

And `asq` must not be launched when there is no terminal, because the code
already has a contract for that case and it is not a TUI. Measured on 0.6.0:

| Invocation | stdout | Exit |
| --- | --- | --- |
| bare `aisquare`, piped | the usage page | 2 |
| `aisquare --json`, piped | `{"error": "usage", "message": "Missing command."}` | 2 |

An installer that ended by piping `asq` into something would therefore finish by
printing a help page and exiting 2 — reading, correctly, as a failure. The
`/dev/tty` guard is what keeps that from happening.

### 3.4 Idempotence, stated per step

Re-running must be boring (§0.8). Each step is either naturally idempotent or
made so:

| Step | On a second run |
| --- | --- |
| `uv` installer | detects its own install; skipped entirely when `uv` is on PATH |
| `uv tool install` | needs `--force` to replace an existing tool, so: skip when the installed version already matches, `--force` when upgrading |
| package manager | `apt install tmux` on a machine that has tmux is a no-op |
| Claude Code installer | manages its own versions dir and symlink |
| `agents connect claude-code` | already idempotent by construction — `install_hooks` filters out aisquare's own hook groups before re-appending them, so hooks never accumulate |
| `aisquare init` | registers an already-registered project without complaint; **never** `--reinit`, which resets `config.toml` and discards `team bind` role bindings |

That last one is worth its own line, because `--reinit` is exactly the flag a
script author reaches for to make a step "clean", and here it would silently
destroy user configuration on every re-run.

### 3.5 Flags and environment, for humans and for CI

Defaults suit a human at a terminal; every one is overridable, which is the
precedence rule the rest of the project already uses.

| Flag | Env | Default | Effect |
| --- | --- | --- | --- |
| `--yes` / `-y` | `AISQUARE_INSTALL_YES=1` | off | never prompt; do not launch the UI |
| `--no-agent` | — | off | skip Claude Code |
| `--no-system-deps` | — | off | skip tmux/gh/Node (for a locked-down box) |
| `--project <dir>` | — | `$PWD` if a git repo, else none | which project to `init` |
| `--no-project` | — | off | machine setup only, register nothing |
| `--version <v>` | `AISQUARE_INSTALL_VERSION` | latest | pin `aisquare-cli` |
| `--python <v>` | — | `3.13` | the interpreter uv resolves |
| `--dry-run` | — | off | print every command, run none |
| `--verbose` | — | off | stream sub-installer output |

Piped invocation takes arguments through `sh -s`, which is worth documenting
because the syntax is not obvious:

```text
curl -fsSL https://aisquare.studio/install.sh | sh -s -- --yes --no-agent
```

`--dry-run` is not a nicety. It is how a person decides whether to trust a
script they are about to pipe into a shell, and it is how reviewers read this
one.

### 3.6 What the script deliberately does not do

- **gbrain** — §0.4. Its warning stays, and the closing summary names it as
  expected rather than leaving the user to wonder about the one amber line.
- **Explainability** — off unless asked for, which is the product's own posture.
  `doctor` reports `tracing is off (turn it on with: aisquare explainability
  enable)` at **ok** status, so this costs nothing.
- **Authenticating Claude Code.** `claude setup-token` opens a browser and
  prints a year-long token to stdout; capturing that inside an install script is
  a credential-handling design that deserves its own review, and the first
  `claude` run does it properly. The script's last line names it as the next
  step.
- **`gh auth login`** — interactive by nature. `_gh_login_note` already appends
  `(no login found: gh auth login)` to the doctor line, so the machine tells the
  user itself.
- **Editing shell profiles beyond what sub-installers do.** uv and the Claude
  installer each manage their own PATH entry. If `~/.local/bin` is still not on
  PATH at the end, the script says so with the one line to add, and does not
  silently rewrite `.zshrc`.

### 3.7 Root, sudo, and what gets written

The script **must not be run as root** and refuses to be, except in a container
where there is no other user. Everything in the Bootstrap and Ours classes lands
under `$HOME`; the only step that needs elevation is the System class, and there
`sudo` is called for that command alone, visibly, never by re-executing the
whole script.

If `sudo` is absent or the user cannot use it, that is not fatal: System-class
failures are warnings (§3.2), and the script prints the exact commands an
administrator would need to run.

Paths written, in full:

```text
~/.local/bin/                     uv, aisquare, asq, claude
~/.local/share/uv/tools/          the aisquare-cli tool environment
~/.local/share/claude/versions/   Claude Code
~/.aisquare/                      config.toml, context.db, projects/
~/.claude/settings.json           MERGED — aisquare's five hook groups only
```

`~/.claude/settings.json` is the one file the script edits rather than creates,
and it is not edited by the script at all — it is edited by `agents connect`,
which merges and is already covered by tests. Saying it out loud here is the
point: a user piping a script into a shell is owed a list of what it will touch.

### 3.8 It verifies, and it reports honestly

The last step is `aisquare --json doctor`, parsed. Not the rendered table — that
is Rich output wrapped to terminal width, which is a bad parsing target for the
same reason `test_documented_commands.py` refuses to read `--help`.

The summary then distinguishes three kinds of amber, because collapsing them is
how a script teaches people to ignore its output:

1. **Expected** — `brain`. Named as out of scope.
2. **Actionable by the user** — `gh` present but logged out, Claude Code not
   authenticated. Printed with the one command each.
3. **Unexpected** — anything else. Printed in full, with the doctor's own `fix`
   string, since every non-ok check carries one.

Exit codes: `0` when nothing is unexpected, `1` when a fatal class failed, `2`
when the install completed but an unexpected check is amber. A script that exits
0 onto a broken machine is worse than one that never ran.

---

## 4. The script, step by step

Fifteen steps. Each is one shell function, which is also the unit the tests in
§8 address.

```text
 1  preflight      not root; sh is POSIX; curl or wget present; $HOME writable
 2  detect         uname -s/-m -> os, arch; /proc/version or $WSL_DISTRO_NAME -> wsl
 3  pkg_manager    apt|dnf|pacman|zypper|apk|brew  (§5)
 4  banner         what this will install and where (§3.7); honour --dry-run
 5  install_uv     skip if on PATH; else the astral installer; export
                   UV_PYTHON_DOWNLOADS=automatic  (§3.1)
 6  install_cli    uv tool install --python 3.13 --with tiktoken aisquare-cli
                   (--force only when upgrading; §3.4)
 7  path_check     is ~/.local/bin on PATH? if not, say the line to add (§3.6)
 8  install_tmux   >= 3.2 required, >= 3.5 preferred; warn-only
 9  install_gh     warn-only
10  install_node   only if absent or < 22; prefer system, else fnm (§3.2)
11  install_claude claude.ai/install.sh; warn-only
12  init           aisquare init --local --yes --agent claude-code [<project>]
13  doctor         aisquare --json doctor; classify (§3.8)
14  summary        what was installed, what is amber and why, what is next
15  handoff        /dev/tty guard; prompt; exec asq < /dev/tty  (§3.3)
```

Step 12 is where the four warnings of §1.2 collapse to one. `--agent
claude-code` makes `init` install the hooks and ingest `~/.claude/CLAUDE.md`,
and without `--no-onboard` it packs the Repomix snapshot in the same run —
measured output:

```text
✓ aisquare initialized at <home>
  project: proj (prj_c33d85baffee543db5ca88c3)
  note: Snapshot: 1 files, 367 tokens packed for fast agent context.
  note: Connected claude-code: hooks installed, imported 0 entries.
```

Which is `claude-code` and `snapshot` fixed, `tiktoken` having been fixed at
step 6, and `home`/`config`/`database` created. The one remaining amber is
`brain`.

**Which project?** `$PWD` when it is a git repo, and nothing otherwise —
registering `$HOME` because someone ran the installer from their home directory
is a mess that persists in the store. With no project, steps 12–13 still run for
the machine-wide checks, and the summary's next step is `cd` somewhere and
`aisquare init`.

---

## 5. The package-manager matrix

Commands taken from each project's official documentation, not from memory.
`tmux` is in every distro's own repositories; `gh` needs an added repository on
Debian and Fedora.

| Platform | Detect | tmux | gh |
| --- | --- | --- | --- |
| macOS | `uname -s` = Darwin | `brew install tmux` | `brew install gh` |
| Debian/Ubuntu | `apt-get` | `apt-get install -y tmux` | keyring + `cli.github.com/packages` source, then `apt install gh` |
| Fedora/RHEL | `dnf` | `dnf install -y tmux` | `dnf install dnf5-plugins`, `dnf config-manager addrepo --from-repofile=…/gh-cli.repo`, `dnf install gh` |
| Arch | `pacman` | `pacman -S --noconfirm tmux` | `pacman -S --noconfirm github-cli` |
| openSUSE | `zypper` | `zypper install -y tmux` | `zypper addrepo …/gh-cli.repo && zypper install gh` |
| Alpine | `apk` | `apk add tmux` | `apk add github-cli` |

Two notes that will otherwise cost someone an afternoon:

- **`dnf config-manager addrepo` is dnf5 syntax.** dnf4 spells it
  `--add-repo` and needs `dnf install 'dnf-command(config-manager)'`. The script
  branches on `dnf --version`. Fedora 41+ is dnf5; RHEL 9 and derivatives are
  dnf4.
- **Homebrew may be absent on macOS,** which is the platform where the script is
  most likely to need it. Prompt to install it (or `--yes` to proceed without),
  and degrade to warn-only if declined — do not install Homebrew silently, it is
  a large thing to put on someone's machine unasked.

An unrecognised platform is not a failure. It skips the System class with a
warning naming the three tools, exactly as `install_hint()` already does when
`/etc/os-release` names a distro it does not know — that function returns all
three hints rather than a wrong one, and this script should behave the same way.

---

## 6. Bugs in the current tree this work must fix

Found while measuring §1. All three are in the path a new user walks, which is
this plan's whole subject.

### 6.1 `doctor` recommends installing the wrong package — `services/diagnostics.py:147,153`

Both `install` fixes say:

```text
Install as a global tool: pipx install aisquare
```

`aisquare` on PyPI is the **Explainability SDK** — a different project, version
1.2.0, "Explainability SDK for tracing, graphing, and policy auditing of AI
agents". This CLI is `aisquare-cli`. Anyone following that advice installs the
wrong package.

Worse than a typo, because of something the tree already documents at length:
the SDK ships its own `aisquare/__init__.py` into the same directory this
package occupies, and pip's RECORD for the two overlaps on that file. The
`explainability` extra exists partly to control the install *order* for that
reason. So this fix string does not merely fail — it can land the user in the
one dependency shape `pyproject.toml` has a twelve-line comment warning about.

Fix: `pipx install aisquare-cli`, and add the `uv tool install` form beside it,
since that is what the installer will have used.

### 6.2 The tiktoken fix names the same wrong package — `services/diagnostics.py`

```text
Install it: pip install tiktoken (or: pipx inject aisquare tiktoken)
```

`pipx inject` takes the name of an installed **pipx environment**, which is
`aisquare-cli`. `pipx inject aisquare tiktoken` fails on any machine that
followed the documented install.

Fix: `pipx inject aisquare-cli tiktoken`, plus the
`uv tool install --with tiktoken aisquare-cli` form.

### 6.3 `repomix` is green when it cannot run — `services/diagnostics.py`

§1.4 in full. `npx` present is treated as sufficient; Repomix needs Node ≥ 22;
Debian 12 ships 18 and Ubuntu 22.04 ships 12. Green check, failing feature.

Fix: read the Node version and warn below 22, naming it. This one is a genuine
behaviour change to a check, so it is a small PR of its own with its own tests,
landing before the script depends on it.

---

## 7. Hosting, and the one-line command

### 7.1 The command

Once `install.sh` is on `main` and the vanity path is published:

```text
curl -fsSL https://aisquare.studio/install.sh | sh
```

`wget`, for the machines that have no curl:

```text
wget -qO- https://aisquare.studio/install.sh | sh
```

With arguments (note the `-s --`, §3.5):

```text
curl -fsSL https://aisquare.studio/install.sh | sh -s -- --yes
```

Working immediately, with no DNS or hosting work at all:

```text
curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh
```

Windows-native, PowerShell:

```text
irm https://aisquare.studio/install.ps1 | iex
```

### 7.2 Why the vanity URL is worth the small effort

`aisquare.studio` is already the project's homepage in `pyproject.toml`, so the
domain exists and this is a redirect, not a deployment. It buys three things the
raw GitHub URL cannot:

- **It is memorable and quotable.** A README line someone can retype.
- **It decouples the URL from the branch.** `raw.githubusercontent.com/…/main/…`
  bakes in a branch name; a redirect can be repointed to a tag, which is how you
  stop shipping every mid-flight commit to new users.
- **It is a place to pin.** `…/install.sh` can serve the latest *released*
  script while `main` moves.

`-fsSL` on every curl, deliberately: `-f` so an HTTP error is a non-zero exit
rather than an error page piped into a shell, `-S` so the error is visible, `-L`
to follow the redirect that makes the vanity URL work.

### 7.3 Serving it from a tag, not a branch

New users should not get whatever landed on `main` an hour ago. Point the vanity
URL at the script from the newest release tag, and have the release process
update it — a one-line change to `publish.yml`'s neighbourhood, or a redirect
rule, depending on where `aisquare.studio` is hosted. **This is an open question
for the owner (§11.2)** because the answer depends on hosting this plan cannot
see.

---

## 8. Tests

A `curl | sh` installer is the least testable and highest-blast-radius artifact
a project can ship, so this section is not optional and the CI job is part of
the deliverable, not a follow-up.

### 8.1 Static

- **`shellcheck`** on `install.sh`, and **`shfmt -d`** for format, both added to
  the `check` job. Non-negotiable for a script that runs on other people's
  machines.
- `sh -n` under `dash`, not just `bash`, so a bashism cannot reach a Debian
  `/bin/sh`.
- A guard that the script contains no `read -r` without `< /dev/tty` (§3.3) —
  that is the bug this plan is most worried about reintroducing, and it is
  trivially greppable.

### 8.2 Unit, per function

`install.sh` sources cleanly with `AISQUARE_INSTALL_LIB=1` set, so each function
can be called from a test with stub commands on PATH:

- `detect` against captured `uname` / `/etc/os-release` / `/proc/version`
  fixtures for each platform in §5, WSL included.
- `pkg_manager` picks the right one when several exist, and degrades to
  warn-only when none is recognised.
- `--dry-run` executes nothing: assert against a PATH of stubs that all fail.
- Node version comparison: 18 and 22.0.0 and 26 on either side of the boundary.

### 8.3 Integration, in containers

The real test, and the only one that proves §0.2. A matrix job over
`debian:12`, `ubuntu:22.04`, `fedora:44`, `archlinux`, `alpine:3.20`, each
running the real one-liner against a local checkout, then asserting:

```text
aisquare --json doctor  ->  every check ok EXCEPT brain
```

That single assertion is the whole plan's acceptance criterion, and it is
machine-checkable. `alpine` is in the matrix on purpose: it is musl, `/bin/sh`
is BusyBox ash, and it is where a bashism or a GNU-only flag surfaces.

Claude Code is stubbed rather than really installed in most cells, so the matrix
does not depend on a third party's CDN being up; one cell installs it for real
and is allowed to be flaky.

macOS via `runs-on: macos-latest` for the Homebrew path. Windows is manual for
now, as the fleet's key matrix already is.

### 8.4 Idempotence, as an assertion

Every container cell runs the installer **twice** and asserts the second run
exits 0, changes no version, and leaves `~/.claude/settings.json` byte-identical
to after the first. §3.4 is a claim; this is what makes it true. Hook
accumulation is exactly the kind of bug that only shows up on the second run,
and `install_hooks` filtering its own groups is what prevents it — so the test
belongs here rather than in the description.

---

## 9. Phased delivery

Each phase is one PR, `make check` green, mergeable alone.

| Phase | Contents | Why this order |
| --- | --- | --- |
| **1** | §6.1, §6.2, §6.3 — the three doctor fixes, with tests | Independent of the script and worth shipping regardless. Also: the script asserts a green doctor, so a wrong `fix` string or a lying `repomix` check would be baked into its acceptance test. |
| **2** | `install.sh`: steps 1–7 and 12–15 (uv, CLI, init, doctor, handoff), Linux + macOS. Unit tests (§8.2), shellcheck (§8.1). | The spine. Already gets a machine that had tmux/gh/Node to 16/17 green, which is most developer machines. |
| **3** | Steps 8–11 — the System and Agent classes, the §5 matrix. Container matrix (§8.3, §8.4). | The part that varies by OS, landing once the spine is proven, with the tests that make the matrix a fact. |
| **4** | `install.ps1` for Windows-native — WSL2 detection and delegation. | Smallest audience, and it delegates to phase 2/3's script. |
| **5** | README's install section rewritten around the one-liner; the vanity URL; `docs/install.md`. | Documentation follows a working thing. Doing this first would advertise a script that does not exist. |

Phase 2 is the one to review hardest. It is where §3.3 lives.

---

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| **`curl \| sh` is a trust ask.** Some users and most security teams will refuse it. | Never the only path. Keep `pipx install aisquare-cli` and `uv tool install aisquare-cli` documented and supported; offer `--dry-run` and "download, read, then run" in the README. |
| **The prompt bug of §3.3 reappears** in a later edit by someone who does not know why `< /dev/tty` is there. | A comment at the line saying why, plus the static guard in §8.1 so the reason is enforced rather than remembered. |
| **A third-party installer changes its interface** (astral or Anthropic). | The container matrix runs on a schedule as well as on PRs, so it fails on our clock rather than a new user's. Both are pinned where pinning is possible. |
| **Node stays the weak link.** Distro Node is old; `fnm` adds a version manager to someone's shell. | `repomix` is warn-only and the product works without it. After §6.3 the check tells the truth, which is the actual requirement. |
| **A user's `~/.claude/settings.json` is precious.** | Untouched by the script; written only by `agents connect`, which merges and has tests. §8.4 asserts byte-identity across a re-run. |
| **`uv` may already be installed and configured differently** — measured, §3.1. | Force `UV_PYTHON_DOWNLOADS=automatic` in the script's environment; never write to the user's uv config. |
| **Version skew**: the script from `main` installing an older released CLI. | §7.3 — serve the script from a tag. Until that exists, `--version` pins explicitly. |

---

## 11. Open questions for the owner

Everything else in this document is decided. These four need an answer that is
not in the code.

1. **`uv` as the bootstrap — agreed?** It is the decision the rest depends on
   (§3.1). The alternative is requiring a system Python 3.11+ and bootstrapping
   `pipx`, which is more code, more failure modes, and puts PEP 668 in the path.
2. **Where does `aisquare.studio` serve from,** and can it get an
   `/install.sh` redirect (§7.2)? Until it can, the raw GitHub URL works and is
   uglier. Related: serving from a tag rather than `main` (§7.3).
3. **Should the script install Homebrew on a macOS machine that lacks it?**
   Recommended: ask, and degrade to warn-only if declined (§5). It is a big
   thing to install unasked, and macOS is the platform where its absence is most
   likely.
4. **How much does the fleet UI need on first launch?** §0.5 lands the user in
   the TUI, but a fresh install has one project and no manager running, so the
   first screen is the Welcome page. Worth deciding whether the handoff should
   instead land on the project with its Manager tab open — a small UI change,
   and a better first impression than a checklist.

---

## Decisions log

| Date | Decision |
| --- | --- |
| 2026-09-04 | Plan written against `main` @ `f4c3387` (0.6.0). Baselines in §1 measured, not estimated: 17 checks, 4 warnings after `init`, 16/17 green after the §1.3 recipe. |
| 2026-09-04 | One POSIX `sh` script for macOS/Linux/WSL2 plus a PowerShell shim, not one per OS (§2) — 85% of the steps do not vary by platform. |
| 2026-09-04 | `uv` chosen as the bootstrap over `pipx` + system Python (§3.1). `UV_PYTHON_DOWNLOADS=automatic` forced, after a measured failure on a distro-packaged uv with `python-downloads = manual`. |
| 2026-09-04 | Python pinned to 3.13 — the highest version CI tests. |
| 2026-09-04 | Three pre-existing bugs found while measuring and folded in as phase 1 (§6): two `doctor` fixes naming the SDK instead of this CLI, and `repomix` green on Node < 22. |
