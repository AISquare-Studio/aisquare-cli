"""A build must not silently delete config it does not understand.

Measured on this machine at 12:30, two builds and one file: the train writes a
config carrying ``target``, ``roles``, ``ship``, ``gateway_url`` and
``[explainability.targets]``; the stale installed build loads and saves the same
file and ALL FIVE ARE GONE — exit 0, no warning. Its
``ExplainabilitySettings`` has three fields where the train's has eight, so it
discards what it cannot represent and writes back only what it knows.

Because ``ship`` defaults to false and the tracing seam is deliberately
fail-open, the result is not an error. It is no tracing, no shipping, and a
machine that looks configured.

WHAT THIS FIX DOES NOT DO, stated first so nobody reads it as closing the live
hazard: it cannot help against the build that is stale TODAY, because that build
predates it. It makes every FUTURE schema addition survive an older writer, and
the current incident is closed by reinstalling per the runbook's §0.

The delete case matters as much as the keep case. `team bind --clear` and
`init --reinit` remove things on purpose, so the model has to stay authoritative
for every field it owns — including when the value is absent.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from aisquare.core.config import AppConfig, RoleLaunchProfile, load_config, save_config


def _write(target: Path, body: str) -> None:
    target.write_text(body, encoding="utf-8")


def _sections(target: Path) -> dict[str, object]:
    with target.open("rb") as handle:
        data: dict[str, object] = tomllib.load(handle)
    return data


def test_a_section_this_build_never_heard_of_survives_a_save(tmp_path: Path) -> None:
    """The simple case: a whole table with no matching field."""
    target = tmp_path / "config.toml"
    _write(target, 'profile = "default"\n\n[future_feature]\nsomething = 42\n')

    save_config(load_config(target), target)

    assert _sections(target).get("future_feature") == {"something": 42}, (
        "a top-level section written by a newer build was deleted by this one"
    )


def test_an_unknown_key_INSIDE_a_known_section_survives(tmp_path: Path) -> None:
    """The shape the harm actually took, and the reason a shallow merge is not enough.

    Every one of the five keys lost on this machine was a SUB-key of
    ``[explainability]`` — a section both builds knew about. A merge that only
    preserved unrecognised top-level tables would have kept nothing at all.
    """
    target = tmp_path / "config.toml"
    _write(
        target,
        'profile = "default"\n\n[explainability]\nenabled = false\n'
        'a_field_from_the_future = "keep me"\n',
    )

    save_config(load_config(target), target)

    explainability = _sections(target)["explainability"]
    assert isinstance(explainability, dict)
    assert explainability.get("a_field_from_the_future") == "keep me", (
        "an unknown key inside a known section was dropped — this is exactly the "
        "shape that erased target/roles/ship/gateway_url"
    )
    assert explainability["enabled"] is False, "the known field must still be written"


def test_the_model_still_wins_for_fields_it_owns(tmp_path: Path) -> None:
    """Preservation must not resurrect what an operator deliberately removed.

    `team bind --clear` and `init --reinit` delete bindings. If a save merged the
    old file back in wholesale, a cleared binding would reappear and the command
    would look like it worked while changing nothing.

    A REGRESSION GUARD, not proof of the fix: it passes before and after by
    design, because what it protects is behaviour the fix must not break.
    """
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.team.profiles["coder1"] = RoleLaunchProfile(env={"CLAUDE_CONFIG_DIR": "$HOME/.claude2"})
    save_config(config, target)
    assert "coder1" in str(_sections(target)["team"])

    cleared = load_config(target)
    cleared.team.profiles.pop("coder1", None)
    save_config(cleared, target)

    assert "coder1" not in str(_sections(target)["team"]), (
        "a removed binding came back — the model is no longer authoritative for "
        "fields it owns, so clear/reinit have silently stopped working"
    )


def test_an_unparseable_existing_file_does_not_block_the_write(tmp_path: Path) -> None:
    """Reading the old file fails open, because a broken config is repairable.

    A write is the most likely thing to be REPAIRING a corrupt file, so refusing
    to write when the old one cannot be parsed would strand the operator with
    exactly the state they are trying to leave.

    A REGRESSION GUARD too: before the fix there was no read at all, so it could
    not fail. It exists because the fix ADDED a read that could.
    """
    target = tmp_path / "config.toml"
    _write(target, "this is not [ valid toml\n")

    save_config(AppConfig(), target)

    assert load_config(target).profile == "default"


def test_unknown_keys_survive_a_round_trip_through_this_build(tmp_path: Path) -> None:
    """End to end, in the direction the incident actually ran.

    A newer build writes fields this one cannot represent; this build then does
    something ordinary — the equivalent of `config set` — and the newer fields
    must still be there afterwards.

    The field names are DELIBERATELY ones this build does not have. The first
    draft of this test used ``ship``/``target``/``roles``, the real names from
    the incident — and it passed without the fix, because those are exactly the
    fields THIS build knows. A test for unknown-key preservation cannot be
    written with keys the running model recognises; it reads as the real
    scenario and proves nothing. Verified red against the unmerged write.
    """
    target = tmp_path / "config.toml"
    _write(
        target,
        'profile = "default"\n\n[explainability]\nenabled = false\n'
        'sampling_rate = 0.5\nredaction_profile = "strict"\n\n'
        "[explainability.retry]\nattempts = 3\n",
    )

    config = load_config(target)
    config.profile = "changed"
    save_config(config, target)

    explainability = _sections(target)["explainability"]
    assert isinstance(explainability, dict)
    for key in ("sampling_rate", "redaction_profile", "retry"):
        assert key in explainability, f"{key} was erased by an ordinary write"
    assert _sections(target)["profile"] == "changed", "the actual edit did not land"
