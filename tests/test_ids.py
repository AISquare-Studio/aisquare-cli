"""Entry id format, uniqueness, and time-ordering."""

from __future__ import annotations

import re
import time

from aisquare.core.ids import ENTRY_PREFIX, new_entry_id

_ID_PATTERN = re.compile(r"^ctx_[0-9abcdefghjkmnpqrstvwxyz]{26}$")


def test_id_has_expected_shape() -> None:
    entry_id = new_entry_id()
    assert entry_id.startswith(ENTRY_PREFIX)
    assert _ID_PATTERN.match(entry_id), entry_id


def test_ids_are_unique() -> None:
    ids = {new_entry_id() for _ in range(5_000)}
    assert len(ids) == 5_000


def test_ids_sort_by_creation_time() -> None:
    # The 48-bit millisecond prefix makes ids lexicographically time-ordered, so
    # a plain ``ORDER BY id`` lists entries oldest-first. A small gap guarantees
    # the two ids land in different milliseconds.
    earlier = new_entry_id()
    time.sleep(0.005)
    later = new_entry_id()
    assert earlier < later
