"""Fixture repos for measuring the session-start snapshot directive (#282).

The P1 handoff (``docs/plans/handoff-p1-token-diet.md`` §1.7, §5) measures Read calls on
the skeleton and context tokens of a fresh session on two repos, so every workstream
packs the same trees:

- ``between``: a skeleton over 25,000 tokens (one default Read) and a full pack within the
  150,000-token budget, so the snapshot is ``ready`` with a skeleton beside it;
- ``over``: a compressed pack over the budget, so the snapshot is ``skeleton_only``.

The trees are generated, never committed: the same arguments write the same tree. Packed
with repomix 1.18.1 on 2026-10-23, ``between`` measured 74,519 tokens full and 42,096
skeleton (``ready``), ``over`` 814,079 full and 450,516 compressed (``skeleton_only``).
Those sizes are what the shapes are for, not a promise this code makes: re-measure with
``aisquare project onboard`` after changing the numbers here.

    python tests/snapshot_repos.py <dir> between|over
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Modules and functions per module for each shape.
SHAPES: dict[str, tuple[int, int]] = {"between": (60, 12), "over": (400, 20)}


def _module(index: int, functions: int) -> str:
    parts = [f'"""Ledger rules, part {index}."""\n\nfrom __future__ import annotations\n']
    for n in range(functions):
        parts.append(
            f"\n\ndef settle_{index:03d}_{n:02d}(amount: int, rate: float, *, rounding: str = "
            f'"half-even") -> int:\n'
            f'    """Settle part {index}, rule {n}: apply ``rate`` to ``amount`` and round."""\n'
            f"    total = amount\n"
            f"    for step in range({n % 5 + 1}):\n"
            f"        total = int(total * rate) + step\n"
            f'    if rounding == "half-even" and total % 2:\n'
            f"        total += 1\n"
            f"    return total - {index}\n"
        )
    return "".join(parts)


def write(root: Path, shape: str) -> Path:
    """Write the ``shape`` fixture repo at ``root`` (a new directory) as one commit."""
    modules, functions = SHAPES[shape]
    package = root / "ledger"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""A generated ledger."""\n', encoding="utf-8")
    for index in range(modules):
        (package / f"part_{index:03d}.py").write_text(_module(index, functions), encoding="utf-8")
    (root / "README.md").write_text(f"# ledger ({shape})\n", encoding="utf-8")
    git = ["git", "-C", str(root), "-c", "user.name=fixture", "-c", "user.email=fixture@example"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", f"fixture: {shape}"], check=True)
    return root


if __name__ == "__main__":
    write(Path(sys.argv[1]), sys.argv[2])
