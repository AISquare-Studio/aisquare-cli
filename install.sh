#!/bin/sh
# aisquare one-line installer — curl … | sh to a green doctor and the fleet UI.
#
#   curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh
#
# Design, measurements and the rejected alternatives: docs/plans/one-line-install.md.
# Section marks below (§) point into it. Four things about this file are
# load-bearing and easy to undo by accident:
#
#  1. EVERYTHING IS A FUNCTION, and `main` is called on the LAST LINE. Under
#     `curl … | sh` the shell interprets the stream as it arrives, so a
#     connection that drops mid-download runs whatever prefix arrived. With the
#     work inside functions, a truncated download defines some functions and
#     calls nothing — the standard mitigation, and the reason no top-level
#     statement below does anything but define things.
#  2. STDIN IS THE SCRIPT, NOT THE TERMINAL (§3.3). Nothing here may `read`
#     from stdin; the prompt and the handoff go through /dev/tty. There is a
#     static guard for this in tests/test_install_script_is_posix.py, because it
#     is invisible until a human tries it and it is the bug this plan is most
#     worried about reintroducing.
#  3. POSIX sh, not bash. It runs as Debian's `dash` and Alpine's BusyBox `ash`.
#     No `[[`, no arrays, no `local -a`, no `pipefail`, no `${x,,}`.
#  4. `uv tool upgrade` DOES NOT move a pinned install (§3.9.1) — it prints
#     "Nothing to upgrade" and exits 0. Upgrades here always go through
#     `uv tool install --force … @latest`. A test asserts that, so a future edit
#     cannot quietly swap in the friendlier-looking command.
#
# shellcheck shell=sh
# shellcheck disable=SC3043  # `local` is not in POSIX, but dash, BusyBox ash,
# bash, ksh and zsh all implement it; the alternative is prefixed globals in a
# 700-line script, which trades a theoretical portability risk for a real
# variable-collision one. Proven rather than assumed: CI runs this script under
# dash and under BusyBox ash (§8.3).

set -eu

AISQUARE_INSTALL_VERSION_SELF="1.0.0"

# --- what the script installs, and the floors it holds them to ---------------

PYPI_PACKAGE="aisquare-cli"
PYPI_JSON_URL="https://pypi.org/pypi/aisquare-cli/json"

#: What `uv tool install` is actually pointed at. Overridable so CI can test
#: THIS TREE rather than the last release: the container matrix (§8.3) asserts a
#: green doctor, and a doctor check whose behaviour changed in the branch under
#: review would otherwise be graded by the version already on PyPI. A path
#: (wheel, sdist or project directory) has no PyPI version to compare against,
#: so setting this also turns the version comparison off — see `resolve`.
INSTALL_TARGET=${AISQUARE_INSTALL_PACKAGE:-$PYPI_PACKAGE}
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
CLAUDE_INSTALLER_URL="https://claude.ai/install.sh"
FNM_INSTALLER_URL="https://fnm.vercel.app/install"
NODESOURCE_DEB_URL="https://deb.nodesource.com/setup_NODEMAJOR.x"
NODESOURCE_RPM_URL="https://rpm.nodesource.com/setup_NODEMAJOR.x"

DEFAULT_PYTHON="3.13"
# The highest version CI actually tests (.github/workflows/ci.yml is 3.11–3.13).
# Deliberately not "whatever is newest": 3.14 works on the machine this was
# measured on, but no CI job proves it, and an installer is the wrong place to
# find that out.

MIN_TMUX_MAJOR=3
MIN_TMUX_MINOR=2
# core/tmux.py MIN_VERSION — `new-window -e` and `extended-keys` arrived in 3.2.
RECOMMENDED_TMUX_MINOR=5
# 3.5 is where S-Enter reaches an agent pane; below it the fleet works without.

MIN_NODE_MAJOR=22
# THE SAME NUMBER AS `core/snapshot.py`'s MIN_NODE, which is the source of truth
# — repomix@1.18.0 declares "engines": {"node": ">=22.0.0"}. It has to be
# duplicated here because this script runs before any Python exists, which is
# the whole premise of §3.1; tests/test_install_script_is_posix.py asserts the
# two are equal so they cannot drift.
#
# Debian 12 ships Node 18 and Ubuntu 22.04 ships 12, which is why this is a real
# floor and not a formality (§1.4).

# `brain` is always expected to be amber when this finishes — gbrain is out of
# scope (§0.4). `expected_amber` adds to it when the run asked for no project.
EXPECTED_AMBER="brain"

# --- options (§3.5) ---------------------------------------------------------

ASSUME_YES=${AISQUARE_INSTALL_YES:-0}
WANT_AGENT=1
WANT_SYSTEM_DEPS=1
WANT_PROJECT=1
PROJECT_DIR=""
PIN_VERSION=${AISQUARE_INSTALL_VERSION:-}
PYTHON_VERSION="$DEFAULT_PYTHON"
DRY_RUN=0
VERBOSE=0
UPGRADE_ALL=0
OFFLINE=0
FORCE=0

# --- state the steps fill in ------------------------------------------------

OS=""
ARCH=""
IS_WSL=0
PKG=""
PKG_SUDO=""
DNF_MAJOR=""

HAVE_CURL=0
HAVE_WGET=0

UV_VERSION=""
CLI_VERSION=""
CLAUDE_VERSION=""
TMUX_VERSION=""
GH_VERSION=""
GIT_VERSION=""
NODE_VERSION=""
LATEST_VERSION=""

CLI_ACTION=""    # install | upgrade | current
NODE_ACTION=""   # install | current
TMUX_ACTION=""   # install | current
GH_ACTION=""     # install | current
GIT_ACTION=""    # install | current
CLAUDE_ACTION="" # install | update

INSTALLED_LIST=""
UPGRADED_LIST=""
SKIPPED_LIST=""
WARNINGS=0
UNEXPECTED=0

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

# Colour only when stdout is a terminal AND NO_COLOR is unset. Under `curl … |
# sh` stdout IS the terminal (only stdin is the pipe), so this is normally on —
# which is the whole reason it has to be guarded for the CI and Dockerfile case.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD=$(printf '\033[1m')
    C_DIM=$(printf '\033[2m')
    C_RED=$(printf '\033[31m')
    C_GREEN=$(printf '\033[32m')
    C_YELLOW=$(printf '\033[33m')
    C_RESET=$(printf '\033[0m')
else
    C_BOLD=''
    C_DIM=''
    C_RED=''
    C_GREEN=''
    C_YELLOW=''
    C_RESET=''
fi

say() { printf '%s\n' "$*"; }
note() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
step() { printf '%s==>%s %s\n' "$C_BOLD" "$C_RESET" "$*"; }
good() { printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }

warn() {
    WARNINGS=$((WARNINGS + 1))
    printf '  %swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2
}

# Fatal: the Bootstrap and Ours classes only (§3.2). Exit 1, and say what a
# person can do next rather than only what failed.
die() {
    printf '%serror%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
    printf '%s\n' "Nothing was left half-installed on purpose: rerun this script, or" >&2
    printf '%s\n' "install by hand with: uv tool install --with tiktoken aisquare-cli" >&2
    exit 1
}

debug() {
    [ "$VERBOSE" = 1 ] || return 0
    printf '  %s· %s%s\n' "$C_DIM" "$*" "$C_RESET"
}

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

# Run a command, honouring --dry-run. Every state-changing invocation goes
# through here or through sudo_run, which is what makes --dry-run a guarantee
# rather than a claim — tests/test_install_script_is_posix.py asserts it against
# a PATH of stubs that all fail.
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s %s\n' "$C_DIM" "$C_RESET" "$*"
        return 0
    fi
    debug "run: $*"
    if [ "$VERBOSE" = 1 ]; then
        "$@"
    else
        "$@" >/dev/null 2>&1
    fi
}

# The System class's only elevation (§3.7): sudo for ONE command, visibly, never
# by re-executing this script as root.
sudo_run() {
    if [ "$PKG_SUDO" = "" ]; then
        run "$@"
        return $?
    fi
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s sudo %s\n' "$C_DIM" "$C_RESET" "$*"
        return 0
    fi
    printf '  %s(sudo)%s %s\n' "$C_DIM" "$C_RESET" "$*"
    if [ "$VERBOSE" = 1 ]; then
        sudo "$@"
    else
        sudo "$@" >/dev/null 2>&1
    fi
}

# stdout <- URL. curl preferred, wget the fallback; -fsSL deliberately (§7.2):
# -f so an HTTP error is a non-zero exit rather than an error page piped into a
# shell, -S so the error is visible, -L to follow the vanity redirect.
fetch() {
    if [ "$HAVE_CURL" = 1 ]; then
        curl -fsSL "$1"
    else
        wget -qO- "$1"
    fi
}

# Pipe a remote installer into a shell. $2 is the interpreter, because uv's
# installer is POSIX sh and Claude Code's needs bash.
fetch_into_shell() {
    _url=$1
    _shell=$2
    shift 2
    if [ "$HAVE_CURL" = 1 ]; then
        curl -fsSL "$_url" | "$_shell" -s -- "$@"
    else
        wget -qO- "$_url" | "$_shell" -s -- "$@"
    fi
}

# True when the CONTROLLING TERMINAL can actually be opened.
#
# Not `[ -r /dev/tty ]`, which the plan's §3.3 sketch used: in a container with
# no controlling terminal the device node exists and its mode bits pass the test,
# while open(2) on it fails with ENXIO. Measured in `docker run` without `-t`,
# where `[ -r /dev/tty ]` is TRUE and the read then fails. Opening it is the only
# test that answers the question actually being asked, which is "is a human
# here?" — and getting it wrong in the permissive direction is how an unattended
# run ends up blocked on a prompt nobody can see.
tty_available() {
    # A SUBSHELL, and `true` rather than `:`. Both halves were measured, and the
    # obvious spelling — `{ : </dev/tty; } 2>/dev/null` — is a LATENT ABORT:
    #
    #   `:` is a POSIX SPECIAL BUILT-IN, and "if a redirection error occurs with
    #   a special built-in, a non-interactive shell shall exit". Not "returns
    #   non-zero" — EXITS, whatever `set -e` says and even inside an `if`
    #   condition where `set -e` is suspended. Measured under bash (which is
    #   /bin/sh on Fedora, RHEL, Arch and macOS): with no controlling terminal
    #   the installer died right here, silently, exit 1, having printed nothing
    #   since the last step. Under dash it survived — so the bug was invisible
    #   on Debian and Ubuntu and fatal on the rest.
    #
    # The machines it killed are precisely the ones §0.9 exists for: CI, a
    # Dockerfile, a provisioning run, anything under `setsid`. The container
    # matrix did not catch it because every cell passes `--yes`, which
    # short-circuits before this is ever called — see
    # tests/test_install_script_functions.py, which now reaches it with
    # start_new_session so the condition is real rather than simulated.
    #
    # A subshell cannot take the parent down with it whatever the redirect does,
    # and `true` is a regular built-in rather than a special one. Either change
    # alone fixes it; both are here because the cost is nothing and the failure
    # mode is an installer that exits 1 with no explanation.
    (true </dev/tty) 2>/dev/null
}

# Ask a yes/no question. Reads /dev/tty, NEVER stdin (§3.3) — stdin is this
# script's own bytes. `--yes` and "no terminal" both answer without asking, and
# they answer DIFFERENTLY, which is the point of having both: --yes means "do
# not stop to ask me", no-terminal means "there is nobody to ask".
confirm() {
    _prompt=$1
    _default=$2
    if [ "$ASSUME_YES" = 1 ]; then
        debug "confirm: --yes, answering y to: $_prompt"
        return 0
    fi
    if ! tty_available; then
        debug "confirm: no terminal, answering $_default to: $_prompt"
        [ "$_default" = y ]
        return $?
    fi
    if [ "$_default" = y ]; then
        printf '  %s [Y/n] ' "$_prompt"
    else
        printf '  %s [y/N] ' "$_prompt"
    fi
    _answer=""
    # shellcheck disable=SC2162  # -r is what we want; no backslash processing.
    read -r _answer </dev/tty || _answer=""
    [ -n "$_answer" ] || _answer=$_default
    case "$_answer" in
        y | Y | yes | YES | Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# Compare dotted versions numerically, field by field, over four fields.
#
# `sort -V` would be shorter and is not portable: BusyBox `sort` has no -V, so it
# would silently degrade to a lexical compare on Alpine and call 0.10.0 older
# than 0.9.0. Non-numeric suffixes are stripped per field, which is what lets the
# same function read `tmux 3.7c`. The known limit, stated rather than discovered:
# a pre-release (`1.0.0rc1`) compares equal to its release, so this must not be
# used to order pre-releases against each other.
version_lt() {
    _lhs=$1
    _rhs=$2
    [ "$_lhs" = "$_rhs" ] && return 1
    _field=1
    while [ "$_field" -le 4 ]; do
        _l=$(printf '%s' "$_lhs" | cut -d. -f"$_field" | tr -cd '0-9')
        _r=$(printf '%s' "$_rhs" | cut -d. -f"$_field" | tr -cd '0-9')
        [ -n "$_l" ] || _l=0
        [ -n "$_r" ] || _r=0
        if [ "$_l" -lt "$_r" ]; then return 0; fi
        if [ "$_l" -gt "$_r" ]; then return 1; fi
        _field=$((_field + 1))
    done
    return 1
}

# ---------------------------------------------------------------------------
#  1  preflight
# ---------------------------------------------------------------------------

# True in a container, where root is the only user there is (§3.7).
in_container() {
    [ -f /.dockerenv ] && return 0
    [ -f /run/.containerenv ] && return 0
    [ -n "${container:-}" ] && return 0
    if [ -r /proc/1/cgroup ] && grep -qE 'docker|lxc|containerd|kubepods' /proc/1/cgroup 2>/dev/null; then
        return 0
    fi
    return 1
}

preflight() {
    step "Checking this machine can be installed onto"

    # Root is refused, except where there is no other user (§3.7). Everything in
    # the Bootstrap and Ours classes lands under $HOME, so running as root would
    # put the tools in /root and leave the user's own PATH without them — a
    # failure that looks like success until the first `aisquare` command.
    if [ "$(id -u)" = 0 ] && [ "${AISQUARE_INSTALL_ALLOW_ROOT:-0}" != 1 ]; then
        if in_container; then
            note "running as root inside a container — allowed, there is no other user here"
        else
            die "do not run this as root: uv, aisquare and Claude Code install under \$HOME,
so a root run installs them into root's home and leaves yours without them.
Run it as your own user (it uses sudo only for tmux/gh/Node), or set
AISQUARE_INSTALL_ALLOW_ROOT=1 if you really mean root's home."
        fi
    fi

    have curl && HAVE_CURL=1
    have wget && HAVE_WGET=1
    if [ "$HAVE_CURL" = 0 ] && [ "$HAVE_WGET" = 0 ]; then
        die "neither curl nor wget is available, and every installer this script
uses is fetched over HTTPS. Install one of them first."
    fi

    if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
        die "\$HOME is not set to a directory, and everything this script installs
for you lands under it."
    fi
    if [ ! -w "$HOME" ]; then
        die "\$HOME ($HOME) is not writable."
    fi

    # Says what actually held, not a fixed sentence: the root line above can
    # contradict it, and an installer's own preflight must not print something
    # untrue two lines after printing the truth.
    _who=$(id -un 2>/dev/null || echo "uid $(id -u)")
    _downloader=curl
    [ "$HAVE_CURL" = 1 ] || _downloader=wget
    good "$_downloader present, \$HOME writable, running as $_who"
}

# ---------------------------------------------------------------------------
#  2  detect_os
# ---------------------------------------------------------------------------

detect_os() {
    OS=$(uname -s 2>/dev/null || echo unknown)
    ARCH=$(uname -m 2>/dev/null || echo unknown)

    case "$OS" in
        Linux) OS=linux ;;
        Darwin) OS=macos ;;
        FreeBSD | NetBSD | OpenBSD) OS=bsd ;;
        MINGW* | MSYS* | CYGWIN*) OS=windows ;;
        *) OS=unknown ;;
    esac

    # WSL: the env var when a shell was started by WSL, /proc/version otherwise
    # (a service or a cron job inside WSL has no WSL_DISTRO_NAME).
    if [ -n "${WSL_DISTRO_NAME:-}" ]; then
        IS_WSL=1
    elif [ -r /proc/version ] && grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
        IS_WSL=1
    fi

    if [ "$OS" = windows ]; then
        die "this is Windows-native (Git Bash / MSYS). The fleet UI runs agents in
tmux, and there is no tmux on Windows — so aisquare runs inside WSL2 there.
Install WSL2 with:   wsl --install
then run this same command inside the Ubuntu shell it gives you.
(install.ps1 in this repo does both steps for you.)"
    fi

    _label="$OS/$ARCH"
    [ "$IS_WSL" = 1 ] && _label="$_label (WSL2)"
    step "Detected $_label"
}

# ---------------------------------------------------------------------------
#  3  pkg_manager
# ---------------------------------------------------------------------------

# Which package manager to use for the System class, and whether sudo is needed.
#
# Ordered rather than first-found-wins where it matters: a machine can carry
# several (Homebrew on Linux beside apt, `apk` inside a Docker builder on a
# Debian host). The native manager is the one that owns /usr, so it wins on
# Linux; brew is only the answer on macOS.
pkg_manager() {
    PKG=""
    case "$OS" in
        macos)
            if have brew; then PKG=brew; fi
            ;;
        linux)
            if have apt-get; then
                PKG=apt
            elif have dnf; then
                PKG=dnf
                DNF_MAJOR=$(dnf --version 2>/dev/null | head -1 | cut -d. -f1 | tr -cd '0-9')
                [ -n "$DNF_MAJOR" ] || DNF_MAJOR=4
            elif have pacman; then
                PKG=pacman
            elif have zypper; then
                PKG=zypper
            elif have apk; then
                PKG=apk
            fi
            ;;
    esac

    # sudo only when we are not already root and sudo exists. Absent sudo is not
    # fatal: System-class failures are warnings (§3.2) and the summary prints
    # the commands an administrator would run.
    PKG_SUDO=""
    if [ "$PKG" != "" ] && [ "$PKG" != brew ] && [ "$(id -u)" != 0 ]; then
        if have sudo; then
            PKG_SUDO=sudo
        fi
    fi

    if [ "$PKG" = "" ]; then
        debug "no known package manager"
    else
        debug "package manager: $PKG${DNF_MAJOR:+ (dnf$DNF_MAJOR)}${PKG_SUDO:+, via sudo}"
    fi
}

# ---------------------------------------------------------------------------
#  4  survey — what is already here (§3.9). READS ONLY; writes nothing.
# ---------------------------------------------------------------------------

# Six tools print six different version-string shapes, so there are six parsers
# and not one clever regex:
#
#   aisquare 0.6.0                        -> field 2
#   uv 0.12.3 (x86_64-unknown-linux-gnu)  -> field 2
#   2.1.263 (Claude Code)                 -> field 1
#   tmux 3.7c                             -> field 2, non-numeric suffix
#   gh version 2.97.0 (2026-07-31)        -> field 3
#   v26.7.0                               -> field 1, `v` prefix
#
# All six were measured on 2026-09-08, not recalled.

first_line() { head -1 2>/dev/null || true; }

version_of_aisquare() { aisquare --version 2>/dev/null | first_line | cut -d' ' -f2; }
version_of_uv() { uv --version 2>/dev/null | first_line | cut -d' ' -f2; }
version_of_claude() { claude --version 2>/dev/null | first_line | cut -d' ' -f1; }
version_of_tmux() { tmux -V 2>/dev/null | first_line | cut -d' ' -f2; }
version_of_gh() { gh --version 2>/dev/null | first_line | cut -d' ' -f3; }
version_of_git() { git --version 2>/dev/null | first_line | cut -d' ' -f3; }
version_of_node() { node --version 2>/dev/null | first_line | sed 's/^v//'; }

survey() {
    step "Surveying what is already installed"

    have uv && UV_VERSION=$(version_of_uv || true)
    have aisquare && CLI_VERSION=$(version_of_aisquare || true)
    have claude && CLAUDE_VERSION=$(version_of_claude || true)
    have tmux && TMUX_VERSION=$(version_of_tmux || true)
    have gh && GH_VERSION=$(version_of_gh || true)
    have git && GIT_VERSION=$(version_of_git || true)
    have node && NODE_VERSION=$(version_of_node || true)

    _report uv "$UV_VERSION"
    _report aisquare "$CLI_VERSION"
    _report "Claude Code" "$CLAUDE_VERSION"
    _report tmux "$TMUX_VERSION"
    _report gh "$GH_VERSION"
    _report git "$GIT_VERSION"
    _report Node "$NODE_VERSION"
}

_report() {
    if [ -n "$2" ]; then
        note "$1 $2"
    else
        note "$1 — not installed"
    fi
}

# ---------------------------------------------------------------------------
#  5  resolve — decide per dependency: current | behind | absent (§3.9)
# ---------------------------------------------------------------------------

# The latest published aisquare-cli, or "" when it cannot be determined.
#
# The extraction is anchored rather than the greedy `sed` the plan sketched in
# §3.9.2, and the difference is not cosmetic. PyPI's payload embeds the whole
# README in `info.description`, where every quote is BACKSLASH-ESCAPED. Splitting
# on commas and anchoring on `^"version":"` therefore cannot match a
# `\"version\":\"9.9.9\"` that appears in prose, while a greedy `.*"version":"…`
# takes the LAST match in a 44 KB blob and would. Measured against exactly that
# input. A regex over JSON is still the wrong instrument in general; it is
# acceptable here because it reads one short stable field from a first-party API
# and its failure mode is handled — an empty answer means "could not determine",
# which is reported and never treated as "behind".
latest_version() {
    if have python3; then
        # Preferred when a Python exists: no parsing of JSON by regex at all.
        fetch "$PYPI_JSON_URL" 2>/dev/null | python3 -c \
            'import json,sys
try:
    sys.stdout.write(json.load(sys.stdin)["info"]["version"])
except Exception:
    pass' 2>/dev/null && return 0
    fi
    fetch "$PYPI_JSON_URL" 2>/dev/null |
        tr ',' '\n' |
        sed -n 's/^"version":"\([^"]*\)".*/\1/p' |
        head -1
}

resolve() {
    step "Deciding what needs doing"

    # --- aisquare-cli -------------------------------------------------------
    if [ "$INSTALL_TARGET" != "$PYPI_PACKAGE" ]; then
        # A local build: always install it, and never compare versions — a wheel
        # in a directory is not "behind" or "current" relative to PyPI, and
        # pretending otherwise is how a CI cell would silently test the release
        # instead of the branch.
        LATEST_VERSION=""
        CLI_ACTION=install
        [ -n "$CLI_VERSION" ] && CLI_ACTION=upgrade
        note "installing from $INSTALL_TARGET (AISQUARE_INSTALL_PACKAGE)"
        _resolve_system
        return 0
    fi
    if [ -n "$PIN_VERSION" ]; then
        LATEST_VERSION=$PIN_VERSION
        note "pinned to $PIN_VERSION"
    elif [ "$OFFLINE" = 1 ]; then
        LATEST_VERSION=""
        note "--offline: not asking PyPI what is current"
    else
        LATEST_VERSION=$(latest_version 2>/dev/null || true)
        if [ -z "$LATEST_VERSION" ]; then
            # Reported, never fatal, and never silently read as "behind".
            warn "could not read the latest $PYPI_PACKAGE version from PyPI — proceeding without a comparison"
        fi
    fi

    if [ -z "$CLI_VERSION" ]; then
        CLI_ACTION=install
    elif [ "$FORCE" = 1 ]; then
        CLI_ACTION=upgrade
    elif [ -z "$LATEST_VERSION" ]; then
        CLI_ACTION=current
    elif version_lt "$CLI_VERSION" "$LATEST_VERSION"; then
        CLI_ACTION=upgrade
    else
        CLI_ACTION=current
    fi

    _resolve_system
    debug "cli=$CLI_ACTION tmux=$TMUX_ACTION gh=$GH_ACTION git=$GIT_ACTION node=$NODE_ACTION claude=$CLAUDE_ACTION"
}

# The System and Agent classes' decisions, which do not depend on how the CLI
# itself is being sourced.
_resolve_system() {
    # --- tmux: only below the floor (§3.9) ----------------------------------
    if [ -z "$TMUX_VERSION" ]; then
        TMUX_ACTION=install
    elif version_lt "$TMUX_VERSION" "$MIN_TMUX_MAJOR.$MIN_TMUX_MINOR"; then
        TMUX_ACTION=install
    else
        TMUX_ACTION=current
    fi

    # --- gh: any release works, so only ever "absent" -----------------------
    if [ -z "$GH_VERSION" ]; then GH_ACTION=install; else GH_ACTION=current; fi

    # --- git: same shape. Not in the plan's dependency table, and it belongs
    # in the System class all the same: the fleet gives every agent its own
    # `git worktree` (services/fleet.py::_git), so a machine without git can
    # register a project and never spawn an agent into one. One line of the
    # same `case`, warn-only like its neighbours. No doctor check measures it,
    # which is why the summary names it rather than leaving it to `doctor`.
    if [ -z "$GIT_VERSION" ]; then GIT_ACTION=install; else GIT_ACTION=current; fi

    # --- Node: absent or below the Repomix floor ----------------------------
    if [ -z "$NODE_VERSION" ]; then
        NODE_ACTION=install
    elif version_lt "$NODE_VERSION" "$MIN_NODE_MAJOR.0.0"; then
        NODE_ACTION=install
    else
        NODE_ACTION=current
    fi

    # --- Claude Code: never version-managed by us (§3.9.3) ------------------
    # It ships `claude update` and auto-updates by default, and the fleet needs
    # a 2.1.x FLOOR rather than an exact version. Pinning it would fight its own
    # updater and lose, leaving a machine that silently drifts from what this
    # script claims it installed.
    if [ -z "$CLAUDE_VERSION" ]; then
        CLAUDE_ACTION=install
    else
        CLAUDE_ACTION=update
    fi
}

# ---------------------------------------------------------------------------
#  6  short_circuit — a current machine does nothing at all (§3.9.4)
# ---------------------------------------------------------------------------

# Everything the script would touch is already in the state it would leave it.
#
# The test for "already configured" is `doctor`'s own verdict rather than a
# handful of file probes, for two reasons: it is exactly the acceptance criterion
# this whole feature is measured against (§8.3), and `doctor` is proven not to
# create the state it reports on (tests/test_doctor_does_not_create_state.py), so
# asking it costs no writes.
# A tilde inside quotes does not expand — which is exactly what is wanted here:
# these strings are DISPLAY TEXT telling a person where things went, and `~` is
# how a person writes that. shellcheck cannot tell a path being used from a path
# being described, so the rule is switched off for the printing functions only
# (never file-wide, where a real `cd "~/x"` bug would then hide).
# shellcheck disable=SC2088
short_circuit() {
    [ "$CLI_ACTION" = current ] || return 1
    [ "$INSTALL_TARGET" = "$PYPI_PACKAGE" ] || return 1
    [ "$FORCE" = 0 ] || return 1
    [ "$UPGRADE_ALL" = 0 ] || return 1
    [ "$TMUX_ACTION" = current ] || return 1
    [ "$GH_ACTION" = current ] || return 1
    [ "$GIT_ACTION" = current ] || return 1
    [ "$NODE_ACTION" = current ] || return 1
    # Only when we would have installed it: under --no-agent an absent Claude
    # Code IS the requested state, and demanding it here meant a second run
    # never short-circuited — measured in the container matrix, where every
    # cell passes --no-agent.
    [ "$WANT_AGENT" = 0 ] || [ -n "$CLAUDE_VERSION" ] || return 1
    have aisquare || return 1

    # A SUBSET, not an equal set. Measured on macOS, where the amber list came
    # back empty: the machine was HEALTHIER than expected and an equality test
    # refused to short-circuit because of it — exactly backwards. The condition
    # is "nothing amber that we did not expect"; fewer amber lines than expected
    # is good news and must never block the no-op path.
    _amber=$(doctor_amber 2>/dev/null || true)
    for _check in $_amber; do
        is_expected_amber "$_check" || return 1
    done

    say ""
    say "${C_BOLD}aisquare $CLI_VERSION is already the latest.${C_RESET}"
    note "uv $UV_VERSION · Claude Code ${CLAUDE_VERSION:-skipped} · tmux $TMUX_VERSION · gh $GH_VERSION · git $GIT_VERSION · Node $NODE_VERSION"
    if [ "$WANT_AGENT" = 1 ]; then
        note "~/.aisquare configured · claude-code hooks installed"
    else
        note "~/.aisquare configured (--no-agent: no agent hooks)"
    fi
    _why="gbrain is out of scope"
    if [ "$WANT_PROJECT" = 0 ] || [ -z "$PROJECT_DIR" ]; then
        _why="$_why; no project registered"
    fi
    if [ -n "$_amber" ]; then
        note "doctor: everything ok except $_amber ($_why)"
    else
        note "doctor: every check ok"
    fi
    say ""
    say "Nothing to do. Open the fleet UI with: ${C_BOLD}asq${C_RESET}"
    return 0
}

# ---------------------------------------------------------------------------
#  7  banner
# ---------------------------------------------------------------------------

# A tilde inside quotes does not expand — which is exactly what is wanted here:
# these strings are DISPLAY TEXT telling a person where things went, and `~` is
# how a person writes that. shellcheck cannot tell a path being used from a path
# being described, so the rule is switched off for the printing functions only
# (never file-wide, where a real `cd "~/x"` bug would then hide).
# shellcheck disable=SC2088
banner() {
    say ""
    say "${C_BOLD}aisquare installer${C_RESET} — this will:"

    _plan=""
    [ "$CLI_ACTION" = install ] && _plan="$_plan\n  install  Python $PYTHON_VERSION + $PYPI_PACKAGE + tiktoken (via uv, into its own venv)"
    [ "$CLI_ACTION" = upgrade ] && _plan="$_plan\n  upgrade  $PYPI_PACKAGE $CLI_VERSION -> ${LATEST_VERSION:-latest}"
    [ "$CLI_ACTION" = current ] && _plan="$_plan\n  keep     $PYPI_PACKAGE $CLI_VERSION"
    [ -z "$UV_VERSION" ] && _plan="$_plan\n  install  uv (the bootstrap: a static binary that brings its own Python)"

    if [ "$WANT_SYSTEM_DEPS" = 1 ]; then
        [ "$TMUX_ACTION" = install ] && _plan="$_plan\n  install  tmux (the fleet's substrate)"
        [ "$GH_ACTION" = install ] && _plan="$_plan\n  install  gh (the fleet's PR flow)"
        [ "$GIT_ACTION" = install ] && _plan="$_plan\n  install  git (the fleet's per-agent worktrees)"
        [ "$NODE_ACTION" = install ] && _plan="$_plan\n  install  Node $MIN_NODE_MAJOR+ (Repomix snapshots)"
    else
        _plan="$_plan\n  skip     tmux/gh/Node (--no-system-deps)"
    fi

    if [ "$WANT_AGENT" = 1 ]; then
        [ "$CLAUDE_ACTION" = install ] && _plan="$_plan\n  install  Claude Code"
        [ "$CLAUDE_ACTION" = update ] && _plan="$_plan\n  update   Claude Code $CLAUDE_VERSION (via its own updater)"
    else
        _plan="$_plan\n  skip     Claude Code (--no-agent)"
    fi

    if [ "$WANT_PROJECT" = 1 ] && [ -n "$PROJECT_DIR" ]; then
        _plan="$_plan\n  register $PROJECT_DIR as a project, and connect claude-code's hooks"
    else
        _plan="$_plan\n  set up   ~/.aisquare (no project registered)"
    fi

    # shellcheck disable=SC2059  # the format string is ours, built above.
    printf "$_plan\n"

    say ""
    say "Written to:"
    note "~/.local/bin/                     uv, aisquare, asq, claude"
    note "~/.local/share/uv/tools/          the $PYPI_PACKAGE tool environment"
    note "~/.aisquare/                      config.toml, context.db, projects/"
    note "~/.claude/settings.json           MERGED — aisquare's hook groups only"
    say ""

    if [ "$DRY_RUN" = 1 ]; then
        say "${C_BOLD}--dry-run: printing commands, running none.${C_RESET}"
        say ""
    fi
}

# ---------------------------------------------------------------------------
#  8  install_uv — the bootstrap (§3.1). FATAL on failure.
# ---------------------------------------------------------------------------

install_uv() {
    # Forced whatever happens next, including on the skip path, because the
    # traps below belong to the uv we USE, not the one we install.
    #
    #   UV_PYTHON_DOWNLOADS=automatic — measured trap: a distro-packaged uv can
    #   ship `python-downloads = manual` (Fedora's does), and the install then
    #   fails with "No interpreter found for Python 3.13 in search path or
    #   managed installations". A user who already has uv is the COMMON case,
    #   and their config is not ours to guess at, so the script forces the
    #   behaviour it relies on in its own environment and never writes to their
    #   uv config.
    export UV_PYTHON_DOWNLOADS=automatic
    # Not a TTY-backed installer; keep uv's output machine-plain.
    export UV_NO_PROGRESS=1

    if have uv; then
        step "uv $UV_VERSION already installed"
        if [ "$UPGRADE_ALL" = 1 ]; then
            note "--upgrade-all: moving uv itself"
            run uv self update || warn "uv self update failed — a distro-packaged uv updates through its own package manager"
        fi
        SKIPPED_LIST="$SKIPPED_LIST uv"
        return 0
    fi

    step "Installing uv (the bootstrap)"
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s fetch %s | sh\n' "$C_DIM" "$C_RESET" "$UV_INSTALLER_URL"
    elif ! fetch_into_shell "$UV_INSTALLER_URL" sh >/dev/null 2>&1; then
        die "the uv installer ($UV_INSTALLER_URL) failed.
uv is the bootstrap — it brings its own Python, so nothing else can proceed
without it. Install it another way (your package manager may have it) and
rerun, or install this CLI by hand with pipx: pipx install $PYPI_PACKAGE"
    fi

    # uv installs to ~/.local/bin and edits shell profiles for FUTURE shells;
    # this one needs it on PATH now.
    PATH="$HOME/.local/bin:$PATH"
    export PATH

    if [ "$DRY_RUN" = 0 ]; then
        have uv || die "uv installed but is not on PATH (looked in ~/.local/bin).
Open a new shell and rerun, or add it by hand:
  export PATH=\"\$HOME/.local/bin:\$PATH\""
        UV_VERSION=$(version_of_uv || true)
        good "uv $UV_VERSION"
        INSTALLED_LIST="$INSTALLED_LIST uv"
    fi
}

# ---------------------------------------------------------------------------
#  9  install_cli — Python + aisquare-cli + tiktoken (§3.9.1). FATAL.
# ---------------------------------------------------------------------------

install_cli() {
    _target=$INSTALL_TARGET
    [ -n "$PIN_VERSION" ] && [ "$INSTALL_TARGET" = "$PYPI_PACKAGE" ] &&
        _target="$PYPI_PACKAGE==$PIN_VERSION"

    case "$CLI_ACTION" in
        current)
            step "$PYPI_PACKAGE $CLI_VERSION is current"
            SKIPPED_LIST="$SKIPPED_LIST $PYPI_PACKAGE"
            return 0
            ;;
        install)
            step "Installing $PYPI_PACKAGE (Python $PYTHON_VERSION, with tiktoken)"
            # `uv tool install` with no version change is already a safe no-op —
            # measured: "`aisquare-cli` is already installed", exit 0 — so
            # --force is NOT used here. It is only for the upgrade path below.
            if ! run uv tool install --python "$PYTHON_VERSION" --with tiktoken "$_target"; then
                die "uv tool install $_target failed.
Rerun with --verbose to see uv's own output."
            fi
            INSTALLED_LIST="$INSTALLED_LIST $PYPI_PACKAGE"
            ;;
        upgrade)
            step "Upgrading $PYPI_PACKAGE $CLI_VERSION -> ${LATEST_VERSION:-latest}"
            # NEVER `uv tool upgrade`. Measured (§3.9.1): a tool installed with
            # an exact pin is NOT moved by it — "Nothing to upgrade", exit 0,
            # and a machine still on the old version. A script that trusted that
            # exit code would report an upgrade that did not happen, which is
            # the precise failure this step exists to prevent.
            #
            # `install --force … @latest` moves a pinned install, RE-STATES
            # `--with tiktoken` so the extra cannot be silently dropped, and is
            # deterministic regardless of how the existing install was made.
            if [ "$INSTALL_TARGET" != "$PYPI_PACKAGE" ]; then
                # `@latest` is meaningless for a path, and appending it makes uv
                # treat the whole thing as a package name.
                _spec=$INSTALL_TARGET
            else
                _spec="$PYPI_PACKAGE@latest"
                [ -n "$PIN_VERSION" ] && _spec="$PYPI_PACKAGE@$PIN_VERSION"
            fi
            if ! run uv tool install --force --python "$PYTHON_VERSION" \
                --with tiktoken "$_spec"; then
                die "uv tool install --force $_spec failed.
The previous install is untouched. Rerun with --verbose for uv's output."
            fi
            UPGRADED_LIST="$UPGRADED_LIST $PYPI_PACKAGE"
            ;;
    esac

    [ "$DRY_RUN" = 1 ] && return 0

    # PATH again: a first-ever uv install put ~/.local/bin there, but a machine
    # that already had uv may not have it.
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *)
            PATH="$HOME/.local/bin:$PATH"
            export PATH
            ;;
    esac

    have aisquare || die "$PYPI_PACKAGE installed but \`aisquare\` is not on PATH.
uv puts it in ~/.local/bin; add that to your PATH and rerun:
  export PATH=\"\$HOME/.local/bin:\$PATH\""

    _now=$(version_of_aisquare || true)
    # The upgrade is VERIFIED and not assumed, because §3.9.1's failure mode is
    # a successful exit code over an unchanged version.
    if [ "$CLI_ACTION" = upgrade ] && [ -n "$LATEST_VERSION" ] && [ "$_now" != "$LATEST_VERSION" ]; then
        die "the upgrade reported success but \`aisquare --version\` still says
$_now, not $LATEST_VERSION. That is the silent no-op described in
docs/plans/one-line-install.md §3.9.1 — please report it."
    fi
    CLI_VERSION=$_now
    good "aisquare $CLI_VERSION"
}

# ---------------------------------------------------------------------------
# 10  path_check (§3.6)
# ---------------------------------------------------------------------------

PATH_HINT=""

# A tilde inside quotes does not expand — which is exactly what is wanted here:
# these strings are DISPLAY TEXT telling a person where things went, and `~` is
# how a person writes that. shellcheck cannot tell a path being used from a path
# being described, so the rule is switched off for the printing functions only
# (never file-wide, where a real `cd "~/x"` bug would then hide).
# shellcheck disable=SC2088
path_check() {
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) return 0 ;;
    esac
    # Said, not silently fixed: uv and the Claude installer each manage their own
    # PATH entry, and rewriting someone's .zshrc on top of that is not this
    # script's business (§3.6).
    PATH_HINT="export PATH=\"\$HOME/.local/bin:\$PATH\""
    warn "~/.local/bin is not on your PATH in this shell. Add this to your shell profile:
         $PATH_HINT"
}

# ---------------------------------------------------------------------------
# 11-13  the System class — tmux, gh, Node. WARN-ONLY (§3.2).
# ---------------------------------------------------------------------------

# True when we have a package manager we can actually install with. On macOS
# without Homebrew this asks first (§5): it is a large thing to put on someone's
# machine unasked, and macOS is where its absence is most likely.
ensure_pkg_manager() {
    [ -n "$PKG" ] && return 0
    if [ "$OS" != macos ]; then
        return 1
    fi
    say ""
    note "tmux and gh have no sane source on macOS other than Homebrew, and it is not installed."
    if ! confirm "Install Homebrew? (declining just skips tmux/gh)" n; then
        note "leaving Homebrew alone"
        return 1
    fi
    step "Installing Homebrew"
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s the Homebrew installer\n' "$C_DIM" "$C_RESET"
        return 1
    fi
    if ! fetch_into_shell \
        "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh" bash; then
        warn "the Homebrew installer failed — skipping tmux/gh"
        return 1
    fi
    # Apple silicon puts it in /opt/homebrew, Intel in /usr/local.
    for _prefix in /opt/homebrew /usr/local; do
        if [ -x "$_prefix/bin/brew" ]; then
            PATH="$_prefix/bin:$PATH"
            export PATH
            break
        fi
    done
    have brew || {
        warn "Homebrew installed but \`brew\` is not on PATH — skipping tmux/gh"
        return 1
    }
    PKG=brew
    INSTALLED_LIST="$INSTALLED_LIST homebrew"
    return 0
}

# Install one package with whatever manager this machine has. The `case` that
# §2 says is the only OS-varying part of the script.
pkg_install() {
    _package=$1
    case "$PKG" in
        apt)
            sudo_run apt-get update || true
            sudo_run apt-get install -y "$_package"
            ;;
        dnf) sudo_run dnf install -y "$_package" ;;
        pacman) sudo_run pacman -S --noconfirm --needed "$_package" ;;
        zypper) sudo_run zypper --non-interactive install "$_package" ;;
        apk) sudo_run apk add --no-cache "$_package" ;;
        brew) run brew install "$_package" ;;
        *) return 1 ;;
    esac
}

# The manual command, for the summary when we could not run it ourselves. Mirrors
# services/diagnostics.py install_hint(), which gives ALL THREE hints rather
# than a wrong one when it does not recognise the platform.
pkg_hint() {
    case "$PKG" in
        apt) printf 'sudo apt install %s' "$1" ;;
        dnf) printf 'sudo dnf install %s' "$1" ;;
        pacman) printf 'sudo pacman -S %s' "$1" ;;
        zypper) printf 'sudo zypper install %s' "$1" ;;
        apk) printf 'sudo apk add %s' "$1" ;;
        brew) printf 'brew install %s' "$1" ;;
        *) printf 'apt install %s / dnf install %s / brew install %s' "$1" "$1" "$1" ;;
    esac
}

install_tmux() {
    [ "$WANT_SYSTEM_DEPS" = 1 ] || return 0
    if [ "$TMUX_ACTION" = current ]; then
        if version_lt "$TMUX_VERSION" "$MIN_TMUX_MAJOR.$RECOMMENDED_TMUX_MINOR"; then
            note "tmux $TMUX_VERSION — fleet available ($MIN_TMUX_MAJOR.$RECOMMENDED_TMUX_MINOR+ adds Shift+Enter in agent panes)"
        else
            note "tmux $TMUX_VERSION — current"
        fi
        SKIPPED_LIST="$SKIPPED_LIST tmux"
        return 0
    fi

    if ! ensure_pkg_manager; then
        warn "no package manager found for tmux — the fleet UI needs it; everything else works.
         Install it with: $(pkg_hint tmux)"
        return 0
    fi

    if [ -n "$TMUX_VERSION" ]; then
        step "Upgrading tmux $TMUX_VERSION (below the $MIN_TMUX_MAJOR.$MIN_TMUX_MINOR floor)"
    else
        step "Installing tmux"
    fi
    if pkg_install tmux; then
        TMUX_VERSION=$(version_of_tmux || true)
        if [ -n "$TMUX_VERSION" ] && version_lt "$TMUX_VERSION" "$MIN_TMUX_MAJOR.$MIN_TMUX_MINOR"; then
            # Honest about the one thing a package manager cannot fix.
            warn "this distribution's tmux is $TMUX_VERSION, below the $MIN_TMUX_MAJOR.$MIN_TMUX_MINOR the fleet needs.
         Everything except the fleet UI works. Build a newer tmux, or use a newer release."
        else
            good "tmux ${TMUX_VERSION:-installed}"
            INSTALLED_LIST="$INSTALLED_LIST tmux"
        fi
    else
        warn "could not install tmux — the fleet UI needs it; everything else works.
         Install it with: $(pkg_hint tmux)"
    fi
}

install_gh() {
    [ "$WANT_SYSTEM_DEPS" = 1 ] || return 0
    if [ "$GH_ACTION" = current ]; then
        note "gh $GH_VERSION — current (any release works)"
        SKIPPED_LIST="$SKIPPED_LIST gh"
        return 0
    fi
    if ! ensure_pkg_manager; then
        warn "no package manager found for gh — only the fleet's PR flow needs it.
         Install it with: $(pkg_hint gh)"
        return 0
    fi

    step "Installing gh (GitHub CLI)"
    # apt and dnf need a repository added first; the others carry it themselves.
    case "$PKG" in
        apt) _gh_add_apt_repo || true ;;
        dnf) _gh_add_dnf_repo || true ;;
    esac

    _gh_package=gh
    case "$PKG" in
        pacman) _gh_package=github-cli ;;
        apk) _gh_package=github-cli ;;
    esac

    if pkg_install "$_gh_package"; then
        GH_VERSION=$(version_of_gh || true)
        good "gh ${GH_VERSION:-installed}"
        INSTALLED_LIST="$INSTALLED_LIST gh"
    else
        warn "could not install gh — only the fleet's PR flow needs it.
         Install it with: $(pkg_hint gh)"
    fi
}

_gh_add_apt_repo() {
    # github.com/cli/cli/blob/trunk/docs/install_linux.md, not from memory.
    [ "$DRY_RUN" = 1 ] && {
        printf '  %swould run:%s add the cli.github.com apt repository\n' "$C_DIM" "$C_RESET"
        return 0
    }
    have gpg || pkg_install gpg || true
    sudo_run mkdir -p -m 755 /etc/apt/keyrings || return 1
    _key=/etc/apt/keyrings/githubcli-archive-keyring.gpg
    if [ ! -f "$_key" ]; then
        fetch https://cli.github.com/packages/githubcli-archive-keyring.gpg |
            ${PKG_SUDO:+sudo }tee "$_key" >/dev/null || return 1
        sudo_run chmod go+r "$_key" || return 1
    fi
    _arch=$(dpkg --print-architecture 2>/dev/null || echo amd64)
    printf 'deb [arch=%s signed-by=%s] https://cli.github.com/packages stable main\n' \
        "$_arch" "$_key" |
        ${PKG_SUDO:+sudo }tee /etc/apt/sources.list.d/github-cli.list >/dev/null || return 1
    sudo_run apt-get update || true
}

_gh_add_dnf_repo() {
    [ "$DRY_RUN" = 1 ] && {
        printf '  %swould run:%s add the cli.github.com dnf repository\n' "$C_DIM" "$C_RESET"
        return 0
    }
    # dnf5 spells this `addrepo --from-repofile=`; dnf4 spells it `--add-repo`
    # and needs the config-manager plugin installed first. Fedora 41+ is dnf5;
    # RHEL 9 and derivatives are dnf4. Branching on the MAJOR rather than on
    # whether a command errors, so a failure is a failure and not a fallback.
    if [ "${DNF_MAJOR:-4}" -ge 5 ]; then
        sudo_run dnf install -y dnf5-plugins || true
        sudo_run dnf config-manager addrepo \
            --overwrite --from-repofile=https://cli.github.com/packages/rpm/gh-cli.repo
    else
        sudo_run dnf install -y 'dnf-command(config-manager)' || true
        sudo_run dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
    fi
}

install_git() {
    [ "$WANT_SYSTEM_DEPS" = 1 ] || return 0
    if [ "$GIT_ACTION" = current ]; then
        note "git $GIT_VERSION — current (any release works)"
        SKIPPED_LIST="$SKIPPED_LIST git"
        return 0
    fi
    if ! ensure_pkg_manager; then
        warn "no package manager found for git — the fleet's per-agent worktrees need it.
         Install it with: $(pkg_hint git)"
        return 0
    fi
    step "Installing git"
    if pkg_install git; then
        GIT_VERSION=$(version_of_git || true)
        good "git ${GIT_VERSION:-installed}"
        INSTALLED_LIST="$INSTALLED_LIST git"
    else
        warn "could not install git — the fleet's per-agent worktrees need it.
         Install it with: $(pkg_hint git)"
    fi
}

# Node is the awkward one, and it gets stated rules rather than a guess.
#
# THREE SOURCES, tried in order, each with a measured reason for its place:
#
#  1. the platform package, when it is new enough. Free, system-wide, and no
#     third party involved. Arch and Alpine 3.22 ship Node >= 22, so this is the
#     whole story there.
#  2. NodeSource, for apt and dnf. Debian 12 ships Node 18 and Ubuntu 22.04
#     ships 12 (measured, §1.4), and no amount of `apt install nodejs` moves
#     that — so on exactly the platforms this floor exists for, the platform
#     package CANNOT be the answer. NodeSource is Node's own distribution
#     channel for those platforms, it installs system-wide, and it needs no
#     shell hook. Same shape as the `gh` step: add the vendor repository, then
#     install from it.
#  3. fnm, last. It is a single static binary and needs no root, which is why it
#     is here at all — but it needs `unzip` (measured: the installer refuses on
#     a bare Debian without it) and, worse, its Node is only on PATH in shells
#     that have run `fnm env`. That means a machine where `doctor` reports "no
#     node on PATH" right after a successful install, which is a bad outcome for
#     a one-line installer. So it is the fallback, not the plan.
#
# Never `sudo npm install -g` anywhere: the Claude Code docs call it out as a
# security risk and it is how ~/.npm ends up root-owned.
install_node() {
    [ "$WANT_SYSTEM_DEPS" = 1 ] || return 0
    if [ "$NODE_ACTION" = current ]; then
        note "Node $NODE_VERSION — current (Repomix needs $MIN_NODE_MAJOR+)"
        _ensure_npx || true
        SKIPPED_LIST="$SKIPPED_LIST node"
        return 0
    fi

    if [ -n "$NODE_VERSION" ]; then
        step "Node $NODE_VERSION is below Repomix's $MIN_NODE_MAJOR floor"
    else
        step "Installing Node (Repomix needs $MIN_NODE_MAJOR+)"
    fi

    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s install Node %s+ (platform package, else NodeSource, else fnm)\n' \
            "$C_DIM" "$C_RESET" "$MIN_NODE_MAJOR"
        return 0
    fi

    # --- 1. the platform package ------------------------------------------
    if ensure_pkg_manager; then
        _node_package=nodejs
        [ "$PKG" = brew ] && _node_package=node
        pkg_install "$_node_package" || true
        _reread_node
        if _node_ok; then
            _ensure_npx || true
            good "Node $NODE_VERSION"
            INSTALLED_LIST="$INSTALLED_LIST node"
            return 0
        fi
        note "this distribution's Node is ${NODE_VERSION:-unreadable} — too old for Repomix"

        # --- 2. NodeSource, on the two platforms that need it -------------
        case "$PKG" in
            apt | dnf)
                step "Adding Node's own repository (NodeSource) for Node $MIN_NODE_MAJOR"
                if _nodesource_setup && pkg_install "$_node_package"; then
                    _reread_node
                    if _node_ok; then
                        _ensure_npx || true
                        good "Node $NODE_VERSION (NodeSource)"
                        INSTALLED_LIST="$INSTALLED_LIST node"
                        return 0
                    fi
                fi
                note "NodeSource did not produce a Node $MIN_NODE_MAJOR+ — trying fnm"
                ;;
        esac
    fi

    # --- 3. fnm ------------------------------------------------------------
    _install_node_via_fnm
}

# True when the surveyed Node is at or above the floor.
_node_ok() {
    [ -n "$NODE_VERSION" ] || return 1
    ! version_lt "$NODE_VERSION" "$MIN_NODE_MAJOR.0.0"
}

# Re-read `node --version` after a package operation. `command -v` caches on some
# shells, so the hash is dropped first — without it a freshly installed node is
# invisible for the rest of the run.
_reread_node() {
    hash -r 2>/dev/null || true
    NODE_VERSION=$(version_of_node || true)
}

# Repomix is reached through `npx`, and on several distributions npm is a
# SEPARATE package from nodejs (Arch, Alpine, Debian's own nodejs). A Node 22
# with no npx leaves the `repomix` doctor check amber for a reason that reads
# like a Node problem and is not one — so npx is checked, and npm installed
# beside node when it is missing.
_ensure_npx() {
    have npx && return 0
    [ -n "$PKG" ] || return 1
    debug "node is present but npx is not — installing npm"
    pkg_install npm || return 1
    hash -r 2>/dev/null || true
    have npx
}

# Fetch and run NodeSource's own setup script. From a FILE rather than a pipe,
# deliberately: this is the one thing here that runs as root, and a script on
# disk can be read afterwards to see what did.
_nodesource_setup() {
    case "$PKG" in
        apt) _ns_url=$(printf '%s' "$NODESOURCE_DEB_URL" | sed "s/NODEMAJOR/$MIN_NODE_MAJOR/") ;;
        dnf) _ns_url=$(printf '%s' "$NODESOURCE_RPM_URL" | sed "s/NODEMAJOR/$MIN_NODE_MAJOR/") ;;
        *) return 1 ;;
    esac
    have bash || {
        debug "NodeSource's setup script needs bash"
        return 1
    }
    _ns_file="${TMPDIR:-/tmp}/aisquare-nodesource-$$.sh"
    if ! fetch "$_ns_url" >"$_ns_file" 2>/dev/null; then
        rm -f "$_ns_file"
        return 1
    fi
    if sudo_run bash "$_ns_file"; then
        rm -f "$_ns_file"
        return 0
    fi
    rm -f "$_ns_file"
    return 1
}

_install_node_via_fnm() {
    step "Installing Node $MIN_NODE_MAJOR via fnm"
    if ! have bash; then
        warn "fnm's installer needs bash, which is not on this machine.
         Codebase snapshots (Repomix) need Node $MIN_NODE_MAJOR+; everything else works.
         Install Node $MIN_NODE_MAJOR+ with: $(pkg_hint nodejs)"
        return 0
    fi
    # Measured: fnm's installer checks for `unzip` and refuses without it, and a
    # bare Debian has none. Supplying it is cheaper than the confusing failure.
    if ! have unzip && [ -n "$PKG" ]; then
        pkg_install unzip || true
        hash -r 2>/dev/null || true
    fi
    if ! fetch_into_shell "$FNM_INSTALLER_URL" bash --skip-shell >/dev/null 2>&1; then
        warn "the fnm installer failed. Codebase snapshots (Repomix) need Node $MIN_NODE_MAJOR+;
         everything else works. Install Node $MIN_NODE_MAJOR+ yourself and rerun."
        return 0
    fi
    for _dir in "$HOME/.local/share/fnm" "$HOME/.fnm"; do
        if [ -x "$_dir/fnm" ]; then
            PATH="$_dir:$PATH"
            export PATH
            break
        fi
    done
    if ! have fnm; then
        warn "fnm installed but is not on PATH — install Node $MIN_NODE_MAJOR+ yourself; snapshots need it."
        return 0
    fi
    if run fnm install "$MIN_NODE_MAJOR" && run fnm default "$MIN_NODE_MAJOR"; then
        # fnm's shims live in a multishell dir that only a shell hook sets up, so
        # the alias directory is put on PATH for the rest of THIS run — that is
        # what lets `aisquare init` below pack a snapshot.
        for _alias in "$HOME/.local/share/fnm/aliases/default/bin" "$HOME/.fnm/aliases/default/bin"; do
            if [ -x "$_alias/node" ]; then
                PATH="$_alias:$PATH"
                export PATH
                break
            fi
        done
        _reread_node
        good "Node ${NODE_VERSION:-$MIN_NODE_MAJOR} via fnm"
        INSTALLED_LIST="$INSTALLED_LIST node(fnm)"
        warn "fnm's Node is only on PATH in shells that have run its hook. Add to your profile:
         eval \"\$(fnm env --use-on-cd)\"
         Until then \`aisquare doctor\` will report no node on PATH."
    else
        warn "fnm could not install Node $MIN_NODE_MAJOR — snapshots need it; everything else works."
    fi
}

# ---------------------------------------------------------------------------
# 14  install_claude — the Agent class. WARN-ONLY (§3.2).
# ---------------------------------------------------------------------------

install_claude() {
    [ "$WANT_AGENT" = 1 ] || {
        note "skipping Claude Code (--no-agent)"
        return 0
    }

    if [ "$CLAUDE_ACTION" = update ]; then
        step "Claude Code $CLAUDE_VERSION — letting its own updater run"
        # §3.9.3: never version-managed by us. `claude update` is the tool's own
        # command and the fleet needs a floor, not an exact version.
        if run claude update; then
            CLAUDE_VERSION=$(version_of_claude || true)
            good "Claude Code $CLAUDE_VERSION"
        else
            note "claude update did not report a change — Claude Code auto-updates itself anyway"
        fi
        SKIPPED_LIST="$SKIPPED_LIST claude-code"
        return 0
    fi

    step "Installing Claude Code"
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s fetch %s | bash\n' "$C_DIM" "$C_RESET" "$CLAUDE_INSTALLER_URL"
        return 0
    fi
    if ! have bash; then
        warn "Claude Code's installer needs bash, which is not on this machine.
         The CLI works; the fleet has nothing to spawn. Install bash, then:
         curl -fsSL $CLAUDE_INSTALLER_URL | bash"
        return 0
    fi
    if ! fetch_into_shell "$CLAUDE_INSTALLER_URL" bash >/dev/null 2>&1; then
        warn "the Claude Code installer failed. The CLI works; the fleet has nothing
         to spawn until an agent is installed. See https://claude.com/claude-code"
        return 0
    fi
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    CLAUDE_VERSION=$(version_of_claude || true)
    if [ -n "$CLAUDE_VERSION" ]; then
        good "Claude Code $CLAUDE_VERSION"
        INSTALLED_LIST="$INSTALLED_LIST claude-code"
    else
        warn "Claude Code installed but \`claude\` is not on PATH yet. Open a new shell."
    fi
}

# ---------------------------------------------------------------------------
# 15  init (§3.4) — NEVER --reinit
# ---------------------------------------------------------------------------

# A tilde inside quotes does not expand — which is exactly what is wanted here:
# these strings are DISPLAY TEXT telling a person where things went, and `~` is
# how a person writes that. shellcheck cannot tell a path being used from a path
# being described, so the rule is switched off for the printing functions only
# (never file-wide, where a real `cd "~/x"` bug would then hide).
# shellcheck disable=SC2088
init_home() {
    step "Setting up ~/.aisquare"

    # --agent claude-code installs the five lifecycle hooks and ingests
    # ~/.claude/CLAUDE.md; without --no-onboard it also packs the Repomix
    # snapshot in the same run. That is `claude-code` and `snapshot` fixed in one
    # command (§4).
    #
    # NEVER --reinit. It is exactly the flag a script author reaches for to make
    # a step "clean", and here it resets config.toml and DISCARDS the role
    # bindings made with `team bind` — silently destroying user configuration on
    # every re-run.
    set -- init --local --yes
    [ "$WANT_AGENT" = 1 ] && set -- "$@" --agent claude-code
    if [ "$WANT_PROJECT" = 1 ] && [ -n "$PROJECT_DIR" ]; then
        set -- "$@" "$PROJECT_DIR"
    else
        set -- "$@" --no-onboard
    fi

    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s aisquare %s\n' "$C_DIM" "$C_RESET" "$*"
        return 0
    fi
    if ! run aisquare "$@"; then
        die "\`aisquare $*\` failed. Rerun with --verbose to see its output."
    fi
    if [ "$WANT_PROJECT" = 1 ] && [ -n "$PROJECT_DIR" ]; then
        good "~/.aisquare set up, $PROJECT_DIR registered"
    else
        good "~/.aisquare set up"
    fi
}

# ---------------------------------------------------------------------------
# 16  doctor (§3.8)
# ---------------------------------------------------------------------------

DOCTOR_RAW=""
DOCTOR_AMBER=""

# The checks that SHOULD be amber when this run finishes, sorted.
#
# `brain` always. And `snapshot` whenever no project was registered — which is
# not a defect but the state `--no-project` asks for, and the state a run from a
# directory that is not a git repo lands in (§4). Measured: with the set
# hardcoded to `brain`, a `--no-project` machine could NEVER reach §3.9.4's
# "nothing to do" however current it was, because `snapshot` was always there —
# the short-circuit was unreachable in that whole mode, and the summary told the
# user to run `project onboard` for a project that does not exist.
expected_amber() {
    if [ "$WANT_PROJECT" = 1 ] && [ -n "$PROJECT_DIR" ]; then
        printf '%s' "$EXPECTED_AMBER"
    else
        # Sorted, to match doctor_amber's own ordering.
        printf '%s' "$EXPECTED_AMBER snapshot"
    fi
}

# True when $1 is one of the names `expected_amber` returns.
is_expected_amber() {
    for _expected in $(expected_amber); do
        [ "$1" = "$_expected" ] && return 0
    done
    return 1
}

# The names of every check that is not ok, SORTED and space-separated.
#
# Sorted so a comparison is about the SET rather than the order checks happen to
# run in — a reordering inside `doctor()` is not a regression and must not read
# as one. `--json` and not the rendered table: that is Rich output wrapped to
# terminal width, which is a bad parsing target for the same reason
# tests/test_documented_commands.py refuses to read --help.
#
# TWO `-e` EXPRESSIONS RATHER THAN `\(warn\|fail\)`, and that is not a style
# choice. `\|` alternation in a BASIC regular expression is a GNU extension:
# GNU sed has it, BusyBox sed has it, and **BSD sed — which is macOS's sed —
# does not**. There it matches the literal text `warn|fail`, so the amber list
# came back EMPTY on every Mac. Measured in CI the first time this ran on
# macos-latest: `doctor: 17 checks, 0 not ok` on a machine whose gbrain is
# absent. That is the worst failure this function has — an installer that calls
# every Mac perfectly healthy, never short-circuits, and can never surface an
# unexpected check, which is exactly what §3.8 exists to prevent. Every
# container cell in the matrix passed it, because none of them is a Mac.
doctor_amber() {
    _raw=$(aisquare --json doctor 2>/dev/null || true)
    [ -n "$_raw" ] || return 1
    printf '%s' "$_raw" |
        tr '{' '\n' |
        sed -n \
            -e 's/.*"name": *"\([^"]*\)".*"status": *"warn".*/\1/p' \
            -e 's/.*"name": *"\([^"]*\)".*"status": *"fail".*/\1/p' |
        sort |
        tr '\n' ' ' |
        sed 's/  */ /g; s/^ //; s/ $//'
}

# How many checks the payload claims to hold, so a parse that silently dropped
# one is caught rather than reported as health (§3.8's whole point).
doctor_count() {
    printf '%s' "$1" | tr '{' '\n' | grep -c '"name": *"' || true
}

run_doctor() {
    step "Verifying with aisquare doctor"
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %swould run:%s aisquare --json doctor\n' "$C_DIM" "$C_RESET"
        return 0
    fi

    DOCTOR_RAW=$(aisquare --json doctor 2>/dev/null || true)
    if [ -z "$DOCTOR_RAW" ]; then
        warn "\`aisquare --json doctor\` produced no output — cannot verify this install."
        UNEXPECTED=$((UNEXPECTED + 1))
        return 0
    fi

    _total=$(doctor_count "$DOCTOR_RAW")
    _parsed=$(printf '%s' "$DOCTOR_RAW" | tr '{' '\n' | grep -c '"status": *"' || true)
    if [ "$_total" != "$_parsed" ]; then
        # A `{` inside a check's detail string would split an object across two
        # lines and lose it. Reported rather than papered over: a script that
        # says "all green" over checks it could not read is worse than one that
        # admits it. Run `aisquare doctor` yourself is always the fallback.
        warn "read $_parsed of $_total doctor checks — verify by hand with: aisquare doctor"
        UNEXPECTED=$((UNEXPECTED + 1))
        return 0
    fi

    DOCTOR_AMBER=$(doctor_amber || true)
    note "doctor: $_total checks, $(printf '%s' "$DOCTOR_AMBER" | wc -w | tr -d ' ') not ok"
}

# ---------------------------------------------------------------------------
# 17  summary (§3.8) — three kinds of amber, never collapsed into one
# ---------------------------------------------------------------------------

# Actionable by the user: a real credential step this script deliberately does
# not take (§3.6). Each gets the one command that fixes it.
# shellcheck disable=SC2016  # the backticks are markdown for the reader, not
# a command substitution — this string is printed, never evaluated.
_actionable_fix() {
    case "$1" in
        gh) printf 'gh auth login' ;;
        claude-code) printf 'run `claude` once to authenticate it' ;;
        snapshot) printf 'aisquare project onboard' ;;
        *) printf '' ;;
    esac
}

summary() {
    say ""
    say "${C_BOLD}Done.${C_RESET}"

    [ -n "$INSTALLED_LIST" ] && note "installed:$INSTALLED_LIST"
    [ -n "$UPGRADED_LIST" ] && note "upgraded:$UPGRADED_LIST"
    [ -n "$SKIPPED_LIST" ] && note "already current:$SKIPPED_LIST"

    if [ "$DRY_RUN" = 1 ]; then
        say ""
        say "--dry-run: nothing above was actually run."
        return 0
    fi

    _expected=""
    _actionable=""
    _unexpected=""
    for _check in $DOCTOR_AMBER; do
        if is_expected_amber "$_check"; then
            _expected="$_expected $_check"
            continue
        fi
        _fix=$(_actionable_fix "$_check")
        if [ -n "$_fix" ]; then
            _actionable="$_actionable $_check"
        else
            _unexpected="$_unexpected $_check"
            UNEXPECTED=$((UNEXPECTED + 1))
        fi
    done

    if [ -n "$_expected" ]; then
        say ""
        note "expected:$_expected"
        note "  brain    — gbrain is out of scope for this installer; team"
        note "             decisions are simply not distilled without it."
        case " $_expected " in
            *" snapshot "*)
                # NOT "run project onboard": there is no project to onboard.
                # Advice that cannot work is worse than no advice.
                note "  snapshot — no project is registered yet. From a git repo, run:"
                note "             aisquare init"
                ;;
        esac
    fi

    if [ -n "$_actionable" ]; then
        say ""
        say "Two minutes of your own, and these go green:"
        for _check in $_actionable; do
            printf '  %s — %s\n' "$_check" "$(_actionable_fix "$_check")"
        done
    fi

    if [ -n "$_unexpected" ]; then
        say ""
        say "${C_YELLOW}Not expected, and worth a look:${C_RESET}"
        for _check in $_unexpected; do
            printf '  %s\n' "$_check"
        done
        note "the full detail and a fix for each: aisquare doctor"
    fi

    if [ -n "$PATH_HINT" ]; then
        say ""
        say "Add to your shell profile so new shells find aisquare:"
        note "$PATH_HINT"
    fi
}

# ---------------------------------------------------------------------------
# 18  handoff (§3.3, §0.5)
# ---------------------------------------------------------------------------

handoff() {
    _exit=0
    [ "$UNEXPECTED" -gt 0 ] && _exit=2
    # Exit 2, not 0: the install completed but an unexpected check is amber. A
    # script that exits 0 onto a broken machine is worse than one that never ran.

    if [ "$DRY_RUN" = 1 ]; then
        exit "$_exit"
    fi

    if ! have asq; then
        say ""
        say "Open the fleet UI with: ${C_BOLD}asq${C_RESET}"
        exit "$_exit"
    fi

    # --yes means "do not stop to ask me", so it does NOT launch the UI (§3.5) —
    # a provisioning run must not end in a full-screen TUI.
    if [ "$ASSUME_YES" = 1 ] || ! tty_available; then
        say ""
        say "Open the fleet UI with: ${C_BOLD}asq${C_RESET}"
        exit "$_exit"
    fi

    say ""
    if ! confirm "Open the aisquare fleet UI now?" y; then
        say ""
        say "Open it any time with: ${C_BOLD}asq${C_RESET}"
        exit "$_exit"
    fi

    # `exec … < /dev/tty` is the whole point of §3.3, twice over. Only STDIN is
    # the pipe carrying this script's bytes — stdout and stderr are already the
    # terminal — so the UI needs its stdin reconnected to the terminal or it
    # gets the remains of this script. And `exec` rather than a plain call, so
    # the UI REPLACES this shell instead of running as its child with a pipe
    # still attached.
    #
    # Measured on 0.6.0, which is why the guard above is not optional: bare
    # `aisquare` with a piped stdin prints the usage page and exits 2. An
    # installer that got this wrong would end by printing a help page and
    # reporting failure.
    exec asq </dev/tty
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

usage() {
    cat <<'USAGE'
aisquare installer — one command to a working install and the fleet UI.

  curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh

With options, note the `-s --` (sh reads the script on stdin, so the options
have to be handed to sh explicitly):

  curl -fsSL .../install.sh | sh -s -- --yes --no-agent

Options
  -y, --yes            Never prompt, and do not launch the UI at the end.
      --no-agent       Skip Claude Code.
      --no-system-deps Skip tmux, gh and Node.
      --project DIR    Register DIR as the project (default: $PWD if a git repo).
      --no-project     Machine setup only; register nothing.
      --version V      Pin aisquare-cli to V (default: latest).
      --python V       Interpreter uv resolves for the tool env (default: 3.13).
      --offline        Do not ask PyPI what the latest version is.
      --upgrade-all    Also move uv itself and the system packages.
      --force          Reinstall aisquare-cli even when the version matches.
      --dry-run        Print every command; run none.
  -v, --verbose        Stream the sub-installers' output.
  -h, --help           This.

Environment
  AISQUARE_INSTALL_YES=1        same as --yes
  AISQUARE_INSTALL_VERSION=V    same as --version V
  AISQUARE_INSTALL_ALLOW_ROOT=1 permit a root install (containers do this anyway)
  AISQUARE_INSTALL_PACKAGE=P     install P instead of aisquare-cli — a wheel,
                                sdist or project directory. For testing this
                                script against a local build; turns the PyPI
                                version comparison off.
  NO_COLOR=1                    no ANSI colour

Exit codes
  0  installed (or nothing to do), with nothing unexpected
  1  a fatal step failed — uv, or aisquare-cli itself
  2  installed, but a check is amber for a reason this script did not expect

Not installed, on purpose: gbrain (out of scope), explainability tracing (off
unless asked for), and any credential — `claude` and `gh auth login` do their
own. Full design and measurements: docs/plans/one-line-install.md
USAGE
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -y | --yes) ASSUME_YES=1 ;;
            --no-agent) WANT_AGENT=0 ;;
            --no-system-deps) WANT_SYSTEM_DEPS=0 ;;
            --no-project) WANT_PROJECT=0 ;;
            --project)
                [ $# -ge 2 ] || die "--project needs a directory"
                PROJECT_DIR=$2
                shift
                ;;
            --project=*) PROJECT_DIR=${1#--project=} ;;
            --version)
                [ $# -ge 2 ] || die "--version needs a version"
                PIN_VERSION=$2
                shift
                ;;
            --version=*) PIN_VERSION=${1#--version=} ;;
            --python)
                [ $# -ge 2 ] || die "--python needs a version"
                PYTHON_VERSION=$2
                shift
                ;;
            --python=*) PYTHON_VERSION=${1#--python=} ;;
            --offline) OFFLINE=1 ;;
            --upgrade-all) UPGRADE_ALL=1 ;;
            --force) FORCE=1 ;;
            --dry-run) DRY_RUN=1 ;;
            -v | --verbose) VERBOSE=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            --self-version)
                say "$AISQUARE_INSTALL_VERSION_SELF"
                exit 0
                ;;
            *)
                # Refused rather than ignored: a typo'd flag that is silently
                # dropped is how someone ends up thinking --no-agent worked.
                printf 'unknown option: %s\n\n' "$1" >&2
                usage >&2
                exit 64
                ;;
        esac
        shift
    done
}

# Which project to register (§4): $PWD when it is a git repo, and nothing
# otherwise. Registering $HOME because someone ran the installer from their home
# directory is a mess that persists in the store.
choose_project() {
    [ "$WANT_PROJECT" = 1 ] || return 0
    if [ -n "$PROJECT_DIR" ]; then
        [ -d "$PROJECT_DIR" ] || die "--project $PROJECT_DIR is not a directory"
        return 0
    fi
    if have git && git -C "$PWD" rev-parse --show-toplevel >/dev/null 2>&1; then
        PROJECT_DIR=$(git -C "$PWD" rev-parse --show-toplevel)
        return 0
    fi
    # git is not required to answer this, and must not be: this step runs
    # BEFORE install_git, so on the bare machine the installer exists for,
    # `have git` is false and asking git would mean never finding the repo the
    # user is standing in. Walking up for `.git` needs nothing installed. A
    # `.git` FILE counts as well as a directory — that is what a worktree and a
    # submodule have, and the fleet's own agents work in worktrees.
    _dir=$PWD
    while [ -n "$_dir" ] && [ "$_dir" != / ]; do
        if [ -e "$_dir/.git" ]; then
            PROJECT_DIR=$_dir
            return 0
        fi
        _dir=$(dirname "$_dir")
    done
    # The loop stops before `/` on purpose: a repository AT the filesystem root
    # is not a case worth code.
    debug "no git repo at or above $PWD — setting up the machine only"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"
    preflight
    detect_os
    pkg_manager
    survey
    choose_project
    resolve
    if short_circuit; then
        exit 0
    fi
    banner
    install_uv
    install_cli
    path_check
    install_tmux
    install_gh
    install_git
    install_node
    install_claude
    init_home
    run_doctor
    summary
    handoff
}

# AISQUARE_INSTALL_LIB=1 sources this file without running it, so the unit tests
# in tests/test_install_script_functions.py can call one function at a time with
# stub commands on PATH (§8.2).
#
# This is also the LAST LINE of the file on purpose — see note 1 in the header:
# under `curl … | sh` a truncated download must define functions and call
# nothing.
if [ "${AISQUARE_INSTALL_LIB:-0}" != "1" ]; then
    main "$@"
fi
