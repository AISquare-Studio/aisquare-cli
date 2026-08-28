"""A config write must not be able to leave a torn file.

``save_config`` was ``target.write_text(...)`` straight onto the live path, and
five seats are bound on this machine as of 11:31 with each session's startup able
to write. Two writers interleaving, or one writer dying mid-write, leaves a
truncated TOML document, and ``load_config`` raises on it.

THE CONSEQUENCE IS WORST WHERE IT IS QUIETEST. Most call sites fail hard, which
is loud and therefore safe. The explainability seam is deliberately fail-open —
``cli/launch.py`` catches a broken config and prints "config unreadable —
launching untraced" — so a torn file does not crash anything, it silently drops
tracing for that launch. That is this project's north star failing through a
file-write race rather than through anything anyone would notice.

The tests are DELIBERATELY NOT RACES. A twelve-way concurrency test was how the
store migration race was nearly mis-guarded: it fired in 0-2 of 15 attempts
depending on machine load, so it asserted about the box while appearing to assert
about the code. Two of these three assert a deterministic invariant instead, and
the third is honest about being a smoke test.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.config import AppConfig, load_config, save_config


def _populated() -> AppConfig:
    """A config with enough content that a partial write is a broken document."""
    config = AppConfig()
    config.explainability.enabled = False
    config.explainability.gateway_url = "https://stg.example"
    return config


def test_an_interrupted_write_leaves_the_previous_config_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, stated as behaviour rather than as implementation.

    A writer that dies part-way must not take the existing file with it. Against
    the in-place write this FAILS: ``write_text`` truncates the target before it
    has the new bytes, so an exception mid-write leaves whatever prefix landed —
    and `load_config` then raises where it used to return a config.

    The interruption is deterministic: the first write call on the real file
    object writes a prefix and raises, which is what a dying process, a full
    disk, or a signal at the wrong moment all look like from here.
    """
    target = tmp_path / "config.toml"
    save_config(_populated(), target)
    before = target.read_text(encoding="utf-8")
    assert "gateway_url" in before, "fixture did not write a populated config"

    real_open = Path.open

    def _die_midway(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if "w" in str(args[0] if args else kwargs.get("mode", "r")):
            original_write = handle.write

            def _partial(data: Any) -> int:
                original_write(data[: max(1, len(data) // 3)])
                raise OSError("interrupted mid-write")

            handle.write = _partial
        return handle

    monkeypatch.setattr(Path, "open", _die_midway)

    changed = _populated()
    changed.explainability.gateway_url = "https://prod.example"
    with pytest.raises(OSError):
        save_config(changed, target)

    monkeypatch.undo()

    # The old config must still be there and still be loadable.
    assert target.read_text(encoding="utf-8") == before, (
        "an interrupted write modified the live file — a reader that loses this "
        "race sees a torn document instead of the previous valid one"
    )
    assert load_config(target).explainability.gateway_url == "https://stg.example"


def test_the_live_path_is_never_opened_for_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant, asserted where it cannot be load-sensitive.

    Atomicity here is a property of the SEQUENCE, not of a timing window: the
    destination is never truncated because it is never written to. New bytes go
    to a sibling temp file and arrive by ``os.replace``, which is atomic within a
    filesystem — hence "a sibling", since replace across filesystems is not.

    Pinned as an ordering invariant for the same reason the store-migration guard
    is: the racing version of this test passed against the unfixed code.
    """
    target = tmp_path / "config.toml"
    save_config(_populated(), target)

    opened_for_write: list[str] = []
    replaced: list[tuple[str, str]] = []
    real_open = Path.open
    real_replace = os.replace

    def _record_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        mode = str(args[0] if args else kwargs.get("mode", "r"))
        if any(flag in mode for flag in ("w", "a", "+", "x")):
            opened_for_write.append(str(self))
        return real_open(self, *args, **kwargs)

    def _record_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        replaced.append((str(src), str(dst)))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(Path, "open", _record_open)
    monkeypatch.setattr(os, "replace", _record_replace)

    save_config(_populated(), target)
    monkeypatch.undo()

    assert str(target) not in opened_for_write, (
        f"save_config opened the live config for writing ({opened_for_write}) — "
        "any reader arriving between truncate and write sees a partial document"
    )
    assert replaced, "no os.replace: the new bytes did not arrive atomically"
    source, destination = replaced[-1]
    assert destination == str(target)
    assert Path(source).parent == target.parent, (
        f"temp file {source} is not a sibling of {target} — os.replace is only "
        "atomic within one filesystem, so a temp dir elsewhere loses the property"
    )


def test_concurrent_writers_never_leave_an_unparseable_file(tmp_path: Path) -> None:
    """A smoke test, and labelled as one.

    It covers that the common path still ends somewhere valid under contention.
    It does NOT claim to guard the fix — reproducing a torn file by racing is
    load-dependent, and a guard that fires only on a busy machine is the shape
    this suite has already had to repair twice.
    """
    target = tmp_path / "config.toml"
    save_config(_populated(), target)
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def _writer(index: int) -> None:
        config = _populated()
        config.explainability.gateway_url = f"https://gw-{index}.example"
        barrier.wait()
        for _ in range(5):
            try:
                save_config(config, target)
                load_config(target)
            except Exception as exc:  # the point of this test is to collect them
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"concurrent save/load produced {errors[:3]}"
    assert load_config(target).explainability.gateway_url.startswith("https://gw-")
