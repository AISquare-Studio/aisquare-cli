#!/bin/sh
# Drive tests/install/cell.sh over a matrix of bare distributions.
#
#   tests/install/matrix.sh                 # every image
#   tests/install/matrix.sh alpine:3.22     # one image
#   ENGINE=docker tests/install/matrix.sh    # docker instead of podman
#
# This is the only test that proves §0.2 ("it detects the OS and takes the right
# path for it"), because it is the only one where the OS is real. Everything else
# about install.sh can be checked with stubs; the package matrix cannot.
#
# WHY THESE FIVE. Not a survey — each one is in the matrix for a property no
# other image has:
#
#   debian:12     apt, and /bin/sh is dash. The Node it ships is 18, BELOW
#                 Repomix's floor, so it is the cell that exercises the
#                 too-old-system-package branch.
#   ubuntu:22.04  apt on an older base; ships Node 12.
#   fedora:41     dnf5 (`config-manager addrepo`), the syntax RHEL's dnf4 does
#                 not have.
#   archlinux     pacman, and `github-cli` rather than `gh` as the package name.
#   alpine:3.22   musl, BusyBox ash as /bin/sh, apk, no bash, and NO CURL — so
#                 it is the cell that takes install.sh's wget path and the one
#                 where a bashism or a GNU-only flag surfaces.
#
# A LOCAL WHEEL, not PyPI. The cell asserts a green doctor, and two of this
# branch's changes are TO doctor checks — so grading against the last release
# would grade the wrong code. `make -C . dist` equivalent below; the wheel is
# mounted and passed through AISQUARE_INSTALL_PACKAGE.

set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
ENGINE=${ENGINE:-}
if [ -z "$ENGINE" ]; then
    if command -v podman >/dev/null 2>&1; then
        ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then
        ENGINE=docker
    else
        echo "need podman or docker" >&2
        exit 1
    fi
fi

IMAGES=${*:-"debian:12 ubuntu:22.04 fedora:41 archlinux alpine:3.22"}

#: Whether the caller named images, captured HERE and not asked later. The wheel
#: check below uses `set -- "$DIST"/*.whl`, which REPLACES "$@" — so a later
#: `[ "$#" -eq 0 ]` reads the wheel count, not the caller's arguments. Measured:
#: the non-root cell was silently skipped on every full run because of it, and a
#: skipped cell looks exactly like a cell that has nothing to say.
ALL_IMAGES=0
[ "$#" -eq 0 ] && ALL_IMAGES=1

# One image also runs as a NORMAL USER WITH SUDO (tests/install/cell-nonroot.sh),
# which is the primary case and the one every root cell cannot reach: `sudo_run`
# only calls sudo when `PKG_SUDO` is set, and it is empty for root. Ubuntu,
# because it is the platform where the `gh` step writes a keyring under /etc as
# root while everything else lands in a user's $HOME — the split this exercises.
NONROOT_IMAGE=${NONROOT_IMAGE:-ubuntu:22.04}

# --- build the wheel the cells will install --------------------------------

DIST="$REPO/dist"
echo "==> Building a wheel from this tree"
rm -rf "$DIST"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3
if ! "$PY" -m build --wheel --outdir "$DIST" "$REPO" >/dev/null 2>&1; then
    echo "wheel build failed; run: $PY -m pip install build" >&2
    exit 1
fi

# Exactly one wheel, asserted — the same trap .github/workflows/ci.yml's package
# job calls out: a second wheel in dist/ collapses two paths into one argument
# and uv then fails on a nonsense requirement rather than on the real problem.
set -- "$DIST"/*.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "expected exactly one wheel in $DIST, found $#: $*" >&2
    exit 1
fi
WHEEL=$(basename "$1")
echo "    $WHEEL"

# --- run the cells ---------------------------------------------------------

passed=""
failed=""
for image in $IMAGES; do
    printf '\n################ %s ################\n' "$image"
    if "$ENGINE" run --rm \
        -v "$REPO:/mnt:ro,z" \
        -e "AISQUARE_INSTALL_PACKAGE=/mnt/dist/$WHEEL" \
        -e "CELL_SKIP_AGENT=${CELL_SKIP_AGENT:-1}" \
        -e HOME=/root \
        "$image" sh /mnt/tests/install/cell.sh; then
        passed="$passed $image"
    else
        failed="$failed $image"
    fi
done

# The non-root cell, unless the caller named specific images.
if [ "$ALL_IMAGES" = 1 ]; then
    printf '\n################ %s (as a non-root user) ################\n' "$NONROOT_IMAGE"
    if "$ENGINE" run --rm \
        -v "$REPO:/mnt:ro,z" \
        -e "AISQUARE_INSTALL_PACKAGE=/mnt/dist/$WHEEL" \
        -e "CELL_SKIP_AGENT=${CELL_SKIP_AGENT:-1}" \
        "$NONROOT_IMAGE" sh /mnt/tests/install/cell-nonroot.sh; then
        passed="$passed $NONROOT_IMAGE(non-root)"
    else
        failed="$failed $NONROOT_IMAGE(non-root)"
    fi
fi

printf '\n================ matrix ================\n'
[ -n "$passed" ] && printf 'passed:%s\n' "$passed"
[ -n "$failed" ] && printf 'FAILED:%s\n' "$failed"
[ -z "$failed" ]
