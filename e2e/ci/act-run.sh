#!/bin/sh
# Run .github/workflows/e2e.yml's Linux leg on this machine with act, the way
# GitHub runs it: the same file and the same steps, started by the same
# workflow_dispatch with the same inputs. What act cannot reproduce is listed in
# e2e/ci/README.md, and it is worth reading before trusting a green run.
#
#   sh e2e/ci/act-run.sh                       # track main, every journey
#   sh e2e/ci/act-run.sh --smoke               # the plumbing only: a fake run
#   sh e2e/ci/act-run.sh --track release --journeys 'J01 or J02'
#   sh e2e/ci/act-run.sh --amd64               # x86_64 under emulation (slow)
#   sh e2e/ci/act-run.sh --keep                # leave a failed job's container
#   sh e2e/ci/act-run.sh -- --verbose          # anything after -- goes to act
#
# Each run gets its own directory, out/act-artifacts/<UTC time>-<track>[-smoke]/
# (out/ is gitignored), holding:
#   act-command.txt  the exact command, to re-run or to quote
#   act.log          act's whole output
#   timings.txt      one line per step: outcome, duration (act's own), step
#   server/          what act's artifact server stored, as uploaded
#   <artifact>/      each uploaded artifact, unzipped
# The artifacts are unzipped before the script exits, even when the job failed,
# because a red run is when the evidence is needed; the verdict and the
# gallery's path are printed last. The exit code is act's.
set -eu

die() {
    echo "act-run.sh: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
usage: sh e2e/ci/act-run.sh [--track main|release] [--journeys EXPR] [--smoke]
                            [--amd64] [--keep] [-- ACT_ARGS...]
  --track     main (a wheel built from this tree) or release (PyPI); default main
  --journeys  a pytest -k expression; default every journey
  --smoke     skip the suite and write a fake run (checks the plumbing)
  --amd64     run linux/amd64 under emulation instead of the native arch
  --keep      do not remove the job's container when it fails
EOF
}

track=main
smoke=false
journeys=
arch=
keep=false
while [ $# -gt 0 ]; do
    case $1 in
        --track)
            [ $# -ge 2 ] || die "--track needs a value"
            track=$2
            shift 2
            ;;
        --track=*)
            track=${1#--track=}
            shift
            ;;
        --journeys)
            [ $# -ge 2 ] || die "--journeys needs a value"
            journeys=$2
            shift 2
            ;;
        --journeys=*)
            journeys=${1#--journeys=}
            shift
            ;;
        --smoke)
            smoke=true
            shift
            ;;
        --amd64)
            arch=linux/amd64
            shift
            ;;
        --keep)
            keep=true
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            usage >&2
            die "unknown option '$1'"
            ;;
    esac
done
case $track in
    main | release) ;;
    *) die "--track is main or release, not '$track'" ;;
esac

# From anywhere in the checkout: act reads the workflow, and copies the tree it
# runs, relative to the current directory.
cd "$(dirname "$0")/../.."
[ -f .github/workflows/e2e.yml ] || die "no .github/workflows/e2e.yml under $(pwd)"

command -v act >/dev/null 2>&1 || die "act is not installed (brew install act)"
docker info >/dev/null 2>&1 || die "docker is not running"

# The image GitHub's ubuntu-24.04 label maps to, as catthehacker rebuilds it
# for act. Not pulled again when present (--pull=false below), so two runs an
# hour apart test the same machine; `docker pull` it to move forward.
image=${E2E_ACT_IMAGE:-catthehacker/ubuntu:act-24.04}

# Native by default: on Apple silicon that is arm64, which GitHub's x86_64
# runner is not, but emulating x86_64 makes Chromium and the installer several
# times slower. --amd64 is there for when an architecture difference is the
# question.
if [ -z "$arch" ]; then
    case "$(uname -m)" in
        arm64 | aarch64) arch=linux/arm64 ;;
        *) arch=linux/amd64 ;;
    esac
fi

# THE ONE ACTION THAT RUNS DIFFERENTLY UNDER ACT: upload-artifact. act's
# artifact server (0.2.89, the latest release) refuses uploads from v6 and
# later. It signs each upload URL over a timestamp that contains spaces, v6
# re-encodes that URL so the signature no longer matches ("Error unauthorized",
# and a 0-byte zip), and v7 also sends a `mime_type` field the server rejects as
# unknown. Measured in one act run: v4 and v5 uploaded, v6 and v7 failed. v5
# speaks the same protocol in a form act accepts, so whatever major the
# workflow names is pointed at a v5 checkout here, pinned by commit, while
# GitHub runs the workflow's own version. act logs the swap as
# "LocalRepositoryCache matched". Delete this once act takes the current
# upload-artifact.
upload_artifact_for_act=v5.0.0
upload_artifact_for_act_sha=330a01c490aca151604b8cf639adc76d48f6c5d4
substitute=out/act-cache/upload-artifact@$upload_artifact_for_act
if [ "$(git -C "$substitute" rev-parse HEAD 2>/dev/null || true)" != "$upload_artifact_for_act_sha" ]; then
    rm -rf "$substitute"
    git -c advice.detachedHead=false clone -q --depth 1 --branch "$upload_artifact_for_act" \
        https://github.com/actions/upload-artifact "$substitute"
    [ "$(git -C "$substitute" rev-parse HEAD)" = "$upload_artifact_for_act_sha" ] ||
        die "upload-artifact $upload_artifact_for_act is not commit $upload_artifact_for_act_sha any more"
fi
upload_refs=$(sed -n 's#^[ -]*uses: *actions/upload-artifact@\([^ #]*\).*#\1#p' .github/workflows/e2e.yml | sort -u)
[ -n "$upload_refs" ] || die "no actions/upload-artifact step in .github/workflows/e2e.yml"

run_dir=out/act-artifacts/$(date -u +%Y%m%dT%H%M%SZ)-$track
[ "$smoke" = false ] || run_dir=$run_dir-smoke
mkdir -p "$run_dir/server"

# --pull=false: see `image` above. --rm: a failed job's container is removed
# too, unless --keep; it holds a 2 GB image's worth of apt and Chromium.
#
# --container-options, two differences from GitHub's VM that the suite can see:
#   --shm-size=2g  Docker gives a container 64 MB of /dev/shm, where Chromium
#                  keeps its shared memory; the VM has gigabytes.
#   --init         act's container runs `tail -f /dev/null` as PID 1, which
#                  never reaps the orphans re-parented to it. Measured in a
#                  real run: every sudo the harness had started, and `asq`
#                  itself, sat there as zombies, and a zombie still answers
#                  `kill -0`, so a liveness check on a dead process says alive.
#                  docker's init reaps them, as the VM's systemd does.
#
# --container-daemon-socket -: act mounts this machine's Docker socket into the
# job container by default. The job needs no Docker, and the product under test
# runs there as a user with passwordless sudo, so with the socket it could reach
# every other container on this machine.
set -- act workflow_dispatch \
    --workflows .github/workflows/e2e.yml \
    --job linux \
    --platform "ubuntu-24.04=$image" \
    --container-architecture "$arch" \
    --container-options "--shm-size=2g --init" \
    --container-daemon-socket - \
    --artifact-server-path "$(pwd)/$run_dir/server" \
    --input "track=$track" \
    --input "smoke=$smoke" \
    --pull=false \
    "$@"
[ -z "$journeys" ] || set -- "$@" --input "journeys=$journeys"
[ "$keep" = true ] || set -- "$@" --rm
for ref in $upload_refs; do
    set -- "$@" --local-repository "actions/upload-artifact@$ref=$(pwd)/$substitute"
done

for arg in "$@"; do printf '%s\n' "$arg"; done >"$run_dir/act-command.txt"
echo "act-run.sh: $*"
echo "act-run.sh: writing to $run_dir/"

# The status through a file: in POSIX sh a pipeline's status is tee's, and
# tee is here so the log is kept while it is watched.
{
    set +e
    "$@" 2>&1
    echo "$?" >"$run_dir/.act-status"
} | tee "$run_dir/act.log"
# No status file means act never got to write one (Ctrl-C): still unzip below.
status=$(cat "$run_dir/.act-status" 2>/dev/null || echo 130)

# act ends every step with a line like
#   [e2e/linux (main)]   ✅  Success - Main Install ttyd 1.7.7 (…) [2.13s]
# so the timings are act's own measurements, not a second clock. -E and no \t:
# macOS's sed has neither `\|` in a basic regex nor `\t` in a replacement.
sed -n -E 's/^\[[^]]*\][^A-Za-z]*(Success|Failure|Skipped) - (.*) \[([^]]*)\]$/\1  \3  \2/p' \
    "$run_dir/act.log" >"$run_dir/timings.txt" || true

find "$run_dir/server" -type f -name '*.zip' >"$run_dir/.zips"
artifacts=0
while IFS= read -r zip; do
    name=$(basename "$zip" .zip)
    rm -rf "${run_dir:?}/$name"
    # A failed upload leaves a truncated or empty zip behind; say so and go on,
    # so the exit code below is still act's.
    if ! unzip -q -o "$zip" -d "$run_dir/$name"; then
        echo "act-run.sh: $zip is not a whole zip (the upload failed part-way?)" >&2
        continue
    fi
    artifacts=$((artifacts + 1))
    echo "act-run.sh: artifact $name -> $run_dir/$name/"
    # The report's verdict when there is one, else run.json's: both are
    # pretty-printed with "verdict" on a line of its own.
    for json in "$run_dir/$name/report/verdict.json" "$run_dir/$name/run.json"; do
        if [ -f "$json" ]; then
            verdict=$(sed -n 's/^ *"verdict": *"\([A-Z_]*\)".*/\1/p' "$json" | head -n 1)
            echo "act-run.sh:   verdict: ${verdict:-unreadable} (${json#"$run_dir/$name/"})"
            break
        fi
    done
    if [ -f "$run_dir/$name/report/gallery.html" ]; then
        echo "act-run.sh:   gallery: $run_dir/$name/report/gallery.html"
    fi
done <"$run_dir/.zips"
rm -f "$run_dir/.zips" "$run_dir/.act-status"

echo
echo "act-run.sh: act exited $status"
echo "act-run.sh: log      $run_dir/act.log"
echo "act-run.sh: timings  $run_dir/timings.txt"
if [ "$artifacts" -eq 0 ]; then
    echo "act-run.sh: no artifact was uploaded (act's server stored: $(find "$run_dir/server" -type f | wc -l | tr -d ' ') files)"
fi
exit "$status"
