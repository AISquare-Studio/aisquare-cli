"""CONTRIBUTING tells you to build an editable checkout; it must name the footgun.

``make install`` is an EDITABLE install. Installing the explainability extra
into one shadows the checkout, and every command afterwards dies with
``No module named 'aisquare.cli'``. The CLI already warns about this — but only
from inside an explainability command, and by that point the reader has usually
already typed the thing that breaks it. Worse, once the extra is in, the surface
that would explain the failure is the surface that no longer starts, so no
diagnostic of ours can ever fire.

The operator document already carries the equivalent warning (the cutover
runbook installs ``'.[dev]'`` with ``# NOT -e / --editable``). Only the
CONTRIBUTOR document was silent, which is the asymmetry this pins.

The strings are EXTRACTED FROM THE CODE rather than retyped, so the page cannot
drift from the hint a contributor actually sees. Both extractions assert on
their own yield first: a hint reworded so the extraction returns nothing would
otherwise turn this file vacuously green, which is the failure mode a
documentation guard has to rule out before it can be evidence of anything.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.services.explainability import EDITABLE_INSTALL_HINT, INSTALL_HINT

_PAGE = Path(__file__).resolve().parents[1] / "CONTRIBUTING.md"


def _recovery_command() -> str:
    """The command the CLI tells you to run once you have already broken it."""
    _, _, tail = EDITABLE_INSTALL_HINT.partition("recover with: ")
    recovery = tail.strip().rstrip(".")
    assert recovery.startswith("pip "), (
        "EDITABLE_INSTALL_HINT no longer ends with a 'recover with: <command>' "
        f"clause — extracted {recovery!r}, so this whole file would pass vacuously"
    )
    return recovery


def _extra_spec() -> str:
    """The ``name[extra]`` token, taken from the hint that recommends it."""
    start = INSTALL_HINT.find("aisquare-cli[")
    assert start != -1, f"INSTALL_HINT no longer names the extra: {INSTALL_HINT!r}"
    end = INSTALL_HINT.index("]", start) + 1
    return INSTALL_HINT[start:end]


def test_contributing_still_prescribes_an_editable_install() -> None:
    """The warning is only correct while the instruction that needs it stands.

    If setup ever moves to a non-editable install the footgun disappears and the
    paragraph below becomes wrong rather than merely unnecessary — so this is
    the premise, asserted, not assumed.
    """
    assert "make install" in _PAGE.read_text(encoding="utf-8")


def test_contributing_names_the_extra_that_breaks_an_editable_checkout() -> None:
    """Naming the exact spec matters: it is what a reader pastes into pip."""
    page = _PAGE.read_text(encoding="utf-8")

    assert _extra_spec() in page, (
        f"CONTRIBUTING does not name {_extra_spec()!r}, so a contributor cannot "
        "recognise the command that will shadow their checkout"
    )


def test_contributing_carries_the_recovery_the_cli_would_have_told_them() -> None:
    """The one thing that cannot be discovered after the fact.

    Every other hint in this product is delivered by a command. This one cannot
    be: once the extra is installed, ``aisquare`` does not start, so the advice
    has to already be on the page the contributor read.
    """
    page = _PAGE.read_text(encoding="utf-8")

    assert _recovery_command() in page, (
        f"CONTRIBUTING must carry {_recovery_command()!r} verbatim — it is the "
        "recovery for a state in which no command of ours can run to suggest it"
    )
