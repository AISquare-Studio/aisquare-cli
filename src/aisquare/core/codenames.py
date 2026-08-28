"""Fleet codenames: ``adjective-animal``, deterministic from the project id.

A codename is the stable, slug-safe token the fleet needs where a directory
name would collide or would not be a legal tmux target (``.`` and ``:`` are
target separators): the tmux session (``asq-amber-otter``), branch names
(``fleet/amber-otter/…``), ``fleet`` command arguments. It is DERIVED from the
project id — itself a hash of the resolved root — so the same checkout gets the
same name on every machine and after a ``context.db`` loss; the user cannot
predict it, which keeps the fun, and can rename it (``fleet rename``).

Both word lists are hand-curated: lowercase ASCII, three to seven letters,
family-friendly, nothing that reads badly beside an animal. ``tests`` pin the
charset, ordering and uniqueness of the lists. See docs/plans/fleet-tui.md §5.7.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection

CODENAME = re.compile(r"^[a-z]{3,7}-[a-z]{3,7}$")
"""The shape every codename — generated or user-chosen — must have."""

WORD = re.compile(r"^[a-z]{3,7}$")

# fmt: off
ADJECTIVES: tuple[str, ...] = (
    "amber", "azure", "bold", "brave", "bright", "brisk", "calm", "cedar",
    "civic", "clear", "clever", "coral", "cosmic", "crisp", "dapper", "deft",
    "dusky", "eager", "early", "ember", "fair", "frosty", "gentle", "gilded",
    "golden", "grand", "happy", "hardy", "hazel", "humble", "indigo", "ivory",
    "jade", "jolly", "keen", "kind", "lively", "lucky", "lunar", "maple",
    "mellow", "merry", "misty", "nimble", "noble", "olive", "opal", "pearl",
    "plucky", "polar", "proud", "quick", "quiet", "rapid", "ready", "rosy",
    "ruby", "rustic", "sable", "sandy", "scarlet", "sharp", "silver", "sleek",
    "snowy", "solar", "spry", "steady", "stellar", "sturdy", "sunny", "swift",
    "tidy", "topaz", "trusty", "velvet", "vivid", "warm", "wild", "witty",
    "zesty",
)

ANIMALS: tuple[str, ...] = (
    "badger", "beaver", "bison", "bobcat", "condor", "crane", "dolphin", "eagle",
    "egret", "falcon", "ferret", "finch", "fox", "gecko", "gibbon", "hare",
    "hawk", "heron", "ibis", "iguana", "jackal", "jaguar", "kestrel", "kiwi",
    "koala", "lemur", "leopard", "lion", "llama", "lynx", "macaw", "magpie",
    "marmot", "marten", "meerkat", "moose", "narwhal", "newt", "ocelot", "octopus",
    "oriole", "osprey", "otter", "owl", "panda", "parrot", "pelican", "penguin",
    "pika", "puffin", "quokka", "rabbit", "raven", "robin", "salmon", "seal",
    "sparrow", "stoat", "swan", "tapir", "tiger", "toucan", "turtle", "walrus",
    "wombat", "wren", "yak", "zebra",
)
# fmt: on


def is_codename(text: str) -> bool:
    """Whether ``text`` has the codename shape (any words, not just ours)."""
    return CODENAME.match(text) is not None


def _pair(index: int) -> str:
    adjective = ADJECTIVES[index // len(ANIMALS)]
    animal = ANIMALS[index % len(ANIMALS)]
    return f"{adjective}-{animal}"


def _start_index(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (len(ADJECTIVES) * len(ANIMALS))


def codename_for(seed: str, *, taken: Collection[str] = ()) -> str:
    """The codename for ``seed`` (a project id), skipping names already ``taken``.

    Deterministic: the same seed always lands on the same first candidate, and
    walks forward one pair at a time past anything in ``taken`` — so two
    projects can never share a name on one machine, and one project keeps its
    name across machines unless something there already had it.
    """
    total = len(ADJECTIVES) * len(ANIMALS)
    start = _start_index(seed)
    unavailable = set(taken)
    for offset in range(total):
        candidate = _pair((start + offset) % total)
        if candidate not in unavailable:
            return candidate
    raise RuntimeError("every codename is taken")  # ~9,000 projects on one machine
