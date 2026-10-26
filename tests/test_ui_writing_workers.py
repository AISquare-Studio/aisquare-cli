"""Every worker the UI starts is one Update and Uninstall wait for, unless it is safe to cut off.

Update and Uninstall refuse under the writers ``ui_app._writing_workers()`` names and wait
out every other thread worker but ``ui_app._cut_off_workers()``'s. Until round 22 of #257
they waited only for the reads a second list named, and rounds 15 to 22 each found a worker
filed as one that writes nothing that opened the store or wrote a cache. Now a worker is
waited for unless it is listed as safe to cut off, so this guard checks what that default
rests on: every ``run_worker(…)`` and ``@work(…)`` in ``aisquare.cli.ui``, read from the
source, is a thread worker (a task worker is left to the quit) and has a name. It follows a
name passed down through a helper's parameter (Welcome's ``_start_work`` → ``_run`` →
``worker_name``), so the app's lists and the hand-over's tests name only workers the UI
starts, and those tests take every worker the walk finds.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import aisquare.cli.ui as ui_package
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui import board
from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.cli.ui.views import agent as agent_view
from aisquare.cli.ui.views import app_settings as app_settings_view
from aisquare.cli.ui.views import welcome as welcome_view
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import diagnostics, first_run
from tests.test_claude_accounts import NOW
from tests.test_usage_aware_accounts import _payload, _slot, _Usage


def _module_name(path: Path) -> str:
    package = Path(ui_package.__file__).parent
    parts = path.relative_to(package).with_suffix("").parts
    return ".".join((ui_package.__name__, *parts))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> ast.FunctionDef | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node  # type: ignore[return-value]
    return None


def _calls_to(tree: ast.AST, function: str) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == function:
            yield node


def _names(
    value: ast.expr,
    where: ast.FunctionDef | None,
    tree: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
    namespace: Mapping[str, Any],
) -> set[str]:
    """Every string ``value`` can be at a worker start, read from the source alone."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        found = getattr(namespace.get(value.value.id), value.attr, None)
        if isinstance(found, str):
            return {found}
    if isinstance(value, ast.Name):
        params = [arg.arg for arg in where.args.args] if where is not None else []
        if where is not None and value.id in params:
            # A helper's parameter: what every caller in this module passes it.
            index = params.index(value.id) - (1 if params[:1] == ["self"] else 0)
            names: set[str] = set()
            for call in _calls_to(tree, where.name):
                passed = call.args[index] if index < len(call.args) else None
                passed = passed or next(
                    (kw.value for kw in call.keywords if kw.arg == value.id), None
                )
                if passed is None:
                    raise AssertionError(f"{where.name} is called without {value.id}")
                names |= _names(passed, _enclosing(call, parents), tree, parents, namespace)
            return names
        found = namespace.get(value.id)
        if isinstance(found, str):
            return {found}
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and len(value.args) == 1:
        helper = namespace.get(value.func.id)
        if callable(helper):
            inner = _names(value.args[0], where, tree, parents, namespace)
            return {str(helper(name)) for name in inner}
    raise AssertionError(f"a worker name this guard cannot read: {ast.unparse(value)}")


def started_workers(source: str, namespace: Mapping[str, Any]) -> set[str]:
    """The name of every worker ``source`` starts: ``run_worker(…, name=…)`` and
    ``@work(…, name=…)``. A start without a name cannot be told apart, and one that is not
    a thread worker would be cut off by the quit rather than waited for, so either fails."""
    tree = ast.parse(source)
    parents = _parents(tree)
    names: set[str] = set()
    decorators = {
        decorator
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
    }
    starts = [*_calls_to(tree, "run_worker")]
    starts += [call for call in _calls_to(tree, "work") if call in decorators]
    for start in starts:
        named = next((kw.value for kw in start.keywords if kw.arg == "name"), None)
        if named is None:
            raise AssertionError(f"a worker started with no name: {ast.unparse(start)}")
        thread = next((kw.value for kw in start.keywords if kw.arg == "thread"), None)
        if not (isinstance(thread, ast.Constant) and thread.value is True):
            raise AssertionError(f"not a thread worker: {ast.unparse(start)}")
        names |= _names(named, _enclosing(start, parents), tree, parents, namespace)
    return names


DORMANT = {Path("views/explainability.py")}
"""UI modules no screen mounts: they start workers, but nothing in the shell can run them, so
the hand-over has none of theirs to wait for or refuse. A re-mounted view (the note at the top
of the module) goes back into the walk by leaving this set, and into both lists."""


def _ui_workers() -> set[str]:
    names: set[str] = set()
    package = Path(ui_package.__file__).parent
    for path in sorted(package.rglob("*.py")):
        if path.relative_to(package) in DORMANT:
            continue
        module = importlib.import_module(_module_name(path))
        names |= started_workers(path.read_text(encoding="utf-8"), vars(module))
    return names


UI_WORKERS = _ui_workers()
WRITING = ui_app._writing_workers()
CUT_OFF = ui_app._cut_off_workers()
WAITED = UI_WORKERS - set(WRITING) - CUT_OFF
"""The workers a hand-over waits out rather than refuses: every other one the UI starts."""


def test_the_lists_name_only_workers_the_ui_starts_and_never_both() -> None:
    """What the guard must still see: every writer and every worker safe to cut off is a
    worker the walk found, so a renamed one, or a walk gone blind, fails here."""
    assert set(WRITING) <= UI_WORKERS, set(WRITING) - UI_WORKERS
    assert CUT_OFF <= UI_WORKERS, CUT_OFF - UI_WORKERS
    assert not set(WRITING) & CUT_OFF
    assert len(WAITED) >= 10, f"the walk found too few to wait for: {sorted(WAITED)}"


def test_the_overdue_toast_names_every_read_it_waits_for_in_words() -> None:
    """Round 24 of #257: past its patience the hand-over named what it waited for by the
    workers' names (``doctor``, ``shell-accounts``, ``welcome-validate``), where the
    refusal beside it speaks in words. Every read the UI starts has words now, a writer
    keeps the refusal's, and a worker neither list knows is "a read"."""
    reads = ui_app._waited_reads()
    assert set(reads) == WAITED, set(reads) ^ WAITED
    assert all(words and words != name for name, words in reads.items())
    assert ui_app._in_words([ui_app._DOCTOR_WORKER]) == "the doctor's checks"
    assert ui_app._in_words(
        [ui_app.ACCOUNTS_WORKER, "hung-read", welcome_view.worker_name("validate"), "other"]
    ) == ("the accounts' read, a read and Welcome's folder check")
    assert ui_app._in_words([accounts_view.ARRANGE_WORKER]) == WRITING[accounts_view.ARRANGE_WORKER]


def test_the_accounts_readers_write_the_store_so_update_waits_for_them(fake_home: Path) -> None:
    """Round 19 of #257: listed here as readers, a usage reading and the reads of the slots
    let Update quit under them. A usage reading records a sample per account it fetches
    (``cached_usage_with_trends``, the page's), and a read of the slots, which the shell's header,
    Settings, the board and an agent's page each run, adds a registry row for a slot that
    has none (``_arranged``)."""
    account = _slot("work@example.com", "tok-work")
    with store_session() as store:
        assert store.claude_accounts() == []

    accounts_service.slot_labels()
    accounts_service.cached_usage_with_trends(
        [account], now=NOW, fetch=_Usage({"tok-work": _payload(40)})
    )

    with store_session() as store:
        assert account.slot in {row.slot for row in store.claude_accounts()}, "reconciled"
        assert store.usage_samples(account.slot, since=NOW - timedelta(days=1))
    readers = {
        accounts_view.USAGE_WORKER,
        ui_app.ACCOUNTS_WORKER,
        app_settings_view.ACCOUNTS_WORKER,
        board.LABELS_WORKER,
        agent_view.LABELS_WORKER,
    }
    assert readers <= WAITED, readers - WAITED


def test_the_doctor_reads_the_slots_and_writes_the_store_so_update_waits_for_it(
    fake_home: Path,
) -> None:
    """Round 20 of #257: the shell's Doctor (and a project tab's, which has since gone) was
    listed as a run that only reads, but once the store exists the accounts' checks list the
    slots, which adds a registry row for a slot that has none, as the accounts' readers do."""
    account = _slot("work@example.com", "tok-work")
    with store_session() as store:
        assert store.claude_accounts() == []

    diagnostics._claude_account_default_checks()

    with store_session() as store:
        assert account.slot in {row.slot for row in store.claude_accounts()}, "reconciled"
    assert ui_app._DOCTOR_WORKER in WAITED


def test_welcomes_folder_check_opens_the_store_so_update_waits_for_it(tmp_path: Path) -> None:
    """Round 21 of #257: Welcome's check of a typed folder was listed as one that reads
    the store, but it looks the folder up there through ``store_session``, whose open
    creates and migrates the store: on a machine that has none, typing a folder made it."""
    folder = tmp_path / "work"
    folder.mkdir()
    assert not paths.db_path().exists()

    verdict = first_run.validate_path(str(folder))

    assert verdict.root == folder and verdict.store_error is None
    assert paths.db_path().exists(), "the check created the store"
    assert welcome_view.worker_name("validate") in WAITED


def test_the_reads_round_22_found_open_the_store_and_are_waited_for_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 22 of #257: the workspace credits, Explainability's status (its view is dormant
    now) and step 1's list were filed as reads that write nothing. The credits and the
    status resolve a project's destination in the store and write the credits' cache, and
    step 1's list with no frame from the shell asks the store, which on a fresh machine
    makes it. None is listed now: every worker the UI starts is waited for unless it is
    safe to cut off."""
    monkeypatch.chdir(tmp_path)
    assert not paths.db_path().exists()

    welcome_view._find_candidates(None)

    assert paths.db_path().exists(), "step 1's list without a frame created the store"
    found = {accounts_view.CREDITS_WORKER, welcome_view.worker_name("candidates")}
    assert found <= WAITED, found - WAITED


def _welcome_name(step: str) -> str:
    return f"welcome-{step}"


def _work(**_kwargs: object) -> Callable[[Any], Any]:
    return lambda function: function


SHAPES = {
    "a literal": "def go(self):\n    self.run_worker(job, name='new-read', thread=True)\n",
    "a module constant": "def go(self):\n    self.run_worker(job, name=NEW_WORKER, thread=True)\n",
    "a decorator": "@work(thread=True, name=NEW_WORKER)\ndef go(self):\n    pass\n",
    "a helper's parameter": (
        "def _run(self, name, job):\n"
        "    self.run_worker(job, name=worker_name(name), group='g', thread=True)\n"
        "def _start_work(self, name, job):\n"
        "    self._run(name, job)\n"
        "def go(self):\n"
        "    self._start_work('read', job)\n"
    ),
}


@pytest.mark.parametrize("shape", SHAPES)
def test_the_rule_reads_every_shape_and_accuses_a_start_that_is_not_a_thread(shape: str) -> None:
    """Controls on the rule itself, one per shape a name reaches a worker start in: read,
    a new worker is one the hand-over's tests wait for; as a task, it would be cut off."""
    namespace = {"NEW_WORKER": "new-read", "worker_name": _welcome_name, "work": _work}
    started = started_workers(SHAPES[shape], namespace)
    exec(compile(SHAPES[shape], shape, "exec"), dict(namespace))  # the shape is real Python
    as_task = SHAPES[shape].replace("thread=True", "thread=False")

    expected = {"welcome-read"} if shape == "a helper's parameter" else {"new-read"}
    assert started == expected
    with pytest.raises(AssertionError, match="not a thread worker"):
        started_workers(as_task, namespace)


def test_a_worker_name_the_guard_cannot_read_fails_rather_than_passing() -> None:
    with pytest.raises(AssertionError, match="cannot read"):
        started_workers("def go(self):\n    self.run_worker(job, name=pick(), thread=True)\n", {})
    with pytest.raises(AssertionError, match="no name"):
        started_workers("def go(self):\n    self.run_worker(job, thread=True)\n", {})
    with pytest.raises(AssertionError, match="not a thread worker"):
        started_workers("def go(self):\n    self.run_worker(job, name='read')\n", {})
