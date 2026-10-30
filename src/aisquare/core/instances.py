"""Every asq UI running on this home, one record each, so that each knows the others (#17).

The owner runs asq at the Mac and over SSH into the same Mac at once, on one home. Nothing
listed them, and an upgraded asq beside an old one ran two releases against one store and
one tmux. So each asq UI registers as it starts, before it touches anything else
(``cli.ui.instance_gate``), keeps its record fresh while it runs and removes it as it quits.

The registry is a folder of small files, not a store table: an asq of another release must
be able to read it before it opens the store, whose migrations are what two releases must
not race on. ``<home>/instances/`` is ``0700``, every file in it ``0600``, as the captain's
ui socket is (``cli.ui.receiver``), so only this user reads or writes them. Per instance:

- ``<id>.json`` — the record (:class:`Instance`), replaced whole at every heartbeat.
- ``<id>.lock`` — locked by the instance for as long as it runs. A pid is recycled; a lock
  is not: the OS drops it with the process, so a record whose lock is free is a crashed
  asq's, whoever has its pid since.
- ``<id>.quit`` — the quit-request channel (:data:`QUIT_CHANNEL`): another asq writes it to
  ask this one to quit, and this one's beat thread hands it to the app, which quits the way
  ``q`` does.

and ``.gate.lock``, held while an asq reads the others and writes its own record
(:func:`gate`), so two asqs of different releases started at the same moment cannot
both find nobody there.

A record is **live** (:func:`is_live`) while its lock is held, whatever host it names; else
when it names this host and its pid runs (and holds no lock only because it keeps none), or
names another host sharing the home and its heartbeat is under :data:`STALE_SECONDS` old.
Every other record is stale, ignored and deleted by the next reader (:func:`roll`); a record
whose lock is held never is. A record is rewritten every :data:`HEARTBEAT_SECONDS` by a
daemon thread of its own, never by a Textual timer: on Windows each pending timer holds a
thread of asyncio's executor (``HOUSEKEEPING_SECONDS``).

A record's id is its FILE NAME's, twelve hex digits (:func:`valid_id`), never the one inside
it: every path is built from a name the folder listing gave, so no record can name a file
outside the folder (review of #462). A fresh record this release cannot read, its content id
another than its name's included, is an asq of an unknown release (:class:`Unreadable`),
which the gate refuses beside: the likeliest such record is a later release's.

Across hosts on an NFS home, a client caches the folder's attributes, so another may not see
a fresh record for about 30 s.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NamedTuple
from uuid import uuid4

from aisquare.core import atomic, locking, paths, procs

Kind = Literal["native", "ssh"]
"""How the person at an asq reaches it: at the machine, or from another one (SSH, a tmux
whose client attached over SSH, a Codespace)."""

HEARTBEAT_SECONDS = 5.0
"""How often a live asq rewrites its record: well under :data:`STALE_SECONDS`, so an asq on
another host sharing the home reads as live through a missed beat or two."""

STALE_SECONDS = 60.0
"""How old the heartbeat of a record from another host may be before it is stale: that host
cannot be asked whether the pid runs, so the heartbeat is the only word on it."""

POLL_SECONDS = 0.5
"""How often the beat thread looks for a quit request: a newcomer waiting for this asq to
quit waits at most this long for it to start."""

QUIT_CHANNEL = 1
"""The quit-request channel this release reads (``<id>.quit``). A record without one is an
asq that cannot be asked to quit (:attr:`Instance.channel`)."""

GATE_WAIT_SECONDS = 2.0
"""How long :func:`gate` waits for another asq's check-and-register to end; past it, the gate
refuses rather than start this asq unseen (``cli.ui.instance_gate``)."""

KIND_SECONDS = 60.0
"""How often, inside tmux, the beat thread asks tmux how its client attached last: one
``tmux show-environment`` a minute, not one a heartbeat (#272 cut tmux's subprocesses)."""

_ID = re.compile(r"[0-9a-f]{12}")

FOLDER = "instances"
GATE_LOCK = ".gate.lock"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Instance:
    """One asq UI's record."""

    id: str
    pid: int
    host: str
    version: str
    """The release it runs (``aisquare-cli``'s version)."""
    schema: int
    """The store schema its release migrates to (``core.store.SCHEMA_VERSION``)."""
    kind: Kind
    tty: str | None
    """The terminal it runs in, ``/dev/ttys004``; ``None`` where none could be named."""
    started_at: datetime
    heartbeat: datetime
    channel: int = 0
    """The newest quit-request channel it reads, and every older one with it; ``0``: none, so
    it cannot be asked to quit (:attr:`askable`)."""

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pid": self.pid,
            "host": self.host,
            "version": self.version,
            "schema": self.schema,
            "kind": self.kind,
            "tty": self.tty,
            "started_at": self.started_at.isoformat(),
            "heartbeat": self.heartbeat.isoformat(),
            "channel": self.channel,
        }

    @classmethod
    def from_record(cls, data: object) -> Instance | None:
        """The record ``data`` holds, or ``None`` for one this release cannot read. Keys it
        does not know are ignored: a later release may say more."""
        if not isinstance(data, dict):
            return None
        try:
            kind = data["kind"]
            channel = data.get("channel", 0)
            tty = data.get("tty")
            instance = cls(
                id=str(data["id"]),
                pid=int(data["pid"]),
                host=str(data["host"]),
                version=str(data["version"]),
                schema=int(data["schema"]),
                kind=kind if kind in ("native", "ssh") else "native",
                tty=tty if isinstance(tty, str) else None,
                started_at=_zoned(datetime.fromisoformat(data["started_at"])),
                heartbeat=_zoned(datetime.fromisoformat(data["heartbeat"])),
                channel=channel if type(channel) is int else 0,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return instance

    @property
    def where(self) -> str:
        """Where it runs, as said to a person: ``ssh on mini, pid 4242, since 11:02``."""
        since = self.started_at.astimezone()
        today = datetime.now().astimezone().date()
        when = since.strftime("%H:%M") if since.date() == today else since.strftime("%b %d %H:%M")
        tty = f", {self.tty}" if self.tty else ""
        return f"{self.kind} on {self.host}, pid {self.pid}{tty}, since {when}"

    @property
    def label(self) -> str:
        """It, named in a line: ``asq 0.9.2 (ssh on mini, pid 4242)``."""
        return f"asq {self.version} ({self.kind} on {self.host}, pid {self.pid})"

    @property
    def askable(self) -> bool:
        """Whether it reads the quit requests this release writes (:func:`request_quit`)."""
        return self.channel >= QUIT_CHANNEL

    def differs_from(self, other: Instance) -> bool:
        """Whether the two run different releases or different store schemas."""
        return self.version != other.version or self.schema != other.schema


@dataclass(frozen=True)
class QuitRequest:
    """Another asq's ask that this one quit: who asked, as it said itself."""

    version: str
    kind: Kind
    host: str
    pid: int

    @property
    def who(self) -> str:
        return f"asq {self.version} ({self.kind} on {self.host}, pid {self.pid})"


def _zoned(moment: datetime) -> datetime:
    """``moment`` with a zone: a record's times are UTC, and one written without says so."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --- where -----------------------------------------------------------------------------------


def registry_dir() -> Path:
    """``<home>/instances``; made by the first asq to register, never by a reader."""
    return paths.aisquare_home() / FOLDER


def valid_id(text: str) -> bool:
    """Whether ``text`` is an instance id: twelve lower-case hex digits, so a path built from
    it stays a file in the folder."""
    return _ID.fullmatch(text) is not None


def _named(instance_id: str, suffix: str) -> Path:
    if not valid_id(instance_id):
        raise ValueError(f"not an asq instance id: {instance_id!r}")
    return registry_dir() / f"{instance_id}{suffix}"


def _record(instance_id: str) -> Path:
    return _named(instance_id, ".json")


def _lock(instance_id: str) -> Path:
    return _named(instance_id, ".lock")


def _quit(instance_id: str) -> Path:
    return _named(instance_id, ".quit")


def _private_dir() -> Path:
    """The registry folder, made ``0700`` (and narrowed to it, if an older one was wider)."""
    folder = registry_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(folder, 0o700)
    return folder


def _write_private(path: Path, body: str) -> None:
    """``body`` into ``path`` in one step, ``0600``. Not fsynced: a record lost to a crash
    is one that would be stale anyway."""
    atomic.write_replacing(path, body, durable=False, owner_only=sys.platform != "win32")


# --- what this process is ----------------------------------------------------------------------


def host_name() -> str:
    return socket.gethostname() or "localhost"


def env_kind(env: Mapping[str, str] | None = None) -> Kind:
    """This process's kind from its environment alone: no tmux, no ``ps``. What the gate
    records; the beat thread asks tmux (:func:`attached_kind`)."""
    from aisquare.core import clipboard

    return "ssh" if clipboard.is_remote(os.environ if env is None else env) else "native"


def attached_kind(env: Mapping[str, str] | None = None) -> Kind:
    """This process's kind, sure: inside tmux, how its client attached last (an asq started
    at the Mac and reattached over SSH is ``ssh``); outside, an SSH server above it too
    (``core.clipboard.session_is_remote``). It asks tmux or runs ``ps``: off the UI thread."""
    from aisquare.core import clipboard

    return "ssh" if clipboard.session_is_remote(os.environ if env is None else env) else "native"


def _tty() -> str | None:
    if sys.platform == "win32":  # no ttyname there; and mypy --platform win32 reads this
        return None
    for stream in (sys.stdin, sys.stdout):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            return os.ttyname(stream.fileno())
    return None


def describe_this_process(*, kind: Kind | None = None) -> Instance:
    """This process as a record would say it, registered or not."""
    from aisquare.core.store import SCHEMA_VERSION
    from aisquare.core.version import __version__

    now = datetime.now(UTC)
    return Instance(
        id=uuid4().hex[:12],
        pid=os.getpid(),
        host=host_name(),
        version=__version__,
        schema=SCHEMA_VERSION,
        kind=env_kind() if kind is None else kind,
        tty=_tty(),
        started_at=now,
        heartbeat=now,
        channel=QUIT_CHANNEL,
    )


# --- reading -----------------------------------------------------------------------------------


def _lock_held(instance_id: str) -> bool | None:
    """Whether a running process holds ``<id>.lock``; ``None`` when it cannot be told (no lock
    file: a release that keeps none)."""
    try:
        fd = locking.open_lock_file(_lock(instance_id), create=False)
    except OSError:
        return None
    try:
        locking.acquire(fd, wait_s=0)
    except TimeoutError:
        return True
    except OSError:
        return None
    else:
        with contextlib.suppress(OSError):
            locking.unlock(fd)
        return False
    finally:
        os.close(fd)


def is_live(instance: Instance, *, host: str | None = None, now: datetime | None = None) -> bool:
    """Its lock held: live, whatever host it names (two machines that share a host name, a
    clock on an NFS home a minute off). Else, this host: its pid runs and it keeps no lock;
    another host: a heartbeat under :data:`STALE_SECONDS` old (that host cannot be asked
    about its pids)."""
    held = _lock_held(instance.id)
    if held:
        return True
    if instance.host == (host_name() if host is None else host):
        return held is None and procs.alive(instance.pid)
    age = (datetime.now(UTC) if now is None else now) - instance.heartbeat
    return age < timedelta(seconds=STALE_SECONDS)


def _forget(instance_id: str) -> None:
    for path in (_record(instance_id), _quit(instance_id), _lock(instance_id)):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


Unloaded = Literal["gone", "failed", "unparsable"]
"""Why a record was not read (:func:`_load`)."""

_RETRY_SECONDS = 0.05
"""How long :func:`roll` waits before reading again a record whose read failed."""


def _load(path: Path) -> Instance | Unloaded:
    """The record at ``path``, or why not: ``gone``, its file no longer there (an asq that
    quit between the folder's listing and this read); ``failed``, an ``OSError`` reading it
    (a sharing violation during another asq's beat on Windows, an NFS ``ESTALE``);
    ``unparsable``, its content, which a release this one cannot read wrote."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "gone"
    except UnicodeDecodeError:
        return "unparsable"
    except OSError:
        return "failed"
    try:
        instance = Instance.from_record(json.loads(text))
    except (ValueError, RecursionError):
        return "unparsable"
    return "unparsable" if instance is None else instance


def _read(path: Path) -> Instance | None:
    loaded = _load(path)
    return None if isinstance(loaded, str) else loaded


@dataclass(frozen=True)
class Unreadable:
    """A fresh record this release cannot read: an asq of an unknown release, most likely a
    later one, whose record says what this release does not know how to read."""

    id: str
    written_at: datetime

    @property
    def path(self) -> Path:
        return _record(self.id)


class Roll(NamedTuple):
    """The registry as one read found it."""

    live: list[Instance]
    """Every live asq, this one included, oldest first."""
    unreadable: list[Unreadable]
    """Fresh records this release cannot read."""


def roll(*, prune: bool = True) -> Roll:
    """The live asqs on this home, and the fresh records this release cannot read.

    A record's id is its file's name (:func:`valid_id`): one whose content names another id
    is unreadable. With ``prune``, a stale record is deleted with its lock and any quit
    request, as is an unreadable one once its file is as old as a stale heartbeat, and so is
    anything else the folder holds that no record explains (a lock or a quit request whose
    record is gone, a temp, a copy) once as old; a lock still held never is. Without
    ``prune`` it writes nothing. Reads only: no folder is made.
    """
    folder = registry_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return Roll([], [])
    here = host_name()
    now = datetime.now(UTC)
    live: list[Instance] = []
    unreadable: list[Unreadable] = []
    for name in names:
        path = folder / name
        stem = name.removesuffix(".json")
        if stem == name or not valid_id(stem):
            if prune and name != GATE_LOCK and _old(path) and not _explained(name, names):
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        loaded = _load(path)
        if loaded == "failed":  # once more, a moment later: the failures it means are brief
            time.sleep(_RETRY_SECONDS)
            loaded = _load(path)
        if loaded in ("gone", "failed"):
            # Quit since the listing, or unreadable twice running for a reason not its
            # content: neither a live asq nor one of an unknown release (re-review of #462).
            continue
        if isinstance(loaded, str) or loaded.id != stem:
            written = _written(path)
            if written is None:
                continue  # gone since
            held = _lock_held(stem) is True
            if held or now - written < timedelta(seconds=STALE_SECONDS):
                # A held lock is a running process at any age, as for a record it can read.
                unreadable.append(Unreadable(stem, written))
            elif prune:
                _forget(stem)
            continue
        instance = loaded
        if is_live(instance, host=here, now=now):
            live.append(instance)
        elif prune:
            _forget(stem)  # not live: its lock is not held
    return Roll(sorted(live, key=lambda instance: instance.started_at), unreadable)


def _explained(name: str, names: list[str]) -> bool:
    """Whether ``name`` (not a record) belongs to a record still there, or is a lock still held."""
    owner = name.split(".", 1)[0]
    if not valid_id(owner):
        return False
    return f"{owner}.json" in names or (name.endswith(".lock") and _lock_held(owner) is True)


def _written(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def live_instances(*, prune: bool = True) -> list[Instance]:
    """Every live asq on this home, this one included, oldest first (:func:`roll`)."""
    return roll(prune=prune).live


def _old(path: Path) -> bool:
    """Whether ``path`` was last written as long ago as a stale heartbeat."""
    try:
        return time.time() - path.stat().st_mtime >= STALE_SECONDS
    except OSError:
        return False


def other_instances(*, prune: bool = True) -> list[Instance]:
    """:func:`live_instances` but this process's own; ``prune=False`` writes nothing."""
    mine = this_instance()
    return [i for i in live_instances(prune=prune) if mine is None or i.id != mine.id]


# --- asking another to quit --------------------------------------------------------------------


def request_quit(instance: Instance, *, by: Instance) -> None:
    """Ask ``instance`` to quit, as ``by``: its beat thread reads the request and its app
    quits as ``q`` would. Raises ``OSError`` when the request cannot be written."""
    body = {"version": by.version, "kind": by.kind, "host": by.host, "pid": by.pid}
    _write_private(_quit(instance.id), json.dumps(body))


def wait_gone(
    instance: Instance, timeout: float, *, clock: Callable[[], float] = time.monotonic
) -> bool:
    """Wait at most ``timeout`` seconds for ``instance`` to quit (its record gone, or no
    longer live); whether it did."""
    deadline = clock() + timeout
    while True:
        instance_now = _read(_record(instance.id))
        if instance_now is None or not is_live(instance_now):
            return True
        if clock() >= deadline:
            return False
        time.sleep(0.1)


def _read_request(path: Path) -> QuitRequest | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        kind = data.get("kind")
        return QuitRequest(
            version=str(data["version"]),
            kind=kind if kind in ("native", "ssh") else "native",
            host=str(data["host"]),
            pid=int(data["pid"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return None


# --- this process's own record ---------------------------------------------------------------


Deliver = Callable[[QuitRequest], object]
"""Hands a quit request to the app, from the beat thread."""


class Registration:
    """This process's record: written at :func:`register`, kept fresh by :meth:`start`'s
    thread, removed by :meth:`deregister`."""

    def __init__(self, instance: Instance, lock_fd: int) -> None:
        self._instance = instance
        self._lock_fd: int | None = lock_fd
        self._guard = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._deliver: Deliver | None = None
        self._others: list[Instance] = []

    @property
    def instance(self) -> Instance:
        return self._instance

    @property
    def others(self) -> list[Instance]:
        """The other live asqs as the beat thread last read them, at most
        :data:`HEARTBEAT_SECONDS` ago: for the UI thread, which reads no folder (a home on a
        hung NFS mount would hold it). Empty until the thread's first beat."""
        return self._others

    def start(self) -> None:
        """Start the beat thread: the heartbeat every :data:`HEARTBEAT_SECONDS` (the first at
        once, saying how tmux's client attached) and the look for a quit request every
        :data:`POLL_SECONDS`. Started once the gate is passed, never in it: it asks tmux."""
        if self._thread is not None or self._closed:
            return
        self._thread = threading.Thread(target=self._run, name="asq-instance", daemon=True)
        self._thread.start()

    def deliver_quits_to(self, deliver: Deliver | None) -> None:
        """Where a quit request goes: the app's handler while it runs, ``None`` before and
        after (a request waits for one)."""
        self._deliver = deliver

    def beat(self, *, kind: Kind | None = None) -> None:
        """Rewrite the record with a heartbeat of now (and ``kind``, when given)."""
        with self._guard:
            if self._closed:
                return
            changes: dict[str, Any] = {"heartbeat": datetime.now(UTC)}
            if kind is not None:
                changes["kind"] = kind
            self._instance = replace(self._instance, **changes)
            _write_private(_record(self._instance.id), json.dumps(self._instance.to_record()))

    def deregister(self) -> None:
        """Stop the beat thread and remove the record, its lock and any quit request. Once."""
        global _this
        with self._guard:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
        # The record first, so no reader finds it once the lock is let go; the lock file
        # after its descriptor is closed, which Windows asks before a delete.
        with contextlib.suppress(OSError):
            _record(self._instance.id).unlink(missing_ok=True)
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        _forget(self._instance.id)
        with _this_guard:
            if _this is self:
                _this = None

    def _run(self) -> None:
        next_beat = 0.0
        next_kind: float | None = 0.0
        while True:
            if time.monotonic() >= next_beat:
                try:
                    kind: Kind | None = None
                    # Outside tmux the answer cannot change: asked once. Inside, a reattach
                    # changes it: asked again every KIND_SECONDS, not at every beat.
                    if next_kind is not None and time.monotonic() >= next_kind:
                        kind = attached_kind()
                        inside = bool(os.environ.get("TMUX"))
                        next_kind = time.monotonic() + KIND_SECONDS if inside else None
                    self.beat(kind=kind)
                except Exception:  # a beat missed is one more next time, never a dead thread
                    _log.debug("the asq instance record could not be written", exc_info=True)
                try:
                    mine = self._instance.id
                    self._others = [i for i in live_instances() if i.id != mine]
                except Exception:
                    _log.debug("the other asqs could not be read", exc_info=True)
                next_beat = time.monotonic() + HEARTBEAT_SECONDS
            try:
                self._look_for_quit()
            except Exception:
                _log.debug("a quit request could not be read", exc_info=True)
            if self._stop.wait(POLL_SECONDS):
                return

    def _look_for_quit(self) -> None:
        deliver = self._deliver
        path = _quit(self._instance.id)
        if deliver is None or not path.exists():
            return
        request = _read_request(path)
        path.unlink(missing_ok=True)
        if request is not None and not self._closed:
            deliver(request)


_this: Registration | None = None
_this_guard = threading.Lock()


def this_registration() -> Registration | None:
    """This process's registration, while it has one."""
    return _this


def this_instance() -> Instance | None:
    """This process's record, while it is registered."""
    registration = _this
    return None if registration is None else registration.instance


@contextlib.contextmanager
def gate() -> Iterator[Roll]:
    """Hold ``.gate.lock`` for the block, and yield the registry as read under it (:func:`roll`).

    An asq reads the others and writes its own record (:func:`register`) inside one such
    block, so another doing the same at the same moment reads it or is read by it. Makes the
    folder; raises :class:`TimeoutError` when another holds the lock past
    :data:`GATE_WAIT_SECONDS`, and ``OSError`` when the folder or the lock cannot be had.
    """
    folder = _private_dir()
    with locking.holding(folder / GATE_LOCK, wait_s=GATE_WAIT_SECONDS, mode=0o600):
        yield roll()


def register(me: Instance) -> Registration:
    """Write ``me``'s record and hold its lock: this process's registration from now on.
    Inside :func:`gate`'s block; ``me`` is :func:`describe_this_process`'s.

    Started and beating NOW: ``me`` was described before the gate, maybe before a question
    a person took minutes to answer, and an older heartbeat read as stale on another host
    until the first beat (review of #462)."""
    global _this
    now = datetime.now(UTC)
    me = replace(me, started_at=now, heartbeat=now)
    _private_dir()
    fd = locking.open_lock_file(_lock(me.id), mode=0o600)
    try:
        locking.lock_exclusive(fd)
        _write_private(_record(me.id), json.dumps(me.to_record()))
    except BaseException:
        os.close(fd)
        _forget(me.id)
        raise
    registration = Registration(me, fd)
    with _this_guard:
        _this = registration
    return registration
