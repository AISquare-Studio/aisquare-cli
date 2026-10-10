# Sourced, never run, by docs/demo.tape's hidden opening: it sets up the shell
# the tape then types into. It leaves:
#   - an empty demo home: no ~/.aisquare and no agent config, a machine that has
#     never run aisquare;
#   - ~/acme-api, a small git repository with one commit, to onboard;
#   - the stand-in agent (docs/demo/stand-in/claude) first on PATH.
#
# Inside the render image only (docs/demo/Dockerfile sets AISQUARE_DEMO_IMAGE).
# Anywhere else it refuses: it re-points PATH and writes into $HOME.
#
# One function, so each refusal returns whether the file is sourced or run. Run
# as a script, a bare top-level `return` is an error bash prints and carries on
# past, into the writes below.

aisquare_demo_seed() {
    if [ "${AISQUARE_DEMO_IMAGE:-}" != 1 ]; then
        echo "seed.sh: runs only inside the demo image (docs/demo/Dockerfile)" >&2
        return 1
    fi
    if [ ! -e docs/demo/stand-in/claude ]; then
        echo "seed.sh: source it from the repository root, where docs/demo.tape runs" >&2
        return 1
    fi
    if [ ! -x docs/demo/stand-in/claude ]; then
        echo "seed.sh: docs/demo/stand-in/claude is not executable (chmod +x it; git stages" \
            "it as 100755; a noexec mount does this too)" >&2
        return 1
    fi
    for state in .aisquare .claude .codex acme-api; do
        if [ -e "$HOME/$state" ]; then
            echo "seed.sh: $HOME/$state exists; the demo starts from an empty home" >&2
            return 1
        fi
    done

    # Exported before `asq` starts: the fleet's tmux server is started by asq,
    # and every agent window inherits the SERVER's environment, not the shell's.
    export PATH="$PWD/docs/demo/stand-in:$PATH"
    # The model-availability probe would start the stand-in to ask a question it
    # cannot answer; skip it, as an offline machine does.
    export AISQUARE_HARNESS_PROBE=0
    unset AISQUARE_HOME CLAUDE_CONFIG_DIR

    mkdir -p "$HOME/acme-api/src/acme" "$HOME/acme-api/tests" || return 1
    cat >"$HOME/acme-api/README.md" <<'EOF'
# acme-api

The small HTTP service the aisquare demo onboards.
EOF
    cat >"$HOME/acme-api/src/acme/app.py" <<'EOF'
def health() -> dict[str, str]:
    return {"status": "ok"}
EOF
    cat >"$HOME/acme-api/tests/test_app.py" <<'EOF'
from acme.app import health


def test_health() -> None:
    assert health() == {"status": "ok"}
EOF
    git -C "$HOME/acme-api" init -q -b main || return 1
    git -C "$HOME/acme-api" add -A || return 1
    git -C "$HOME/acme-api" -c user.name=demo -c user.email=demo@example.invalid \
        commit -q -m "acme-api: a health endpoint and its test" || return 1

    cd "$HOME" || return 1
    # The tape waits for this line, so a seed that refused fails the render at
    # once, with its reason on screen, instead of a walkthrough later.
    echo "seeded: an empty home, ~/acme-api, and the stand-in agent on PATH"
}

aisquare_demo_seed
