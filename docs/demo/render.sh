#!/bin/sh
# Render docs/demo.tape into out/: demo.gif, demo.txt (the text of every frame)
# and welcome.png (the still).
#
# `make demo` runs this, and so does .github/workflows/demo.yml, so a laptop and
# CI render the same GIF the same way. It needs docker and nothing else: the
# image builds aisquare from this tree, and the frame check runs on the image's
# own Python.
#
# Nothing outside this checkout is touched. The container runs as your uid, its
# HOME is out/home (emptied on every run), and its only other mount is this
# checkout, for the tape and the outputs.
#
# It fails when:
#   - a Wait in the tape times out (vhs exits 1 and prints the last screen);
#   - a text frame shows a traceback, or the last frame is not the screen the
#     walkthrough ends on (python -m tests.demo_tape);
#   - the GIF is over 4 MiB after gifsicle (GitHub's image proxy stops at 5 MB).
set -eu

cd "$(dirname "$0")/../.."
image=${DEMO_IMAGE:-aisquare-demo}
limit=4194304

docker build --file docs/demo/Dockerfile --tag "$image" .

rm -rf out/home out/demo.gif out/demo.txt out/welcome.png
mkdir -p out/home

in_image() {
    docker run --rm --user "$(id -u):$(id -g)" \
        --volume "$PWD/out/home:/home/demo" --volume "$PWD:/vhs" "$@"
}

in_image "$image" docs/demo.tape
in_image --entrypoint gifsicle "$image" --batch -O3 --lossy=60 out/demo.gif
in_image --entrypoint python3 "$image" -B -m tests.demo_tape out/demo.txt docs/demo.tape

size=$(($(wc -c <out/demo.gif)))
if [ "$size" -gt "$limit" ]; then
    echo "out/demo.gif is $size bytes, over the limit of $limit" >&2
    exit 1
fi
echo "out/demo.gif: $size bytes (limit $limit)"
