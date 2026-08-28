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

import typer.main
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


def test_the_reinit_help_names_what_a_reset_costs() -> None:
    """A user meets this flag in exactly one place: one line of ``--help``.

    "Re-run setup even if already initialised" describes the trigger and not
    the consequence, next to a command documented as safe to re-run. The help
    has to say that config is reset and that the seat bindings go with it.

    ASKED OF THE DECLARED OPTION, NOT OF RENDERED OUTPUT, and that took three
    failures to learn. Reading `--help` back as text put Rich between the claim
    and the assertion, and Rich is not a constant: it wraps at the terminal
    width (handled, by collapsing whitespace), it TRUNCATES an options panel
    below roughly 70 columns so the flag name vanishes (green at COLUMNS=80,
    red at 60 and 40 — and CI is narrower than a developer's terminal), and
    with the width pinned wide it still rendered no `--reinit` on 3.11-3.13
    while rendering it on 3.14. Three different reasons, none of them anything
    to do with whether the help text says what a reset costs.

    The help string is what the claim is about and it is a declared attribute,
    so this reads it off the command. That gives up one thing honestly: it no
    longer proves Typer SHOWS the option. Nothing here ever proved that
    reliably — the renderer is the part that kept moving — and `--help` listing
    a declared option is Typer's job, tested by Typer.
    """
    command = typer.main.get_command(app)
    init_command = command.commands["init"]  # type: ignore[attr-defined]
    reinit = next(p for p in init_command.params if "--reinit" in p.opts)

    help_text = reinit.help or ""

    for named in ("config.toml", "team bind"):
        assert named in help_text, f"the --reinit help does not name {named!r}: {help_text!r}"
