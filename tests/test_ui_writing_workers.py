"""Every worker the UI starts is either one Update waits for, or named here as one that reads.

Update and Uninstall wait for the workers ``ui_app._writing_workers()`` and
``ui_app._reconciling_workers()`` name. Those lists are kept by hand, and rounds 15 and 16
of #257 each found writers they left out; the test that drives Update takes its cases from
the same lists, so a new writer that is not in one passed everything (round 17). This guard
reads every ``run_worker(name=…)`` and ``@work(name=…)`` in ``aisquare.cli.ui`` from the
source, follows a name passed down through a helper's parameter (Welcome's ``_start_work``
→ ``_run`` → ``worker_name``), and requires each name to be in one of the app's lists or in
:data:`READ_ONLY` below, with why it writes nothing.
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
from aisquare.cli.ui.views import explainability as explainability_view
from aisquare.cli.ui.views import project as project_view
from aisquare.cli.ui.views import settings as settings_view
from aisquare.cli.ui.views import welcome as welcome_view
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import diagnostics, first_run
from tests.test_claude_accounts import NOW
from tests.test_claude_accounts import fake_home as _redirected_home
from tests.test_usage_aware_accounts import _payload, _slot, _Usage

#: The sibling file's redirected home, re-exported so pytest collects it here: the
#: accounts test below writes Claude Code's files, and none may land in the developer's.
fake_home = _redirected_home

READ_ONLY: dict[str, str] = {
    "aisquare-sign-in": "waits for the browser; a quit cancels it and nothing is stored "
    "past a cancel (see _writing_workers)",
    "welcome-look": "probes tmux, Claude Code and gh",
}
"""Workers that write nothing, so Update need not wait for them, with what they read.

None of the accounts' readers is one: a read of the slots reconciles the registry and a
usage reading records its samples (round 19 of #257). Nor is the doctor, whose checks read
the slots too (round 20), nor Welcome's check of a typed folder, which opens the store
(round 21), nor the credits, Explainability's status or step 1's list (round 22)."""


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
    ``@work(…, name=…)``. A start without a name cannot be told apart, so it fails."""
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
        names |= _names(named, _enclosing(start, parents), tree, parents, namespace)
    return names


def unclassified(started: set[str], writing: Mapping[str, str]) -> set[str]:
    """The started workers neither Update waits for nor :data:`READ_ONLY` explains."""
    return started - set(writing) - set(READ_ONLY)


def _ui_workers() -> set[str]:
    names: set[str] = set()
    for path in sorted(Path(ui_package.__file__).parent.rglob("*.py")):
        module = importlib.import_module(_module_name(path))
        names |= started_workers(path.read_text(encoding="utf-8"), vars(module))
    return names


UI_WORKERS = _ui_workers()
RECONCILING = ui_app._reconciling_workers()
WRITING = {**ui_app._writing_workers(), **RECONCILING}


def test_every_worker_the_ui_starts_is_waited_for_or_said_to_write_nothing() -> None:
    """A new writer that is not in ``_writing_workers`` quits under an Update, as round
    15's and 16's did; a renamed worker drops out of the list the same way."""
    assert unclassified(UI_WORKERS, WRITING) == set()


def test_the_lists_name_only_workers_the_ui_starts_and_never_both() -> None:
    """What the guard must still see: every writer and every reader it explains is a
    worker the walk found, so a walk gone blind fails here, not by passing above."""
    assert set(WRITING) <= UI_WORKERS, set(WRITING) - UI_WORKERS
    assert set(READ_ONLY) <= UI_WORKERS, set(READ_ONLY) - UI_WORKERS
    assert not set(WRITING) & set(READ_ONLY)
    assert not set(ui_app._writing_workers()) & set(RECONCILING)


def test_the_accounts_readers_write_the_store_so_update_waits_for_them(fake_home: Path) -> None:
    """Round 19 of #257: listed here as readers, a usage reading and the reads of the slots
    let Update quit under them. A usage reading records a sample per account
    (``read_usage_with_trends``), and a read of the slots, which the shell's header,
    Settings, the board and an agent's page each run, adds a registry row for a slot that
    has none (``_arranged``)."""
    account = _slot("work@example.com", "tok-work")
    with store_session() as store:
        assert store.claude_accounts() == []

    accounts_service.slot_labels()
    accounts_service.read_usage_with_trends(
        [account], now=NOW, fetch=_Usage({"tok-work": _payload(40)})
    )

    with store_session() as store:
        assert account.slot in {row.slot for row in store.claude_accounts()}, "reconciled"
        assert store.usage_samples(account.slot, since=NOW - timedelta(days=1))
    readers = {
        accounts_view.USAGE_WORKER,
        ui_app.ACCOUNTS_WORKER,
        settings_view.ACCOUNTS_WORKER,
        board.LABELS_WORKER,
        agent_view.LABELS_WORKER,
    }
    assert readers <= set(RECONCILING), readers - set(RECONCILING)


def test_the_doctor_reads_the_slots_and_writes_the_store_so_update_waits_for_it(
    fake_home: Path,
) -> None:
    """Round 20 of #257: the shell's and a project tab's Doctor were listed as runs that
    only read, but once the store exists the accounts' checks list the slots, which adds a
    registry row for a slot that has none, as the accounts' readers do."""
    account = _slot("work@example.com", "tok-work")
    with store_session() as store:
        assert store.claude_accounts() == []

    diagnostics._claude_account_default_checks()

    with store_session() as store:
        assert account.slot in {row.slot for row in store.claude_accounts()}, "reconciled"
    doctors = {ui_app._DOCTOR_WORKER, project_view.DOCTOR_WORKER}
    assert doctors <= set(RECONCILING), doctors - set(RECONCILING)


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
    assert welcome_view.worker_name("validate") in RECONCILING


def test_the_credits_the_status_and_step_1s_list_open_the_store_so_update_waits_for_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 22 of #257: listed here as reads, the workspaces' credits and Explainability's
    status resolve a project's destination in the store and write the credits' cache, and
    step 1's list with no frame from the shell asks the store, which on a fresh machine
    makes it."""
    monkeypatch.chdir(tmp_path)
    assert not paths.db_path().exists()

    welcome_view._find_candidates(None)

    assert paths.db_path().exists(), "step 1's list without a frame created the store"
    found = {
        accounts_view.CREDITS_WORKER,
        explainability_view.STATUS_WORKER,
        welcome_view.worker_name("candidates"),
    }
    assert found <= set(RECONCILING), found - set(RECONCILING)


def _welcome_name(step: str) -> str:
    return f"welcome-{step}"


def _work(**_kwargs: object) -> Callable[[Any], Any]:
    return lambda function: function


SHAPES = {
    "a literal": "def go(self):\n    self.run_worker(job, name='new-writer')\n",
    "a module constant": "def go(self):\n    self.run_worker(job, name=NEW_WORKER)\n",
    "a decorator": "@work(thread=True, name=NEW_WORKER)\ndef go(self):\n    pass\n",
    "a helper's parameter": (
        "def _run(self, name, job):\n"
        "    self.run_worker(job, name=worker_name(name), group='g')\n"
        "def _start_work(self, name, job):\n"
        "    self._run(name, job)\n"
        "def go(self):\n"
        "    self._start_work('writer', job)\n"
    ),
}


@pytest.mark.parametrize("shape", SHAPES)
def test_the_rule_accuses_an_unlisted_writer_and_spares_a_listed_one(shape: str) -> None:
    """Controls on the rule itself, one per shape a name reaches a worker start in."""
    namespace = {"NEW_WORKER": "new-writer", "worker_name": _welcome_name, "work": _work}
    started = started_workers(SHAPES[shape], namespace)
    exec(compile(SHAPES[shape], shape, "exec"), dict(namespace))  # the shape is real Python

    expected = {"welcome-writer"} if shape == "a helper's parameter" else {"new-writer"}
    assert started == expected
    assert unclassified(started, WRITING) == expected, "unlisted: accused"
    assert unclassified(started, {name: "it writes" for name in expected}) == set(), "listed"


def test_a_worker_name_the_guard_cannot_read_fails_rather_than_passing() -> None:
    with pytest.raises(AssertionError, match="cannot read"):
        started_workers("def go(self):\n    self.run_worker(job, name=pick())\n", {})
    with pytest.raises(AssertionError, match="no name"):
        started_workers("def go(self):\n    self.run_worker(job)\n", {})
