#!/bin/sh
# Install ttyd 1.7.7, the PTY server the journeys' terminal runs on, from its
# static release binary (e2e/PLAN.md §5). Linux only: on macOS it is
# `brew install ttyd`.
#
# WHY THE RELEASE BINARY AND NOT apt. The harness is written against one ttyd:
# its websocket protocol, its flags (-W -o -c … -p 0) and the way it forks the
# login shell. A distribution package is whatever version that release froze,
# and it differs between the act image, GitHub's runner and a laptop. One
# static binary, one version, everywhere.
#
# WHY TWO CHECKS, and which one is the security check. SHA256SUMS comes from the
# same release page as the binary, so on its own it proves only that the
# download is complete: anyone able to replace a release asset can replace the
# sums file beside it, and a release can be edited after it is published. The
# hashes PINNED below are the check that matters. They were copied out of that
# SHA256SUMS on 2026-10-16. The published file is still compared, so a pin that
# disagrees with upstream fails here and names the file that disagreed, rather
# than letting the two drift apart unnoticed.
#
# Runs as root (act) or as a user with passwordless sudo (GitHub's runner):
# only the final `install` needs root. A second run with the same binary in
# place downloads nothing.
set -eu

TTYD_VERSION=1.7.7
TTYD_SHA256_X86_64=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
TTYD_SHA256_AARCH64=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165

dest=${TTYD_DEST:-/usr/local/bin/ttyd}
base=https://github.com/tsl0922/ttyd/releases/download/$TTYD_VERSION

die() {
    echo "install-ttyd.sh: $*" >&2
    exit 1
}

[ "$(uname -s)" = Linux ] || die "Linux only (on macOS: brew install ttyd)"

# `uname -m` spells the same machine two ways: aarch64 on Linux arm64 kernels,
# arm64 in some containers' userlands. The release names it aarch64.
case "$(uname -m)" in
    x86_64 | amd64) asset=ttyd.x86_64 pinned=$TTYD_SHA256_X86_64 ;;
    aarch64 | arm64) asset=ttyd.aarch64 pinned=$TTYD_SHA256_AARCH64 ;;
    *) die "no pinned ttyd $TTYD_VERSION hash for $(uname -m)" ;;
esac

if [ "$(id -u)" -eq 0 ]; then as_root=""; else as_root=sudo; fi

sha256() {
    sha256sum "$1" | cut -d ' ' -f 1
}

if [ -x "$dest" ] && [ "$(sha256 "$dest")" = "$pinned" ]; then
    echo "ttyd $TTYD_VERSION already at $dest (sha256 matches the pin)"
else
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    # --retry: a release download is a redirect to a CDN, and a single dropped
    # connection should cost a retry, not a nightly.
    curl -fsSL --retry 3 --retry-delay 2 -o "$work/$asset" "$base/$asset"
    curl -fsSL --retry 3 --retry-delay 2 -o "$work/SHA256SUMS" "$base/SHA256SUMS"

    actual=$(sha256 "$work/$asset")
    # The sums file's second column is the bare asset name; `*name` is the
    # binary-mode spelling some tools write, accepted in case it changes.
    published=$(awk -v f="$asset" '$2 == f || $2 == "*" f { print $1 }' "$work/SHA256SUMS")

    [ "$actual" = "$pinned" ] ||
        die "$asset: sha256 $actual is not the pinned $pinned — refusing to install it"
    [ "$published" = "$pinned" ] ||
        die "$asset: the release's SHA256SUMS says '${published:-<no entry>}', the pin says $pinned — update the pin only after finding out why they differ"

    $as_root install -m 0755 "$work/$asset" "$dest"
fi

# The binary's own word, not the file name's: a wrong asset that hashed right
# cannot happen with the checks above, but a $dest shadowed on PATH can.
version=$("$dest" --version)
case "$version" in
    *"$TTYD_VERSION"*) ;;
    *) die "$dest --version says '$version', not $TTYD_VERSION" ;;
esac
echo "installed: $version ($dest, $asset, sha256 $pinned)"
if [ "$(command -v ttyd || true)" != "$dest" ]; then
    echo "::warning::ttyd on PATH is '$(command -v ttyd || echo none)', not $dest — the harness should be given $dest explicitly"
fi
