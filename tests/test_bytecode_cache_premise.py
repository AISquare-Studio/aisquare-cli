"""CONTRIBUTING warns that a same-size edit can be masked by the bytecode cache.

That warning is only worth its space while the premise holds: CPython — and
pytest's assertion-rewriting cache with it — decides a cached module is fresh by
comparing the source's modification time in WHOLE SECONDS and its size in BYTES.
Change neither and the stale bytecode runs, so the file on disk is not the code
under test.

This pins the PREMISE rather than the prose. If a future interpreter defaults to
hash-based invalidation (PEP 552 already provides it, opt-in), this test fails —
and the right response is to DELETE the CONTRIBUTING section, not to repair the
test. A warning about a hazard that no longer exists is worse than no warning,
because it spends a reader's attention on nothing and teaches them the document
is out of date.

Measured before writing this, twice and independently: a test asserting
``"AAA" == "BBB"`` reported ``1 passed`` after a same-second edit, and reported
``1 failed`` on the identical source once ``__pycache__`` was removed.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _import(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_a_same_size_same_mtime_edit_is_still_invisible(tmp_path: Path) -> None:
    """The hazard, demonstrated rather than described.

    ``os.utime`` forces the timestamp collision that a fast edit produces
    naturally, so the test asserts the CACHE RULE instead of racing the clock —
    the same reason the config-atomicity guards assert a sequence rather than
    run a race.
    """
    sys.path.insert(0, str(tmp_path))
    try:
        module = tmp_path / "cache_premise_probe.py"
        module.write_text('VALUE = "AAA"\n', encoding="utf-8")
        assert _import("cache_premise_probe", module).VALUE == "AAA"

        stamp = module.stat()
        module.write_text('VALUE = "BBB"\n', encoding="utf-8")
        assert module.stat().st_size == stamp.st_size, "the probe edit changed size"
        os.utime(module, (stamp.st_atime, stamp.st_mtime))

        del sys.modules["cache_premise_probe"]
        stale = _import("cache_premise_probe", module).VALUE

        assert stale == "AAA", (
            "the bytecode cache noticed a same-size, same-mtime edit — the hazard "
            "documented in CONTRIBUTING ('Proving a test can fail') no longer "
            "exists on this interpreter. DELETE that section rather than fixing "
            f"this test; the import returned {stale!r}."
        )

        # ...and the documented mitigation must actually mitigate it.
        for cache in tmp_path.rglob("__pycache__"):
            for compiled in cache.iterdir():
                compiled.unlink()
        del sys.modules["cache_premise_probe"]
        assert _import("cache_premise_probe", module).VALUE == "BBB", (
            "clearing __pycache__ did not pick the edit up, so the mitigation "
            "CONTRIBUTING prescribes is wrong"
        )
    finally:
        sys.modules.pop("cache_premise_probe", None)
        sys.path.remove(str(tmp_path))


def test_contributing_still_carries_the_warning() -> None:
    """The test and the section are only useful together.

    If the section is deleted while the premise still holds, the hazard is
    undocumented and this file is guarding nothing anyone will read.
    """
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "Proving a test can fail" in text
    assert "__pycache__" in text, "the mitigation is no longer in the document"
    assert "which" in text.lower(), "the detect-it habit (assert WHICH test fails) is gone"
