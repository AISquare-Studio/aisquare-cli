#!/bin/sh
# The latest RELEASE, onto a machine with an old Node and no extra chosen.
#
#   docker run --rm -v "$PWD:/mnt:ro" -e HOME=/root debian:12 sh /mnt/tests/install/release-cell.sh
#
# WHY THIS CELL EXISTS beside cell.sh. Every other cell grades THIS TREE (its
# AISQUARE_INSTALL_PACKAGE is a wheel built from it) and chooses
# --with chrome-devtools, so its Node is 22.12 or newer. But install.sh installs
# the latest release from PyPI for everyone else, and that release's doctor can
# have rows this tree no longer has: up to 0.9.0, the codebase snapshot's
# `repomix`, `snapshot` and `tiktoken` (#399). The review of #399 found
# `curl … | sh -s -- --yes` on Debian 12's own Node 18 exiting 2 on `repomix`,
# which no cell could see. This one installs the release there, with the flags
# a provisioning script passes, and asserts:
#
#   1. --yes --no-agent exits 0, and leaves Debian's Node as it was;
#   2. --yes --no-agent --no-system-deps on the result has nothing to do.
#
# Debian 12, because its nodejs is 18 and its npm a separate package, the
# combination the finding was measured on. --no-agent, as in the matrix, so the
# cell does not depend on a third party's CDN.

set -eu

INSTALLER=${INSTALLER:-/mnt/install.sh}
WORKDIR=${WORKDIR:-/work}

fail() {
    printf '\n### FAIL: %s\n' "$*" >&2
    exit 1
}
head1() { printf '\n=== %s ===\n' "$*"; }

[ -z "${AISQUARE_INSTALL_PACKAGE:-}" ] ||
    fail "this cell grades the RELEASE on PyPI; unset AISQUARE_INSTALL_PACKAGE"

# THIS TREE'S SCRIPT, not the release's. Once main's install.sh hands off to the
# script attached to the latest release (#402), a run with AISQUARE_INSTALL_PACKAGE
# unset downloads that one instead, and 0.9.0's still upgrades Node for Repomix, so
# the Node check below would grade 0.9.0's installer. The variable, not
# --no-handoff: until #402 merges, parse_args refuses the flag, and nothing reads
# the variable.
AISQUARE_INSTALL_NO_HANDOFF=1
export AISQUARE_INSTALL_NO_HANDOFF

head1 "Debian's own Node and npm, below the 22.12 floor"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates nodejs npm >/dev/null
node_before=$(node --version)
printf '  node %s, npx %s\n' "$node_before" "$(npx --version)"

# ---------------------------------------------------------------------------
# RUN 1 — the release, --yes, no extra
# ---------------------------------------------------------------------------

head1 "RUN 1: --yes --no-agent, the latest release, no extra"
mkdir -p "$WORKDIR"
cd "$WORKDIR"
set +e
sh "$INSTALLER" --yes --no-agent
run1=$?
set -e
printf '\nrun 1 exit: %s\n' "$run1"

PATH="$HOME/.local/bin:$PATH"
export PATH
command -v aisquare >/dev/null 2>&1 || fail "aisquare is not on PATH after run 1"
head1 "installed: $(aisquare --version)"
printf '\n--- doctor, for the log ---\n'
aisquare doctor || true

[ "$run1" = 0 ] ||
    fail "run 1 exited $run1: a row of the release's doctor read as unexpected (see above)"
[ "$(node --version)" = "$node_before" ] ||
    fail "Node moved from $node_before to $(node --version), though no extra was chosen"

# ---------------------------------------------------------------------------
# RUN 2 — the same machine, --no-system-deps: nothing to do
# ---------------------------------------------------------------------------

head1 "RUN 2: --yes --no-agent --no-system-deps, which must short-circuit"
set +e
sh "$INSTALLER" --yes --no-agent --no-system-deps >"${TMPDIR:-/tmp}/run2.log" 2>&1
run2=$?
set -e
cat "${TMPDIR:-/tmp}/run2.log"
printf '\nrun 2 exit: %s\n' "$run2"
[ "$run2" = 0 ] || fail "run 2 exited $run2 on a machine run 1 left current"
grep -q 'Nothing to do' "${TMPDIR:-/tmp}/run2.log" ||
    fail "run 2 did not short-circuit: a release row is not expected under --no-system-deps"

head1 "PASS: the latest release onto Node $node_before"
