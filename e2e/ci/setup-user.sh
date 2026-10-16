#!/bin/sh
# Make the person the journeys play: a normal account ($E2E_USER, default
# `e2e`) with a fresh home from /etc/skel, bash as its login shell, and
# passwordless sudo. Linux CI only.
#
# WHY NOT THE JOB'S OWN USER. Under act the job runs as root; on GitHub it runs
# as `runner`, whose home already holds a toolcache, the checkout and a dozen
# SDKs. Neither is someone who just pasted the README one-liner. Root is the
# path where install.sh's `sudo_run` degrades to a plain `run`
# (tests/install/cell-nonroot.sh explains why that path is the one a person
# never takes), and the runner's home is anything but fresh. An account made
# here starts with what `useradd -m` gives a new user on Ubuntu, and nothing
# from the harness: the journeys reach it through a login shell, never by
# mounting or sharing the checkout (e2e/PLAN.md §10).
#
# WHY NOPASSWD. install.sh calls `sudo <one command>` for system packages (tmux,
# the apt keyring for gh), and nobody is there to type a password. The same
# reasoning, and the same sudoers line, as cell-nonroot.sh.
#
# WHY A FIXED UID (2000). `useradd` would take the first free one, and that
# differs by image: it is 1001 in catthehacker's act image, where 1001 also
# owns /opt/acttoolcache (act's Node), and something else on GitHub's runner,
# where `runner` is 1001. One number everywhere keeps paths that embed it, like
# tmux's `tmux-<uid>` socket directory, the same in act, on GitHub and in the
# screenshots of both. If it is taken, useradd fails and names it.
#
# HOW THE HARNESS ENTERS IT: `sudo -n -u e2e -H -- env -i … bash --login`
# (e2e/runner/machine.py), with ttyd running as the job's user. Ubuntu's
# sudoers sets `use_pty`, so inside ttyd's terminal that sudo puts a second PTY
# between the terminal and the shell; it relays keys, resizes and hangups, and
# it is the one layer a person's own terminal does not have.
#
# Re-running it replaces the account, so the home is fresh every time, even in a
# container that `act --reuse` kept alive. That is also why it refuses to run
# anywhere but CI: it deletes a home directory and edits sudoers.
set -eu

user=${E2E_USER:-e2e}
uid=${E2E_UID:-2000}

die() {
    echo "setup-user.sh: $*" >&2
    exit 1
}

[ "$(uname -s)" = Linux ] || die "Linux only: on macOS the harness uses a scratch HOME instead"
# act sets GITHUB_ACTIONS=true as well, so this admits both, and nothing else.
[ "${GITHUB_ACTIONS:-}" = true ] ||
    die "refusing to run outside GitHub Actions or act: it creates an account, grants it root and replaces /home/$user"

# A portable POSIX user name, checked before it is written into sudoers.
case "$user" in
    '' | -* | *[!a-z0-9_-]*) die "'$user' is not a plain lower-case user name" ;;
esac
case "$uid" in
    '' | *[!0-9]*) die "E2E_UID '$uid' is not a number" ;;
esac

# Root for everything below. `sudo` resets the environment, so the variables
# this script reads are handed over explicitly.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -n -- env E2E_USER="$user" E2E_UID="$uid" GITHUB_ACTIONS=true sh "$0" "$@"
fi

if getent passwd "$user" >/dev/null; then
    old_home=$(getent passwd "$user" | cut -d: -f6)
    echo "replacing the existing $user account, so its home starts fresh"
    # A tmux server or a stray shell from a previous run would make userdel
    # refuse ("user is currently used by process").
    pkill -KILL -u "$user" 2>/dev/null || true
    # `--remove` also exits non-zero over things that do not matter here, such
    # as a missing mail spool, so its result is judged by what is left.
    userdel --remove "$user" 2>/dev/null || true
    if getent passwd "$user" >/dev/null; then
        die "could not remove the existing $user account"
    fi
    case "$old_home" in
        /home/?*) rm -rf -- "$old_home" ;;
    esac
fi
# A group left behind without its user would make `useradd --user-group` fail.
if getent group "$user" >/dev/null; then
    groupdel "$user"
fi

useradd --create-home --user-group --uid "$uid" --shell /bin/bash "$user"
home=$(getent passwd "$user" | cut -d: -f6)
if [ -z "$home" ] || [ ! -d "$home" ]; then
    die "useradd made $user without a home directory"
fi

# Written under a name with a dot, then renamed: sudo skips files in
# /etc/sudoers.d whose names contain one, so a half-written file is never read,
# and visudo checks the syntax before the rename makes it live.
rule=/etc/sudoers.d/$user
printf '%s ALL=(ALL:ALL) NOPASSWD:ALL\n' "$user" >"$rule.tmp"
chmod 0440 "$rule.tmp"
visudo -cqf "$rule.tmp" || die "visudo rejected $rule.tmp"
mv "$rule.tmp" "$rule"

# Asserted, not assumed: the installer's first privileged command is where a
# missing grant would otherwise surface, minutes later, as a product failure.
su - "$user" -c 'sudo -n true' || die "$user cannot use sudo without a password"
[ "$(su - "$user" -c 'id -u')" -ne 0 ] || die "$user is root"

printf 'user %s: uid %s, home %s (%s), shell %s, sudo NOPASSWD\n' \
    "$user" "$(id -u "$user")" "$home" "$(find "$home" -mindepth 1 -maxdepth 1 -printf '%f ')" \
    "$(getent passwd "$user" | cut -d: -f7)"
