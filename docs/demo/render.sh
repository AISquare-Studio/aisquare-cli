#!/bin/sh
# Render docs/demo.tape into out/: demo.gif, demo.txt (vhs's snapshot of the
# screen after each command of the tape) and welcome.png (the still).
#
# `make demo` runs this, and so does .github/workflows/demo.yml, so a laptop and
# CI render the same way, in the image docs/demo/Dockerfile builds. It needs
# docker, rootful or rootless, and nothing else: the image builds aisquare from
# this tree, and the checks run on the image's own Python.
#
# Outside this checkout it touches only docker's image store, where the image is
# tagged for this checkout (aisquare-demo-<cksum of its path>, or $DEMO_IMAGE),
# so two checkouts rendering at once never run each other's tree. The
# container's HOME is out/home, emptied on every run, and its only other mount
# is this checkout, for the tape and the outputs.
#
# It fails when:
#   - a Wait in the tape times out (vhs exits 1 and prints the screen it gave up on);
#   - a snapshot shows a traceback, or the last one is not the screen the
#     walkthrough ends on (python -m tests.demo_tape);
#   - doctor, asked again in the home the walkthrough left, crashes or fails a
#     check (its first run's log is in no snapshot);
#   - the GIF is over 4 MiB after gifsicle (GitHub's image proxy stops at 5 MB).
set -eu

cd "$(dirname "$0")/../.."
image=${DEMO_IMAGE:-aisquare-demo-$(pwd -P | cksum | cut -d ' ' -f 1)}
limit=4194304

docker build --file docs/demo/Dockerfile --tag "$image" .

rm -rf out/home out/demo.gif out/demo.txt out/welcome.png
mkdir -p out/home

# Rootful docker shows this checkout as owned by your uid: run as it, so out/ is
# never left owned by root. Rootless docker and podman show it as root's, and
# root in the container IS you there; any other uid maps to one that can write
# neither out/ nor HOME (measured in the review of #250). :z relabels the mounts
# for SELinux engines, podman on Fedora among them, and does nothing elsewhere.
owner=$(docker run --rm --user 0 --entrypoint stat \
    --volume "$PWD/out/home:/home/demo:z" "$image" -c %u /home/demo)
if [ "$owner" = 0 ]; then as=0:0; else as="$(id -u):$(id -g)"; fi

in_image() {
    docker run --rm --user "$as" \
        --volume "$PWD/out/home:/home/demo:z" --volume "$PWD:/vhs:z" "$@"
}

in_image "$image" docs/demo.tape
# The end screen shows the coders the fleet labels coder-1 and coder-2, names no
# Wait can take from the source (they are made at spawn), so they are passed here.
in_image --entrypoint python3 "$image" -B -m tests.demo_tape out/demo.txt docs/demo.tape \
    coder-1 coder-2
# The Onboard view's log of init and doctor is on screen only while the tape
# waits for the project view, so no snapshot holds it, and a doctor that crashed
# there rendered green (review of #250). Ask doctor again, with the command
# onboarding ran, in the home and with the PATH the walkthrough used: a
# traceback, or a check that fails (the GIF's sidebar would show it), fails the
# render here, with doctor's report or traceback just above. The quotes are
# single on purpose: $PATH is the container's, expanded in there.
# shellcheck disable=SC2016
in_image --workdir /home/demo/acme-api --entrypoint sh "$image" -c \
    'PATH=/vhs/docs/demo/bin:$PATH AISQUARE_HARNESS_PROBE=0 aisquare --json doctor'
in_image --entrypoint gifsicle "$image" --batch -O3 --lossy=60 out/demo.gif

size=$(($(wc -c <out/demo.gif)))
if [ "$size" -gt "$limit" ]; then
    echo "out/demo.gif is $size bytes, over the limit of $limit" >&2
    exit 1
fi
echo "out/demo.gif: $size bytes (limit $limit)"
