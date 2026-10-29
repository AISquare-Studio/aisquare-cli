"""Fleet codenames: ``adjective-animal``, deterministic from the project id.

A codename is the stable, slug-safe token the fleet needs where a directory
name would collide or would not be a legal tmux target (``.`` and ``:`` are
target separators): the tmux session (``asq-amber-otter``), branch names
(``fleet/amber-otter/…``), ``fleet`` command arguments. It is DERIVED from the
project id — itself a hash of the resolved root — so the same checkout gets the
same name on every machine and after a ``context.db`` loss; the user cannot
predict it, which keeps the fun, and can rename it (``fleet rename``).

A project RENAMED takes a slug of its new name as its codename
(:func:`codename_from_text`: ``AISquare CLI`` → ``aisquare-cli``), so the name a manager
quotes is one the owner can find on its card. A chosen codename has that wider shape
(:data:`USER_CODENAME`); a generated one is still ``adjective-animal`` (:data:`CODENAME`).

Both word lists are hand-curated: lowercase ASCII, three to seven letters,
family-friendly, nothing that reads badly beside an animal. ``tests`` pin the
charset, ordering and uniqueness of the lists. See docs/plans/fleet-tui.md §5.7.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Collection

from aisquare.core.ids import PROJECT_PREFIX

CODENAME = re.compile(r"^[a-z]{3,7}-[a-z]{3,7}$")
"""The shape of a GENERATED codename (:func:`codename_for`): two curated words."""

CODENAME_MAX = 24
"""The longest codename, in characters: ``asq-<codename>`` and ``fleet/<codename>/…`` stay
short enough to read in a tmux status line and a branch list."""

USER_CODENAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
"""The shape a codename a person chose may have (``fleet rename``, or a project renamed):
lowercase ASCII words of letters and digits joined by single ``-``, starting with a letter.
Never a ``.``, ``:`` (tmux target separators), ``..``, space or ``/`` (a git ref component),
and at most :data:`CODENAME_MAX` characters (:func:`is_codename`)."""

WORD = re.compile(r"^[a-z]{3,7}$")

# fmt: off
ADJECTIVES: tuple[str, ...] = (
    "agile", "amber", "ardent", "azure", "balmy", "bold", "brave", "breezy", "bright", "brisk",
    "calm", "candid", "cedar", "cheery", "civic", "clear", "clever", "coral", "cosmic", "crisp",
    "dapper", "deft", "dusky", "eager", "early", "earnest", "ember", "fair", "frosty", "gentle",
    "gilded", "golden", "grand", "happy", "hardy", "hazel", "hearty", "humble", "indigo",
    "ivory", "jade", "jaunty", "jolly", "keen", "kind", "lively", "loyal", "lucid", "lucky",
    "lunar", "maple", "mellow", "merry", "mighty", "misty", "modest", "nimble", "noble",
    "olive", "opal", "pearl", "plucky", "polar", "proud", "quick", "quiet", "rapid", "ready",
    "regal", "rosy", "ruby", "rustic", "sable", "sandy", "scarlet", "sharp", "silver", "sleek",
    "snowy", "solar", "spry", "steady", "stellar", "sturdy", "sunny", "sweet", "swift", "tidy",
    "topaz", "trusty", "velvet", "vivid", "warm", "wild", "witty", "zesty",
)
"""Ninety-six adjectives: colours, temperaments and textures that read well beside
an animal. Sorted, unique, three to seven lowercase letters — ``tests/test_codenames.py``
pins all three so a careless edit cannot slip in a proper noun or a duplicate."""

ANIMALS: tuple[str, ...] = (
    "alpaca", "badger", "beaver", "bison", "bobcat", "camel", "cheetah", "civet", "condor",
    "coyote", "crane", "dingo", "dolphin", "donkey", "dove", "eagle", "egret", "elk", "emu",
    "falcon", "ferret", "finch", "fox", "gannet", "gazelle", "gecko", "gibbon", "giraffe",
    "goose", "grouse", "hare", "hawk", "heron", "hoopoe", "ibex", "ibis", "iguana", "impala",
    "jackal", "jaguar", "kestrel", "kiwi", "koala", "lark", "lemur", "leopard", "lion",
    "lizard", "llama", "lynx", "macaw", "magpie", "manatee", "marmot", "marten", "meerkat",
    "mole", "moose", "narwhal", "newt", "ocelot", "octopus", "oriole", "osprey", "otter", "owl",
    "panda", "parrot", "pelican", "penguin", "pika", "plover", "puffin", "puma", "quail",
    "quokka", "rabbit", "raven", "robin", "salmon", "seal", "serval", "sparrow", "stoat",
    "swan", "tapir", "tiger", "toucan", "turtle", "vole", "walrus", "weasel", "wombat", "wren",
    "yak", "zebra",
)
"""Ninety-six animals under the same rules. 96 by 96 gives 9,216 codenames."""
# fmt: on


def is_codename(text: str) -> bool:
    """Whether ``text`` may be a codename: :data:`USER_CODENAME`'s shape, at most
    :data:`CODENAME_MAX` characters, and not a prefix of every project id (``p``, ``pr``,
    ``prj``: ``--project prj`` names every project by its id, ``prj_…``, so a codename
    spelled so could never name its own). Every generated codename is one."""
    return (
        len(text) <= CODENAME_MAX
        and USER_CODENAME.match(text) is not None
        and not PROJECT_PREFIX.startswith(text)
    )


def codename_from_text(text: str) -> str | None:
    """The codename a project named ``text`` takes, or ``None`` when ``text`` gives none.

    Lowercased, accents folded (NFKD: ``é`` is ``e``, and what has no ASCII letter, an
    emoji or a CJK character, goes), each run of anything outside ``[a-z0-9]`` one ``-``,
    then trimmed to start with a letter and to :data:`CODENAME_MAX` characters with no
    ``-`` at either end: ``AISquare CLI`` → ``aisquare-cli``, ``Café ✨ Board`` →
    ``cafe-board``. ``!!!``, ``42`` and ``日本語`` give nothing (``None``), and neither do
    ``p``, ``pr`` or ``prj`` (:func:`is_codename`). Pure: whether it is free is
    :func:`first_free`'s to say.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower())
    slug = re.sub(r"^[^a-z]+", "", slug)  # starts with a letter
    slug = slug[:CODENAME_MAX].rstrip("-")
    return slug if slug and is_codename(slug) else None


def first_free(base: str, taken: Collection[str]) -> str:
    """``base`` when nobody holds it, else the first of ``base-2``, ``base-3``, … that is
    free — ``base`` cut short enough (no ``-`` left at its end) that the suffix keeps the
    whole within :data:`CODENAME_MAX`. ``base`` is a codename (:func:`is_codename`)."""
    unavailable = set(taken)
    if base not in unavailable:
        return base
    number = 2
    while True:
        suffix = f"-{number}"
        candidate = base[: CODENAME_MAX - len(suffix)].rstrip("-") + suffix
        if candidate not in unavailable:
            return candidate
        number += 1


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
