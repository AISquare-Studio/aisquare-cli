"""Run on Windows as well as POSIX: no receiver or native CLI is required."""

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import outbox


def test_missing_platform_errno_does_not_mask_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(errno, "EDQUOT", raising=False)
    write = Path.write_text

    def full(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.parent == outbox.queue_dir():
            raise OSError(errno.ENOSPC, "fixture")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", full)
    assert outbox.enqueue({"kind": "fixture"}) is None  # primary path remains fail-open
    with pytest.raises(OSError) as failure:
        outbox.enqueue_retryable({"kind": "fixture"})
    assert failure.value.errno == errno.ENOSPC
    assert outbox.temporary_write_error(failure.value)
    assert not outbox.temporary_write_error(OSError("unknown cause"))


def test_receipts_survive_delivery_and_reopen() -> None:
    with outbox.retry_batch() as batch:
        assert batch.enqueue({"kind": "fixture"}, "same-event") is True
    pending = outbox.pending()
    assert len(pending) == 1
    claimed = outbox.claim(pending[0])
    assert claimed is not None
    outbox.mark_sent(claimed)
    with outbox.retry_batch() as retry:
        assert retry.enqueue({"kind": "fixture"}, "same-event") is False
    assert outbox.pending() == [] and outbox.counts().sent == 1


def test_native_events_keep_their_place_in_the_shared_queue() -> None:
    with outbox.retry_batch() as batch:
        assert batch.enqueue({"kind": "native_event"}, "first") is True
    native = outbox.pending()[0]
    later = outbox.enqueue({"kind": "generic"})
    assert later is not None
    os.utime(native, ns=(1_000_000_000, 1_000_000_000))
    os.utime(later, ns=(2_000_000_000, 2_000_000_000))
    assert outbox.pending(limit=1) == [native]
