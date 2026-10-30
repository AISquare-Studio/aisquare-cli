"""Another asq UI live on the test's home, as the registry sees one (``core.instances``).

Its record is written and its lock held from this process, under an id of its own, so
every reader takes it for another asq: same host, a pid that runs, a lock that is held.
What it runs, where and since when are the test's to say.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from aisquare.core import instances, locking
from aisquare.core.instances import Instance, Kind


@contextlib.contextmanager
def asq_running(
    *,
    version: str | None = None,
    schema: int | None = None,
    channel: int = instances.QUIT_CHANNEL,
    host: str | None = None,
    heartbeat: datetime | None = None,
    pid: int | None = None,
    kind: Kind = "ssh",
    hold_lock: bool = True,
) -> Iterator[Instance]:
    """A live asq's record (and its held lock) for the block; removed afterwards."""
    me = instances.describe_this_process()
    record = replace(
        me,
        id=uuid4().hex[:12],
        version=me.version if version is None else version,
        schema=me.schema if schema is None else schema,
        channel=channel,
        host=me.host if host is None else host,
        heartbeat=datetime.now(UTC) if heartbeat is None else heartbeat,
        pid=os.getpid() if pid is None else pid,
        kind=kind,
        tty="/dev/ttys009",
    )
    folder = instances.registry_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = locking.open_lock_file(folder / f"{record.id}.lock", mode=0o600)
    try:
        if hold_lock:
            locking.lock_exclusive(fd)
        (folder / f"{record.id}.json").write_text(json.dumps(record.to_record()), encoding="utf-8")
        yield record
    finally:
        os.close(fd)
        for suffix in (".json", ".lock", ".quit"):
            with contextlib.suppress(OSError):
                (folder / f"{record.id}{suffix}").unlink()
