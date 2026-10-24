#!/bin/sh
# The hand-off, end to end on a real runner: main's install.sh (this tree,
# unstamped) downloads a release's own stamped install.sh with the REAL curl and
# runs it. .github/workflows/install.yml's `handoff` job runs this.
#
# What tests/test_install_script_handoff.py cannot show and this can: the real
# curl (its flags, its exit codes, a real HTTP 404), the real mktemp and traps,
# and main's script run by dash and by BusyBox ash, as /bin/sh is on Debian and
# Alpine. What it cannot reach is GitHub itself. The release is THIS tree
# stamped by scripts/stamp_installers.py and served by a local http.server, and
# a curl wrapper first on PATH points the release and PyPI URLs at it; every
# other URL reaches the real curl unchanged.
#
# Every run is a --dry-run, so nothing is installed.
#
# Usage, from the repository root (needs python3, curl, dash; busybox if there):
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

REAL_CURL=$(command -v curl) || fail "this needs curl"
cat >"$WORK/bin/curl" <<WRAPPER
#!/bin/sh
# Points the release and PyPI URLs at $LOCAL; the rest goes as it came.
for arg do
    shift
    case "\$arg" in
        https://github.com/AISquare-Studio/aisquare-cli/releases/*)
            arg="$LOCAL/releases/\${arg#https://github.com/AISquare-Studio/aisquare-cli/releases/}"
            ;;
        https://pypi.org/pypi/aisquare-cli/json) arg="$LOCAL/pypi/aisquare-cli/json" ;;
    esac
    set -- "\$@" "\$arg"
done
printf '%s\n' "\$*" >>"$WORK/curl.log"
exec "$REAL_CURL" "\$@"
WRAPPER
chmod +x "$WORK/bin/curl"

DRY="--dry-run --yes --no-project --no-agent --no-system-deps"

# One run of main's install.sh: $1 is the shell, the rest are its arguments.
# Output in $WORK/out, status in $RC, and the TMPDIR must be empty after.
installer() {
    _shell=$1
    shift
    : >"$WORK/curl.log"
    set +e
    # shellcheck disable=SC2086  # the shell is "busybox ash" for one run.
    env PATH="$WORK/bin:$PATH" HOME="$WORK/home" TMPDIR="$WORK/tmp" NO_COLOR=1 \
        $_shell "$ROOT/install.sh" "$@" >"$WORK/out" 2>&1
    RC=$?
    set -e
    cat "$WORK/out"
    [ -z "$(ls -A "$WORK/tmp")" ] || fail "the run left $(ls -A "$WORK/tmp") in its TMPDIR"
}

expect() { grep -qF -e "$1" "$WORK/out" || fail "$2"; }

SHELLS="dash"
if command -v busybox >/dev/null 2>&1; then
    SHELLS="$SHELLS busybox_ash"
else
    head1 "no busybox here: dash only"
fi

for name in $SHELLS; do
    shell=$name
    [ "$name" = busybox_ash ] && shell="busybox ash"

    head1 "$shell: main's script hands off to the latest release ($VERSION)"
    # shellcheck disable=SC2086
    installer "$shell" $DRY
    [ "$RC" = 0 ] || fail "the handed-off dry run exited $RC"
    [ "$(head -n 1 "$WORK/out")" = "  using the v$VERSION installer" ] ||
        fail "the first line is not the hand-off"
    expect "pinned to $VERSION" "the release's installer did not pin its own version"
    # `==` on a machine without aisquare, `@` on one that has it.
    grep -q -e "aisquare-cli==$VERSION" -e "aisquare-cli@$VERSION" "$WORK/out" ||
        fail "the release's installer did not install $VERSION"
    expect "nothing above was actually run" "--dry-run was not passed through"
    grep -q "$LOCAL/releases/latest/download/install.sh" "$WORK/curl.log" ||
        fail "the latest release's installer was not fetched"
    grep -q -e '--max-time' "$WORK/curl.log" || fail "the download had no timeout"

    head1 "$shell: --version $VERSION fetches that release's installer"
    # shellcheck disable=SC2086
    installer "$shell" $DRY --version "$VERSION"
    [ "$RC" = 0 ] || fail "the pinned hand-off exited $RC"
    expect "using the v$VERSION installer" "a pin did not hand off"
    grep -q "$LOCAL/releases/download/v$VERSION/install.sh" "$WORK/curl.log" ||
        fail "the pinned release's installer was not fetched"

    head1 "$shell: piped, as curl … | sh runs it"
    : >"$WORK/curl.log"
    set +e
    # shellcheck disable=SC2086
    cat "$ROOT/install.sh" |
        env PATH="$WORK/bin:$PATH" HOME="$WORK/home" TMPDIR="$WORK/tmp" NO_COLOR=1 \
            $shell -s -- $DRY >"$WORK/out" 2>&1
    RC=$?
    set -e
    cat "$WORK/out"
    [ "$RC" = 0 ] || fail "the piped hand-off exited $RC"
    expect "using the v$VERSION installer" "a piped run did not hand off"
    [ -z "$(ls -A "$WORK/tmp")" ] || fail "the piped run left its download behind"

    head1 "$shell: a real 404 falls back to this script"
    mv "$LATEST/install.sh" "$WORK/held.sh"
    # shellcheck disable=SC2086
    installer "$shell" $DRY
    mv "$WORK/held.sh" "$LATEST/install.sh"
    [ "$RC" = 0 ] || fail "the fallback dry run exited $RC"
    expect "using this installer (the latest release has no installer attached)" \
        "a 404 did not fall back in one line"
    expect "nothing above was actually run" "main's own dry run did not run"
    if grep -qF "pinned to" "$WORK/out"; then
        fail "the fallback pinned a version, so the release's script ran after all"
    fi
done

head1 "the hand-off works end to end"
