"""``init --reinit`` may reset config; it may not do it silently to a cutover.

Measured at head: a configured explainability section — `enabled`, a target, and
the whole `[explainability.targets]` table holding a gateway URL and key-env
mapping — is replaced by defaults, exit 0, no warning. `status` afterwards reads
as a plausible *unconfigured* machine rather than a broken one, so nothing
downstream reports the loss.

That inverts our own doctrine. We hold "nothing ships before the user configured
it"; this silently *un*configures.

WHY REFUSE RATHER THAN PRESERVE OR WARN:

* **Preserve** contradicts the flag. Its help says "resetting config.toml to
  defaults", and it already discards role bindings. Keeping explainability while
  dropping bindings is a split no operator could predict, and it would break
  ``--reinit`` as a way to clear a bad explainability config.
* **Warn after** does not stop the loss. The targets table holds state
  configured out of band — a gateway URL and a key-env name — which the operator
  may not be able to reconstruct from anything on the machine.
* **Refuse unless consent** stops the silent case and keeps the flag's purpose
  intact for anyone who means it. It reuses ``--yes``, which already exists and
  is documented as answering every prompt, rather than inventing a second flag.

A file it cannot read is refused too. That once looked like the case where
refusing would strand someone: ``doctor`` sends an operator with an invalid
config to the reset, and a section nobody can read seemed to have nothing to
protect. It did. A config.toml PowerShell 5.1 saved as UTF-16 holds everything
the operator configured, and a plain ``--reinit`` replaced it with the defaults
(review of the #203 final-review fixes). Such a file is read now, and refused
like any other. One this build still cannot read needs ``--yes``, and
``doctor``'s fix says so, so its recovery is still one command.
"""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilityTarget, load_config, save_config


def _configured_cutover() -> None:
    """What an operator has after `explainability enable --target prod …`."""
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "prod"
    config.explainability.targets["prod"] = ExplainabilityTarget(
        gateway_url="https://gateway.invalid",
        api_key_env="PROD_KEY_VAR",
    )
    save_config(config)


def test_reinit_refuses_when_it_would_discard_a_configured_cutover(
    runner: CliRunner,
) -> None:
    """The defect: exit 0 and a gone targets table. Now it stops."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()

    result = runner.invoke(app, ["init", "--reinit"], catch_exceptions=False)

    assert result.exit_code != 0
    assert "explainability" in result.output.lower()
    surviving = load_config().explainability
    assert surviving.enabled is True, "the refusal must not half-apply"
    assert "prod" in surviving.targets, "the targets table is the irreplaceable part"
    assert surviving.targets["prod"].gateway_url == "https://gateway.invalid"


def test_the_refusal_names_what_would_be_lost_and_how_to_proceed(
    runner: CliRunner,
) -> None:
    """A refusal an operator cannot act on is just a wall."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()

    result = runner.invoke(app, ["init", "--reinit"], catch_exceptions=False)

    assert "prod" in result.output, "name the target that would go"
    assert "--yes" in result.output, "say how to proceed deliberately"


def test_consent_still_resets_and_says_what_it_removed(runner: CliRunner) -> None:
    """`--reinit --yes` keeps doing its job — this is a consent gate, not a veto."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()

    result = runner.invoke(app, ["init", "--reinit", "--yes"], catch_exceptions=False)

    assert result.exit_code == 0
    after = load_config().explainability
    assert after.targets == {}, "consent means the reset actually happens"
    assert after.enabled is False
    assert "explainability" in result.output.lower(), "say what was removed"


def test_reinit_is_unchanged_on_a_machine_with_nothing_configured(
    runner: CliRunner,
) -> None:
    """The common path must not gain a failure. A fresh machine has nothing to lose."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)

    result = runner.invoke(app, ["init", "--reinit"], catch_exceptions=False)

    assert result.exit_code == 0, result.output


#: How Windows writes a config.toml an operator edited there: PowerShell 5.1's ``>`` and
#: ``Out-File`` (UTF-16, little-endian, with its mark), its ``Set-Content -Encoding
#: UTF8`` and Notepad's "UTF-8 with BOM", and UTF-16 big-endian for completeness.
WINDOWS_ENCODINGS = {
    "utf-16-le-bom": lambda text: codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
    "utf-16-be-bom": lambda text: codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
    "utf-8-bom": lambda text: text.encode("utf-8-sig"),
}


@pytest.mark.parametrize("encoding", sorted(WINDOWS_ENCODINGS))
def test_reinit_refuses_to_discard_a_cutover_saved_by_windows(
    runner: CliRunner, encoding: str
) -> None:
    """A config.toml PowerShell 5.1 saved as UTF-16 could not be read, so the refusal
    could not see its explainability section, and the reset replaced it with the defaults:
    exit 0 and the targets table gone (review of the #203 final-review fixes). Read in
    the encoding its mark names, it is refused like any other, and left as it was."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()
    config = paths.config_path()
    saved = WINDOWS_ENCODINGS[encoding](config.read_text(encoding="utf-8"))
    config.write_bytes(saved)

    result = runner.invoke(app, ["init", "--reinit"], catch_exceptions=False)

    assert result.exit_code != 0, result.output
    assert "explainability" in result.output.lower() and "prod" in result.output
    assert config.read_bytes() == saved, "the refusal must not half-apply"
    surviving = load_config().explainability
    assert surviving.enabled is True
    assert surviving.targets["prod"].gateway_url == "https://gateway.invalid"

    consented = runner.invoke(app, ["init", "--reinit", "--yes"], catch_exceptions=False)

    assert consented.exit_code == 0, consented.output
    assert load_config().explainability.targets == {}
    config.read_bytes().decode("utf-8")  # written back as UTF-8


@pytest.mark.parametrize(
    "raw",
    [
        b"this is not [valid toml",
        # UTF-16 WITHOUT its mark: nothing says how to decode it.
        'profile = "work"\n'.encode("utf-16-le"),
    ],
    ids=["invalid-toml", "utf-16-without-a-mark"],
)
def test_a_config_it_cannot_read_is_reset_only_with_consent(runner: CliRunner, raw: bytes) -> None:
    """``doctor`` sends an operator with a config.toml it cannot read to the reset. The
    reset replaced such a file on a plain ``--reinit``, on the grounds that a section
    nobody can read has nothing to protect, which is how a UTF-16 cutover was lost. What
    the file holds cannot be seen, so ``--reinit`` refuses, names ``--yes``, and leaves
    it; ``--reinit --yes`` replaces it with the defaults and says so."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    config = paths.config_path()
    config.write_bytes(raw)

    refused = runner.invoke(app, ["init", "--reinit"], catch_exceptions=False)

    assert refused.exit_code != 0, refused.output
    assert "--yes" in refused.output and "cannot be read" in " ".join(refused.output.split())
    assert config.read_bytes() == raw, "a refusal leaves the file as it was"

    consented = runner.invoke(app, ["init", "--reinit", "--yes"], catch_exceptions=False)

    assert consented.exit_code == 0, consented.output
    assert "could not read" in " ".join(consented.output.split())
    assert load_config() == AppConfig()


def test_plain_init_never_refuses(runner: CliRunner, tmp_path: Path) -> None:
    """Only `--reinit` resets, so only `--reinit` can be refused."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()

    result = runner.invoke(app, ["init"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "prod" in load_config().explainability.targets


def test_consent_still_discards_role_bindings(runner: CliRunner) -> None:
    """Prove `--reinit --yes` resets everything it is supposed to, not just some.

    Trading a silent surprise for a partial reset would be the same defect in a
    new coat.
    """
    from aisquare.services import settings as settings_service

    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    settings_service.bind_role("coder", env={"CLAUDE_CONFIG_DIR": "$HOME/.claude2"})
    _configured_cutover_with_bindings = load_config()
    assert _configured_cutover_with_bindings.team.profiles, "fixture must bind something"

    runner.invoke(app, ["init", "--reinit", "--yes"], catch_exceptions=False)

    assert load_config().team.profiles == {}


@pytest.mark.parametrize("shape", ["targets", "enabled", "gateway"])
def test_each_configured_shape_triggers_the_refusal(runner: CliRunner, shape: str) -> None:
    """Configured is more than one field, so the check cannot key on one.

    A machine mid-cutover may have any of these set; keying on `targets` alone
    would let a half-configured machine be reset silently.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    config = AppConfig()
    if shape == "targets":
        config.explainability.targets["stg"] = ExplainabilityTarget()
    elif shape == "enabled":
        config.explainability.enabled = True
    else:
        config.explainability.gateway_url = "https://gateway.invalid"
    save_config(config)

    result = runner.invoke(app, ["init", "--reinit"], catch_exceptions=False)

    assert result.exit_code != 0, f"{shape} left a machine resettable in silence"
