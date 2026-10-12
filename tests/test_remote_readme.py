"""The README's phone control quickstart: what each way of starting Remote has you run."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def _section() -> str:
    text = README.read_text(encoding="utf-8")
    found = re.search(r"\n### Phone control \(`aisquare remote`\)\n(.*?)\n### ", text, re.S)
    assert found is not None, "the README's phone control section"
    return found.group(1)


def _blocks(text: str) -> list[str]:
    return re.findall(r"```sh\n(.*?)```", text, re.S)


def test_the_quickstart_starts_ngrok_by_hand_only_beside_serve() -> None:
    """The quickstart's one block ran ``aisquare remote serve  # or press R in aisquare ui``,
    then ngrok "in another terminal": a reader who pressed R started a second ngrok on the
    static domain the panel's own ngrok holds, and the second of the two failed
    (ERR_NGROK_334); with the panel's, the panel said phones could not reach Remote and
    showed no QR code. The R panel starts its own ngrok, and the README says so; the one
    block that starts ngrok by hand is ``serve``'s, and names no R."""
    section = _section()
    blocks = _blocks(section)
    by_hand = [block for block in blocks if re.search(r"^ngrok http\b", block, re.M)]
    assert len(blocks) >= 2 and len(by_hand) == 1, blocks
    assert re.search(r"^aisquare remote serve\b", by_hand[0], re.M)
    assert "press R" not in by_hand[0] and "aisquare ui" not in by_hand[0]
    prose = " ".join(section.split())
    assert "the panel starts the server and ngrok on that domain itself" in prose
