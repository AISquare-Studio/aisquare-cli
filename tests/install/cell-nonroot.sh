#!/bin/sh
# The cell, run as a NORMAL USER WITH SUDO — which is the primary case.
#
# WHY THIS EXISTS AS ITS OWN WRAPPER. Every cell in tests/install/matrix.sh runs
# as root, because that is what a container gives you. Root is a legitimate path
# — §3.7 permits it where there is no other user, and a Dockerfile is exactly
# that — but it is the path where `PKG_SUDO` is EMPTY and `sudo_run` degrades to
# a plain `run`. So the whole matrix was exercising the branch a person will
# never take, and leaving the one they always will untested:
#
#   * `sudo_run` actually calling sudo, for one command at a time (§3.7)
#   * the apt/dnf keyring steps for `gh`, which write under /etc as root while
#     the rest of the install lands under a user's $HOME
#   * uv, aisquare and Claude Code landing in ~/.local/bin for a real user
#     rather than /root/.local/bin
#   * `preflight`'s root refusal NOT firing, which is the whole point of it
#
# It prepares the machine as root — install sudo, make a user, grant NOPASSWD —
# and then hands over to cell.sh as that user. Everything asserted is cell.sh's;
# this file only changes who runs it.

set -eu

CELL_USER=${CELL_USER:-aisq}

# shellcheck source=/dev/null  # a file on the container, not in this repo.
. /etc/os-release

printf '\n=== non-root cell: preparing %s on %s %s ===\n' "$CELL_USER" "$ID" "${VERSION_ID:-}"

case "$ID" in
    debian | ubuntu)
        apt-get update -qq
        apt-get install -y -qq sudo curl ca-certificates >/dev/null
        useradd -m -s /bin/sh "$CELL_USER"
        ;;
    fedora | rhel | centos | rocky | almalinux)
        dnf install -y -q sudo shadow-utils >/dev/null 2>&1 || dnf install -y -q sudo >/dev/null
        useradd -m -s /bin/sh "$CELL_USER"
        ;;
    arch)
        pacman -Sy --noconfirm --needed --quiet sudo >/dev/null 2>&1
        useradd -m -s /bin/sh "$CELL_USER"
        ;;
    alpine)
        apk add --no-cache sudo shadow ca-certificates >/dev/null
        adduser -D -s /bin/sh "$CELL_USER"
        ;;
    *)
        echo "### FAIL: cell-nonroot.sh does not know how to prepare '$ID'" >&2
        exit 1
        ;;
esac

# NOPASSWD, because there is no terminal to type a password at — which is also
# true of the CI runner this is really for. The script's own behaviour is
# unchanged either way: it calls `sudo <one command>` and reports a failure as a
# warning (§3.2), never re-executing itself as root.
mkdir -p /etc/sudoers.d
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$CELL_USER" >"/etc/sudoers.d/$CELL_USER"
chmod 440 "/etc/sudoers.d/$CELL_USER"

HOME_DIR=$(getent passwd "$CELL_USER" | cut -d: -f6)
[ -n "$HOME_DIR" ] || HOME_DIR="/home/$CELL_USER"
WORK=${WORKDIR:-/work}
mkdir -p "$WORK"
chown -R "$CELL_USER" "$WORK"

printf '    user %s, home %s, sudo NOPASSWD\n' "$CELL_USER" "$HOME_DIR"

# The root REFUSAL is not asserted here, deliberately: inside a container
# `in_container()` is true by design, so root is allowed and the refusal cannot
# be reached without lying about the machine. It is covered where it can be —
# tests/test_install_script_functions.py stubs `id` and the container markers.

printf '\n=== handing over to cell.sh as %s ===\n\n' "$CELL_USER"

# `env` rather than `su -` so the AISQUARE_INSTALL_PACKAGE and CELL_* variables
# survive; `-s /bin/sh` because that is what a `curl … | sh` user gets.
exec su "$CELL_USER" -s /bin/sh -c "
    HOME='$HOME_DIR' \
    PATH='$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin' \
    AISQUARE_INSTALL_PACKAGE='${AISQUARE_INSTALL_PACKAGE:-}' \
    CELL_SKIP_AGENT='${CELL_SKIP_AGENT:-1}' \
    WORKDIR='$WORK' \
    NO_COLOR=1 \
    sh ${INSTALLER_DIR:-/mnt}/tests/install/cell.sh
"
