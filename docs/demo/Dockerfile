# The image docs/demo.tape is rendered in, by docs/demo/render.sh, which both
# `make demo` and .github/workflows/demo.yml run. The base is pinned by digest,
# so a laptop and CI render with the same VHS, Chromium and base fonts.
#
# What is added below is not pinned, on purpose. apt's packages come from the
# base's Debian release, and aisquare's PyPI dependencies from pyproject.toml's
# ranges, both resolved when the image is built, as ci.yml resolves them: the
# demo runs what a fresh install gets. A laptop reuses its cached layers where
# CI builds fresh (`docker build --no-cache` matches it), and no two renders'
# GIFs are byte-identical anyway, because vhs records live.
#
# Built with the repository as context, narrowed by Dockerfile.dockerignore
# beside this file to the four things the build reads: aisquare is installed
# from this tree as it is now, not from PyPI.

# VHS v0.12.1, pinned by digest: a tag can move, a digest cannot.
FROM ghcr.io/charmbracelet/vhs:v0.12.1@sha256:ea49a6a1c529be83153e88321892b5585964418f0b9055e8c1e0d732194234a3

# What the stock image lacks, each measured missing:
#   tmux             the fleet's substrate; Start manager opens a window in it
#   git              the sample project is a repository, as a real one is
#   python3-venv     the image's Python 3.13 has no pip and no ensurepip
#   fonts-noto-cjk   U+FF0B, the sidebar's "＋ spawn agent", drew as an empty box
#   gifsicle         render.sh shrinks the GIF with it (about a third, measured)
# The base's apt sources name the suite `stable`, which is trixie today and moves
# at Debian 14's release; apt then refuses the base's cached lists ("changed its
# 'Codename' value", measured in the review of #250) and this build would fail.
# So the suites are pinned to the base's own codename first.
RUN . /etc/os-release \
    && sed -i "s/\bstable\b/$VERSION_CODENAME/g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux git python3-venv fonts-noto-cjk gifsicle \
    && rm -rf /var/lib/apt/lists/*

# /src stays: doctor's provenance check warns about a build whose source
# directory is gone, a warning no install from PyPI shows.
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN python3 -m venv /opt/asq \
    && /opt/asq/bin/pip install --no-cache-dir --disable-pip-version-check /src

# The demo's home. render.sh mounts a fresh scratch directory over it; without
# the mount it is an empty directory any uid can write, because render.sh runs
# the container as your uid on rootful docker (so out/ is never left owned by
# root) and as root, which is you, on rootless docker.
RUN mkdir -m 1777 /home/demo

# COLORTERM: without it Textual falls back to 256 colours and the footer turns
#   navy (measured). The tape sets it too, so the tape says what it needs.
# AISQUARE_DEMO_IMAGE: docs/demo/seed.sh refuses to run anywhere else.
ENV PATH=/opt/asq/bin:$PATH \
    COLORTERM=truecolor \
    HOME=/home/demo \
    AISQUARE_DEMO_IMAGE=1
