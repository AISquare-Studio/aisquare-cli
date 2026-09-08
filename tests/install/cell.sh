#!/bin/sh
# One cell of the installer's container matrix: run install.sh on a bare
# distribution and assert what it produced (docs/plans/one-line-install.md §8.3,
# §8.4). Runs INSIDE the container; tests/install/matrix.sh drives it, and
# .github/workflows/install.yml runs the same thing on every push.
#
# The acceptance criterion of the whole feature is one line, and it is asserted
# here rather than described:
#
#     aisquare --json doctor  ->  every check ok EXCEPT brain
#
# THREE RUNS, and the sequence is the test rather than an accident:
#
#   1. bare machine, --no-project. Nothing but a downloader is preinstalled, so
#      this is the run that exercises the §5 package matrix — tmux, gh, git and
#      Node all come from the platform's own manager.
#   2. with a project. `snapshot` is amber after run 1 by construction (no
#      project is registered), which is also what stops run 2 short-circuiting.
#      THIS is the run the acceptance criterion is asserted against.
#   3. again, with every package manager replaced by a stub that records being
#      called. Asserts §3.9.4 with teeth: a current machine must print its
#      summary and exit 0 having installed NOTHING, and the stub is what turns
#      "installed nothing" from a claim into an observation.
#
# Environment:
#   AISQUARE_INSTALL_PACKAGE  a local wheel, so the matrix grades THIS TREE's
#                             doctor rather than the last release's.
#   CELL_SKIP_AGENT=0         install Claude Code for real (one cell does; the
#                             rest pass --no-agent so the matrix does not depend
#                             on a third party's CDN being up).

set -eu

INSTALLER=${INSTALLER:-/mnt/install.sh}
WORKDIR=${WORKDIR:-/work}
STUBDIR=/opt/pkg-stub
STUB_LOG=/tmp/pkg-stub.log
SKIP_AGENT=${CELL_SKIP_AGENT:-1}

fail() {
    printf '\n### FAIL: %s\n' "$*" >&2
    exit 1
}
head1() { printf '\n=== %s ===\n' "$*"; }

# --- 0. what distribution is this, and what is the ONE prerequisite? --------
#
# Only a downloader is installed here. Every other dependency is left absent on
# purpose: if this script pre-installed tmux or Node, the cell would be grading
# its own preparation instead of the installer's package matrix.

. /etc/os-release
head1 "cell: $ID ${VERSION_ID:-} — /bin/sh is $(readlink -f /bin/sh)"

case "$ID" in
    debian | ubuntu)
        apt-get update -qq
        apt-get install -y -qq curl ca-certificates >/dev/null
        ;;
    fedora | rhel | centos | rocky | almalinux)
        # curl and ca-certificates are already in the base image; measured.
        ;;
    arch)
        pacman -Sy --noconfirm --needed --quiet ca-certificates >/dev/null 2>&1 || true
        ;;
    alpine)
        # Deliberately NOT installing curl. Alpine ships BusyBox `wget` and no
        # curl, so this cell is the one that exercises install.sh's wget
        # fallback — the branch that is otherwise never taken anywhere.
        apk add --no-cache ca-certificates >/dev/null
        ;;
    *) fail "cell.sh does not know how to prepare '$ID'" ;;
esac

if command -v curl >/dev/null 2>&1; then
    head1 "downloader: curl"
else
    command -v wget >/dev/null 2>&1 || fail "neither curl nor wget after prep"
    head1 "downloader: wget only (this cell tests the fallback)"
fi

AGENT_FLAG=""
[ "$SKIP_AGENT" = 1 ] && AGENT_FLAG="--no-agent"

# ---------------------------------------------------------------------------
# helpers that read the installed CLI
# ---------------------------------------------------------------------------

PATH="$HOME/.local/bin:$PATH"
export PATH

# The names of every doctor check that is not ok, space-separated and sorted.
# Sorted so the assertion is about the SET and not about the order checks happen
# to run in — an ordering change is not a regression and must not read as one.
amber_checks() {
    aisquare --json doctor 2>/dev/null |
        tr '{' '\n' |
        sed -n 's/.*"name": *"\([^"]*\)".*"status": *"\(warn\|fail\)".*/\1/p' |
        sort |
        tr '\n' ' ' |
        sed 's/  */ /g; s/^ //; s/ $//'
}

# Total checks in the payload, so "all ok except brain" cannot be satisfied by a
# doctor that answered with two checks.
total_checks() {
    aisquare --json doctor 2>/dev/null | tr '{' '\n' | grep -c '"name": *"' || true
}

# ---------------------------------------------------------------------------
# RUN 1 — the bare machine
# ---------------------------------------------------------------------------

head1 "RUN 1: bare machine, no project"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

set +e
# shellcheck disable=SC2086  # AGENT_FLAG is deliberately word-split (or empty).
sh "$INSTALLER" --yes --no-project $AGENT_FLAG
run1=$?
set -e
printf '\nrun 1 exit: %s\n' "$run1"
# 0 or 2 are both legitimate here: `snapshot` is amber with no project
# registered, and whether that is classed actionable (0) or unexpected (2) is
# the thing run 2 pins. 1 means a FATAL step failed, which is never acceptable.
[ "$run1" = 1 ] && fail "run 1 exited 1 — a fatal step (uv or aisquare-cli) failed"

command -v aisquare >/dev/null 2>&1 || fail "aisquare is not on PATH after run 1"
command -v asq >/dev/null 2>&1 || fail "asq is not on PATH after run 1"
head1 "installed: $(aisquare --version)"

# The four things the installer promised, checked one at a time so a failure
# names which one.
aisquare --version >/dev/null || fail "aisquare --version does not run"

# tiktoken in the CLI's OWN environment (§1.2) — the whole reason for `--with`.
case "$(amber_checks)" in
    *tiktoken*) fail "tiktoken is amber: --with tiktoken did not take" ;;
esac

# The install shape the `install` doctor check wants (§3.1): a real binary on
# PATH, outside any virtualenv.
case "$(amber_checks)" in
    *install*) fail "the 'install' check is amber — uv produced a shape doctor rejects" ;;
esac

for tool in tmux gh git node; do
    if command -v "$tool" >/dev/null 2>&1; then
        printf '  %-5s %s\n' "$tool" "present"
    else
        # Warn-only by design (§3.2): each degrades one feature. Recorded, not
        # fatal — but the matrix log has to say so, or a silently toolless
        # machine reads as a pass.
        printf '  %-5s %s\n' "$tool" "ABSENT (warn-only class)"
    fi
done

# ---------------------------------------------------------------------------
# RUN 2 — with a project. The acceptance criterion.
# ---------------------------------------------------------------------------

head1 "RUN 2: with a registered project"

command -v git >/dev/null 2>&1 || fail "git absent after run 1 — cannot make a project to register"
mkdir -p "$WORKDIR/proj"
cd "$WORKDIR/proj"
git init -q . 2>/dev/null || git init -q .
git config user.email cell@example.invalid
git config user.name "Matrix Cell"
printf 'print("hello")\n' >main.py
git add -A
git commit -qm "one file, so Repomix has something to pack"

set +e
# shellcheck disable=SC2086
sh "$INSTALLER" --yes $AGENT_FLAG
run2=$?
set -e
printf '\nrun 2 exit: %s\n' "$run2"
[ "$run2" = 1 ] && fail "run 2 exited 1 — a fatal step failed"

total=$(total_checks)
amber=$(amber_checks)
head1 "doctor: $total checks, not-ok = [$amber]"

# A doctor that answered with a handful of checks must not be able to satisfy
# "all ok except brain".
[ "$total" -ge 15 ] || fail "doctor reported only $total checks — expected the full set (17+)"

# THE ACCEPTANCE CRITERION OF THE WHOLE FEATURE (§0.3, §0.4, §8.3).
if [ "$amber" != "brain" ]; then
    printf '\n--- full doctor, for the log ---\n'
    aisquare doctor || true
    fail "expected exactly [brain] amber, got [$amber]"
fi
head1 "ACCEPTANCE: 	 every check ok except brain"

[ "$run2" = 0 ] || fail "with only brain amber the installer must exit 0, got $run2"

# ---------------------------------------------------------------------------
# RUN 3 — idempotence, with the package managers stubbed (§3.9.4, §8.4)
# ---------------------------------------------------------------------------

head1 "RUN 3: re-run with every package manager stubbed"

# TWO DELIBERATE DIFFERENCES from runs 1 and 2, both required for this run to
# test what it claims:
#
#   env -u AISQUARE_INSTALL_PACKAGE — the short-circuit deliberately never fires
#     for a local build (a wheel in a directory is not "current" relative to
#     anything, and a developer testing a build wants it installed). So the
#     §3.9.4 path can only be exercised through the normal PyPI-named target.
#     The build under test is already installed at this point; this run must not
#     replace it, and asserts below that it did not.
#   --offline — so the assertion does not depend on the tree's version matching
#     whatever PyPI currently serves. With no version to compare against, an
#     installed CLI is "current", which is exactly the state §3.9.4 is about.

# A stub that RECORDS and fails. Recording rather than only failing, because
# "was it called?" is the actual question — a stub that merely fails could be
# called and swallowed by an `|| true` and the test would never know.
mkdir -p "$STUBDIR"
: >"$STUB_LOG"
for pm in apt-get apt dnf yum pacman zypper apk brew npm; do
    cat >"$STUBDIR/$pm" <<STUB
#!/bin/sh
printf '%s %s\n' "$pm" "\$*" >> "$STUB_LOG"
echo "STUB: $pm should not have been called on a current machine" >&2
exit 1
STUB
    chmod +x "$STUBDIR/$pm"
done

# Byte-identity of the agent settings across a re-run (§8.4). Hooks are exactly
# the kind of thing that accumulates on a second run, and `install_hooks`
# filtering its own groups is what prevents it — so the file is fingerprinted
# rather than trusted.
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
before=""
[ -f "$SETTINGS" ] && before=$(cksum <"$SETTINGS")

version_before=$(aisquare --version)

set +e
# shellcheck disable=SC2086
PATH="$STUBDIR:$PATH" env -u AISQUARE_INSTALL_PACKAGE \
    sh "$INSTALLER" --yes --offline $AGENT_FLAG >/tmp/run3.log 2>&1
run3=$?
set -e
cat /tmp/run3.log
printf '\nrun 3 exit: %s\n' "$run3"

[ "$run3" = 0 ] || fail "a re-run on a current machine must exit 0, got $run3"

# §3.9.4 with teeth: it must not merely succeed, it must not have DONE anything.
grep -q 'Nothing to do' /tmp/run3.log ||
    fail "run 3 did not short-circuit — §3.9.4 promises a current machine installs nothing"

if grep -qE '^==> Installing' /tmp/run3.log; then
    fail "run 3 tried to install something: $(grep -E '^==> Installing' /tmp/run3.log)"
fi

if [ -s "$STUB_LOG" ]; then
    printf '\n--- package manager calls during run 3 ---\n'
    cat "$STUB_LOG"
    fail "run 3 called a package manager on an already-current machine"
fi

version_after=$(aisquare --version)
[ "$version_before" = "$version_after" ] ||
    fail "the version moved on a no-op run: $version_before -> $version_after"

if [ -f "$SETTINGS" ]; then
    after=$(cksum <"$SETTINGS")
    [ "$before" = "$after" ] ||
        fail "$SETTINGS changed on a re-run (hook accumulation): $before -> $after"
    head1 "settings.json byte-identical across the re-run"
fi

amber_again=$(amber_checks)
[ "$amber_again" = "brain" ] || fail "after the re-run, not-ok = [$amber_again], expected [brain]"

head1 "CELL PASSED: $ID ${VERSION_ID:-}"
