"""``--reinit`` resets config.toml, and config.toml now holds the seat map.

``init`` promises to be "idempotent and non-interactive: safe to re-run", and
that is true — of ``init``. ``init --reinit`` writes a default ``AppConfig()``
over the file, and since 2026-08-17 that file carries ``[team.profiles.<role>]``
bindings: the per-seat ``CLAUDE_CONFIG_DIR``/``CLAUDE_CODE_TMPDIR`` map that a
human sets up once with five ``aisquare team bind`` commands.

Measured before this was written: five bound seats, ``init --reinit``, zero
seats — exit 0, and not one word in the output about profiles, seats, or a
reset. The line it does print is "✓ aisquare already initialized", which reads
as reassurance at the moment the seat map is discarded.

Nothing here changes what ``--reinit`` DOES. Resetting config is the flag's
job. What changed is the cost of that job, and the only place a user meets the
flag is its one-line help.
"""

from __future__ import annotations

from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import load_config
from aisquare.services import settings as settings_service

_SEATS = {"CLAUDE_CONFIG_DIR": "$HOME/.claude2", "CLAUDE_CODE_TMPDIR": "$HOME/.cache/claude2"}


def test_reinit_discards_bound_seats(runner: CliRunner) -> None:
    """The behaviour the help text has to warn about, pinned.

    If ``--reinit`` is ever changed to PRESERVE profiles, this fails — and the
    warning added alongside it should then be removed rather than left lying.
    """
    runner.invoke(app, ["init"], catch_exceptions=False)
    settings_service.bind_role("coder2", env=dict(_SEATS))
    assert sorted(load_config().team.profiles) == ["coder2"], "the fixture must bind something"

    result = runner.invoke(app, ["init", "--reinit"], catch_exceptions=False)

    assert result.exit_code == 0
    assert load_config().team.profiles == {}, "reinit is expected to reset config"


def test_the_reinit_help_names_what_a_reset_costs(runner: CliRunner) -> None:
    """A user meets this flag in exactly one place: one line of ``--help``.

    "Re-run setup even if already initialised" describes the trigger and not
    the consequence, next to a command documented as safe to re-run. The help
    has to say that config is reset and that the seat bindings go with it.
    """
    # Rich wraps help to the terminal width and breaks lines mid-sentence, so
    # compare on collapsed whitespace rather than on the rendered layout — the
    # renderer's width is not part of the claim (that mistake cost two false
    # positives on this board tonight).
    #
    # Collapsing whitespace handles WRAPPING. It does not handle TRUNCATION,
    # which is what Rich does to an options panel below roughly 70 columns: the
    # flag name itself is dropped and this assertion fails on the one thing it
    # is not about. Measured: green at COLUMNS=80, red at 60 and at 40 — and CI
    # runs narrower than a developer's terminal, which is why this passed
    # locally and failed there. So the width is pinned rather than inherited.
    help_text = runner.invoke(
        app, ["init", "--help"], catch_exceptions=False, env={"COLUMNS": "200"}
    ).output
    flat = " ".join(help_text.split())

    assert "--reinit" in flat
    for named in ("config.toml", "team bind"):
        assert named in flat, f"the --reinit help does not name {named!r}"
