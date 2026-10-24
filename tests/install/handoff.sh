#!/bin/sh
# The hand-off, end to end on a real runner: main's install.sh (this tree,
# unstamped) downloads a release's own stamped install.sh with the REAL curl and
# runs it. .github/workflows/install.yml's `handoff` job runs this.
#
# What tests/test_install_script_handoff.py cannot show and this can: the real
# curl, GNU wget and BusyBox wget (their flags, and what each says for a real
# HTTP 404), the real mktemp and traps, and main's script run by dash and by
# BusyBox ash, as /bin/sh is on Debian and Alpine. What it cannot reach is GitHub itself. The release is THIS tree
# stamped by scripts/stamp_installers.py and served by a local http.server, and
# a curl wrapper first on PATH points the release and PyPI URLs at it; every
# other URL reaches the real curl unchanged.
#
# Every run is a --dry-run, so nothing is installed.
#
# Usage, from the repository root (needs python3, curl, dash; wget and busybox
# are used when present):
#   sh tests/install/handoff.sh

set -eu

ROOT=$(pwd)
[ -f "$ROOT/install.sh" ] || {
    echo "run this from the repository root" >&2
    exit 1
}

fail() {
    printf '\n### FAIL: %s\n' "$*" >&2
    exit 1
}
head1() { printf '\n=== %s ===\n' "$*"; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/aisquare-handoff-XXXXXX")
SERVER_PID=""
trap '[ -z "$SERVER_PID" ] || kill "$SERVER_PID" 2>/dev/null; rm -rf "$WORK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

VERSION=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
SERVED="$WORK/served"
LATEST="$SERVED/releases/latest/download"
PINNED="$SERVED/releases/download/v$VERSION"
mkdir -p "$LATEST" "$PINNED" "$SERVED/pypi/aisquare-cli" "$WORK/bin" "$WORK/home" "$WORK/tmp"

python3 scripts/stamp_installers.py --tag "v$VERSION" --out "$LATEST" >/dev/null
cp "$LATEST/install.sh" "$PINNED/install.sh"
printf '{"info":{"name":"aisquare-cli","version":"%s"}}' "$VERSION" >"$SERVED/pypi/aisquare-cli/json"
grep -q "^STAMPED_VERSION=\"$VERSION\"\$" "$LATEST/install.sh" ||
    fail "the stamper did not stamp $VERSION"

# The server picks a free port and writes it down once it is listening.
python3 -c '
import functools, http.server, sys
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
handler.log_message = lambda *args: None
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
with open(sys.argv[2], "w") as out:
    out.write(str(server.server_address[1]))
server.serve_forever()
' "$SERVED" "$WORK/port.tmp" &
SERVER_PID=$!
tries=0
until [ -s "$WORK/port.tmp" ]; do
    tries=$((tries + 1))
    [ "$tries" -le 100 ] || fail "the local server did not start"
    sleep 0.1
done
PORT=$(cat "$WORK/port.tmp")
LOCAL="http://127.0.0.1:$PORT"

# A downloader on PATH that points the release and PyPI URLs at the local server
# and logs what it was asked. $1 is the directory, $2 its name, $3 the real
# command. curl's `--proto =https` cannot reach an http:// server, so the
# wrapper drops it, after the log line that shows it was asked for.
make_wrapper() {
    mkdir -p "$1"
    cat >"$1/$2" <<WRAPPER
#!/bin/sh
printf '%s\n' "\$*" >>"$WORK/fetch.log"
skip=0
for arg do
    shift
    if [ "\$skip" = 1 ]; then
        skip=0
        continue
    fi
    case "\$arg" in
        --proto | --proto-redir)
            skip=1
            continue
            ;;
        https://github.com/AISquare-Studio/aisquare-cli/releases/*)
            arg="$LOCAL/releases/\${arg#https://github.com/AISquare-Studio/aisquare-cli/releases/}"
            ;;
        https://pypi.org/pypi/aisquare-cli/json) arg="$LOCAL/pypi/aisquare-cli/json" ;;
    esac
    set -- "\$@" "\$arg"
done
exec $3 "\$@"
WRAPPER
    chmod +x "$1/$2"
}

REAL_CURL=$(command -v curl) || fail "this needs curl"
make_wrapper "$WORK/bin" curl "$REAL_CURL"

# For the wget runs: the tools a dry run uses, and no curl or wget among them,
# so the wrapper is the only downloader install.sh can find.
mkdir -p "$WORK/tools"
for tool in sh uname id sed grep head cut tr mktemp rm cat sort wc dirname basename \
    env tee readlink chmod mkdir ls touch; do
    found=$(command -v "$tool") || fail "no $tool on this machine"
    ln -s "$found" "$WORK/tools/$tool"
done

DRY="--dry-run --yes --no-project --no-agent --no-system-deps"

# One run of main's install.sh: $1 is the PATH, $2 the shell, the rest are its
# arguments. Output in $WORK/out, status in $RC, and the TMPDIR must be empty.
installer() {
    _path=$1
    _shell=$2
    shift 2
    : >"$WORK/fetch.log"
    set +e
    # shellcheck disable=SC2086  # the shell is "busybox ash" for one run.
    env PATH="$_path" HOME="$WORK/home" TMPDIR="$WORK/tmp" NO_COLOR=1 \
        $_shell "$ROOT/install.sh" "$@" >"$WORK/out" 2>&1
    RC=$?
    set -e
    cat "$WORK/out"
    [ -z "$(ls -A "$WORK/tmp")" ] || fail "the run left $(ls -A "$WORK/tmp") in its TMPDIR"
}

expect() { grep -qF -e "$1" "$WORK/out" || fail "$2"; }
fetched() { grep -qF -e "$1" "$WORK/fetch.log" || fail "$2"; }

# PyPI is asked once a run at most: the hand-off asks, and a fallback reuses it.
asked_pypi_once() {
    [ "$(grep -c 'pypi.org/pypi/aisquare-cli/json' "$WORK/fetch.log")" -le 1 ] ||
        fail "PyPI was asked more than once"
}

# Main's script, falling back on a real HTTP 404 from the local server.
falls_back_on_404() {
    mv "$LATEST/install.sh" "$WORK/held.sh"
    # shellcheck disable=SC2086
    installer "$1" "$2" $DRY
    mv "$WORK/held.sh" "$LATEST/install.sh"
    [ "$RC" = 0 ] || fail "the fallback dry run exited $RC"
    expect "using this installer (the latest release has no installer attached)" \
        "a 404 did not fall back in one line"
    expect "nothing above was actually run" "main's own dry run did not run"
    if grep -qF "pinned to" "$WORK/out"; then
        fail "the fallback pinned a version, so the release's script ran after all"
    fi
}

SHELLS="dash"
if command -v busybox >/dev/null 2>&1; then
    SHELLS="$SHELLS busybox_ash"
else
    head1 "no busybox here: dash only"
fi

for name in $SHELLS; do
    shell=$name
    [ "$name" = busybox_ash ] && shell="busybox ash"
    CURL_PATH="$WORK/bin:$PATH"

    head1 "$shell: main's script hands off to the latest release ($VERSION)"
    # shellcheck disable=SC2086
    installer "$CURL_PATH" "$shell" $DRY
    [ "$RC" = 0 ] || fail "the handed-off dry run exited $RC"
    [ "$(head -n 1 "$WORK/out")" = "  using the v$VERSION installer" ] ||
        fail "the first line is not the hand-off"
    expect "pinned to $VERSION" "the release's installer did not pin its own version"
    # `==` on a machine without aisquare, `@` on one that has it.
    grep -q -e "aisquare-cli==$VERSION" -e "aisquare-cli@$VERSION" "$WORK/out" ||
        fail "the release's installer did not install $VERSION"
    expect "nothing above was actually run" "--dry-run was not passed through"
    fetched "github.com/AISquare-Studio/aisquare-cli/releases/latest/download/install.sh" \
        "the latest release's installer was not fetched"
    fetched "--max-time" "the download had no timeout"
    fetched "--proto =https" "the download was not held to HTTPS"
    asked_pypi_once

    head1 "$shell: --version $VERSION fetches that release's installer"
    # shellcheck disable=SC2086
    installer "$CURL_PATH" "$shell" $DRY --version "$VERSION"
    [ "$RC" = 0 ] || fail "the pinned hand-off exited $RC"
    expect "using the v$VERSION installer" "a pin did not hand off"
    fetched "github.com/AISquare-Studio/aisquare-cli/releases/download/v$VERSION/install.sh" \
        "the pinned release's installer was not fetched"

    head1 "$shell: piped, as curl … | sh runs it"
    : >"$WORK/fetch.log"
    set +e
    # shellcheck disable=SC2086
    cat "$ROOT/install.sh" |
        env PATH="$CURL_PATH" HOME="$WORK/home" TMPDIR="$WORK/tmp" NO_COLOR=1 \
            $shell -s -- $DRY >"$WORK/out" 2>&1
    RC=$?
    set -e
    cat "$WORK/out"
    [ "$RC" = 0 ] || fail "the piped hand-off exited $RC"
    expect "using the v$VERSION installer" "a piped run did not hand off"
    [ -z "$(ls -A "$WORK/tmp")" ] || fail "the piped run left its download behind"

    head1 "$shell: a real 404 falls back to this script"
    falls_back_on_404 "$CURL_PATH" "$shell"
    asked_pypi_once
done

# wget, where there is no curl: `-t 1 -T 30` (GNU wget would otherwise try 20
# times), and its own words for a 404. GNU wget is on GitHub's Ubuntu runners;
# BusyBox's is Alpine's only downloader. Their PATH is the wrapper and the tools
# above and nothing else, so every command named from outside it (the shell,
# busybox) is given by its full path.
DASH=$(command -v dash) || fail "this needs dash"
WGETS=""
command -v wget >/dev/null 2>&1 && WGETS="gnu"
busybox --list 2>/dev/null | grep -qx wget && WGETS="$WGETS busybox"
[ -n "$WGETS" ] || head1 "no wget here: curl only"
for kind in $WGETS; do
    real=$(command -v wget || true)
    [ "$kind" = busybox ] && real="$(command -v busybox) wget"
    make_wrapper "$WORK/wget-$kind" wget "$real"
    WGET_PATH="$WORK/wget-$kind:$WORK/tools"

    head1 "$kind wget: main's script hands off"
    # shellcheck disable=SC2086
    installer "$WGET_PATH" "$DASH" $DRY
    [ "$RC" = 0 ] || fail "the $kind wget hand-off exited $RC"
    expect "using the v$VERSION installer" "$kind wget did not hand off"
    fetched "-t 1 -T 30" "$kind wget was not limited to one try"

    head1 "$kind wget: a real 404 falls back to this script"
    falls_back_on_404 "$WGET_PATH" "$DASH"
done

head1 "the hand-off works end to end"
