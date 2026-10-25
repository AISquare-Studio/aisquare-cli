"""``asq exec -- COMMAND`` and ``asq reports``: run a command once, read its report back."""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
from contextlib import suppress
from typing import Annotated, Literal

import typer

from aisquare.cli.common import fail
from aisquare.core.state import get_state
from aisquare.services import command_reports as service

app = typer.Typer(
    help="Read saved command reports. Nothing here runs a command again.", no_args_is_help=True
)

Stream = Literal["stdout", "stderr", "both"]


def _emit_bytes(data: bytes, *, stderr: bool = False) -> None:
    stream = sys.stderr if stderr else sys.stdout
    if stream.isatty():
        stream.write(service.safe_text(data))
        stream.flush()
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        stream.write(data.decode("utf-8", errors="backslashreplace"))
        stream.flush()


def _emit_report(report: service.CommandReport, *, raw: bool, stream: Stream = "both") -> None:
    stdout = service.read_stream(report.id, "stdout", raw=raw) if stream != "stderr" else b""
    stderr = service.read_stream(report.id, "stderr", raw=raw) if stream != "stdout" else b""
    if get_state().json_output:
        output = {
            "report": report.model_dump(mode="json"),
            "metrics": report.metrics(),
            "encoding": "base64" if raw else "utf-8, controls escaped",
            "stdout": base64.b64encode(stdout).decode() if raw else service.safe_text(stdout),
            "stderr": base64.b64encode(stderr).decode() if raw else service.safe_text(stderr),
        }
        typer.echo(json.dumps(output))
        return
    _emit_bytes(stdout)
    _emit_bytes(stderr, stderr=True)
    _emit_footer(report, raw=raw, stream=stream)


def _emit_footer(report: service.CommandReport, *, raw: bool, stream: Stream) -> None:
    notes = [
        f"TRUNCATED {name} kept {record.retained_bytes}/{record.observed_bytes} bytes"
        for name, record in (("stdout", report.stdout), ("stderr", report.stderr))
        if record.truncated
    ]
    if report.interrupted_by is not None:
        notes.append(f"interrupted by signal {report.interrupted_by}")
    if report.output_pipes_held_open:
        notes.append("a background process held the output open; later output not kept")
    if report.project_id is not None and not report.source_known:
        notes.append("source identity unknown")
    detail = "".join(f"; {note}" for note in notes)
    hint = (
        f"Saved bytes of report {report.id}; nothing was run again."
        if raw
        else f"Originals: asq reports show {report.id} --raw (saved bytes; never reruns)."
    )
    # The footer must not land in the stream being recovered: `--stream stderr` reads
    # our stderr, so the footer goes to stdout there.
    typer.echo(
        f"\n[asq report {report.id}; exit {report.exit_code}; {report.format}{detail}]\n{hint}",
        err=stream != "stderr",
    )


def exec_command(
    ctx: typer.Context,
    raw: Annotated[
        bool, typer.Option("--raw", help="Show the output unshortened; originals are kept anyway.")
    ] = False,
    session: Annotated[
        str | None, typer.Option("--session", help="The board session this run is for.")
    ] = None,
    task: Annotated[str | None, typer.Option("--task", help="The task this run is for.")] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            help="The project, by a name or an id: record its checkout's source identity "
            "before and after.",
        ),
    ] = None,
    max_output_bytes: Annotated[
        int,
        typer.Option(
            "--max-output-bytes",
            min=1,
            max=service.MAX_STREAM_LIMIT,
            help="Bytes kept per stream (default 1 MiB, at most 16 MiB); truncation is marked.",
        ),
    ] = service.DEFAULT_STREAM_LIMIT,
) -> None:
    """Run a command once and save a report of how it ended: asq exec -- CMD ARGS...

    No shell is added and the arguments pass unchanged; standard input is inherited.
    Put global flags such as --json BEFORE exec: everything after -- is the command's.
    Exits with the command's status. Only git status's usage hints and pytest's
    progress lines are shortened (--raw or AISQUARE_REPORTS=off turns that off).
    Reports are kept 14 days, newest 64; asq reports show ID reads one back.
    """
    if not ctx.args:
        fail("Provide a command: asq exec -- COMMAND [ARGS...]", error="usage", exit_code=2)
    if ctx.args[0].startswith("-"):
        # An option `exec` does not know (`asq exec --json -- true`) arrived as the
        # command's first argument: it was run as a command named `--json`, a saved 127.
        # No command starts with a dash (validator round on #136, n2).
        fail(
            f"exec has no option {ctx.args[0]}: put global flags such as --json before exec, "
            "and the command after --",
            error="usage",
            exit_code=2,
        )
    if project is not None:
        project = _project_id(project)
    if task is not None:
        task = _board_task(task, project)
    try:
        report = service.run_command(
            ctx.args,
            compact=not raw,
            session_id=session,
            task_id=task,
            project_id=project,
            max_output_bytes=max_output_bytes,
        )
    except service.InterruptedAfterSave as stopped:
        # Interrupted while the report was being saved: it is saved, so name it, then
        # stop as the interrupt asked rather than going on to print the output.
        with suppress(OSError):
            _emit_footer(stopped.report, raw=raw, stream="both")
        raise typer.Exit(128 + stopped.number) from None
    except (OSError, ValueError) as exc:
        fail(
            f"Command report failed: {exc}. "
            "The command may already have run; check before retrying.",
            error="command_report_failed",
        )
    try:
        _emit_report(report, raw=raw)
    except BrokenPipeError:
        # The reader went away. The report is saved and the status is known, so the
        # footer and the exit status still tell the truth.
        with suppress(OSError):
            _emit_footer(report, raw=raw, stream="both")
    # sys.platform rather than hasattr: it is what mypy narrows, on the Windows leg too.
    if sys.platform != "win32" and report.signal is not None:
        # Die the way the command died, for a caller that reads the signal rather than
        # 128 + N. The report is already saved.
        if report.signal not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(report.signal, signal.SIG_DFL)
        os.kill(os.getpid(), report.signal)
    raise typer.Exit(report.exit_code)


def _project_id(ref: str) -> str:
    """``--project`` read as ``board --project`` reads it: a name or an id, recorded as the
    project's id. Kept as typed, a name was a board id no project has: the report was saved
    with its source identity unknown, and ``--task`` said the task was not on that board
    (TUI-401d (q)). A ref no project matches is refused before anything runs; with no store
    to read, the ref is kept as typed, as ``--task``'s is: a report needs no board."""
    from aisquare.services import fleet as fleet_service  # lazy: not every run's business

    try:
        return fleet_service.resolve_project(ref).id
    except fleet_service.FleetError as exc:
        fail(f"--project {ref}: {exc}; nothing was run", error="not_found", ref=ref)
    except Exception:  # the store cannot be read: a report needs no board
        return ref


def _board_task(ref: str, project: str | None) -> str:
    """``--task`` read as every other ``--task`` reads it: ``12``, ``#12`` or an id
    (prefix ok), on the ``--project`` board or the caller's, recorded as the task's id.
    Stored as typed, ``--task 1`` was a report "captured for task 1" that evidence from
    task #1 then refused (validator round on #136, M1). A ref the board does not have is
    refused before anything runs. With no board to read (the orchestrator off, the store
    unreadable), the ref is kept as typed: a report needs no board."""
    from aisquare.core.store import AmbiguousIdError
    from aisquare.services import briefs  # lazy: the board is not every run's business
    from aisquare.services.team import NoSuchTaskNumber, TeamDisabledError

    try:
        return briefs.resolve_task_ref(ref, project)
    except TeamDisabledError:
        return ref
    except (NoSuchTaskNumber, ValueError) as exc:
        why = str(exc)
    except AmbiguousIdError:
        why = f"{ref!r} matches several tasks; use more of its id"
    except KeyError:
        why = f"no task matches {ref!r} on this board"
    except Exception:  # the store cannot be read: a report needs no board
        return ref
    fail(f"--task {ref}: {why}; nothing was run", error="unknown_task", exit_code=2)


@app.command("show")
def show(
    report_id: Annotated[str, typer.Argument(help="The complete report ID.")],
    raw: Annotated[
        bool,
        typer.Option("--raw", help="The original bytes; under --json, base64 for exact recovery."),
    ] = False,
    stream: Annotated[str, typer.Option("--stream", help="stdout, stderr or both.")] = "both",
) -> None:
    """Print a saved report's output. Exits 0 when it was read, whatever the command did."""
    if stream not in {"stdout", "stderr", "both"}:
        fail("--stream must be stdout, stderr or both.", error="usage", exit_code=2)
    chosen: Stream = "stdout" if stream == "stdout" else "stderr" if stream == "stderr" else "both"
    try:
        _emit_report(service.load_report(report_id), raw=raw, stream=chosen)
    except (OSError, ValueError) as exc:
        fail(f"Cannot read report: {exc}", error="report_unavailable")


@app.command("list")
def list_() -> None:
    """Saved reports, newest first."""
    try:
        reports = service.list_reports()
    except (OSError, ValueError) as exc:
        fail(str(exc), error="reports_unavailable")
    if get_state().json_output:
        typer.echo(json.dumps([report.model_dump(mode="json") for report in reports]))
    elif not reports:
        typer.echo("No saved command reports.")
    else:
        for report in reports:
            argv = service.safe_text(json.dumps(report.argv).encode())
            when = report.finished_at.strftime("%Y-%m-%d %H:%M")
            typer.echo(f"{report.id}  {when}  exit={report.exit_code}  {report.format}  {argv}")


@app.command("stats")
def stats() -> None:
    """Measured report bytes, and token counts labelled as estimates."""
    try:
        reports = service.list_reports()
    except (OSError, ValueError) as exc:
        fail(str(exc), error="reports_unavailable")
    metrics = [report.metrics() for report in reports]

    def total(key: str) -> int:
        return sum(int(item[key]) for item in metrics)

    payload: dict[str, int | str] = {
        "retained_report_count": len(reports),
        "observed_bytes": total("observed_bytes"),
        "retained_bytes": total("retained_bytes"),
        "display_body_bytes": total("display_body_bytes"),
        "compaction_removed_bytes": total("compaction_removed_bytes"),
        "estimated_retained_tokens": total("estimated_retained_tokens"),
        "estimated_display_body_tokens": total("estimated_display_body_tokens"),
        "token_estimate_method": "UTF-8 byte count / 4, rounded up per report; not provider usage",
        "scope": (
            "Saved report bodies only; excludes receipts, prompts and other tools. "
            "Truncation is not counted as shortening."
        ),
    }
    if get_state().json_output:
        typer.echo(json.dumps(payload))
    else:
        for key, value in payload.items():
            typer.echo(f"{key}: {value}")


@app.command("prune")
def prune(
    keep: Annotated[
        int, typer.Option("--keep", min=0, help="Keep at most this many reports.")
    ] = service.RETAIN_REPORTS,
    days: Annotated[
        int, typer.Option("--days", min=0, help="Remove reports older than this many days.")
    ] = service.RETAIN_DAYS,
) -> None:
    """Apply retention now. Removes saved reports only, never project files."""
    protect = service.protected_report_ids()
    try:
        removed = service.prune_reports(keep=keep, days=days, protect=protect)
    except (OSError, ValueError) as exc:
        fail(str(exc), error="reports_unavailable")
    if protect is None:
        if get_state().json_output:
            typer.echo(json.dumps({"removed": removed, "protection_known": False}))
        else:
            typer.echo(
                f"Removed {len(removed)} abandoned runs; no report was removed, because which "
                "reports back recorded evidence could not be read."
            )
        return
    # Said, not silent: a prune of everything that left the reports evidence cites
    # read as though it had removed them all (validator round on #136, M3).
    kept = len(protect & {report.id for report in service.list_reports()})
    if get_state().json_output:
        payload = {"removed": removed, "protection_known": True, "kept_for_evidence": kept}
        typer.echo(json.dumps(payload))
    elif kept:
        typer.echo(
            f"Removed {len(removed)} saved reports; kept {kept} that recorded evidence "
            "cites. They stay until the briefs that cite them are purged "
            "(`aisquare project forget --purge`)."
        )
    else:
        typer.echo(f"Removed {len(removed)} saved reports.")


def register(root: typer.Typer) -> None:
    root.command(
        "exec",
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": True,
            "allow_interspersed_args": False,
        },
    )(exec_command)
    root.add_typer(app, name="reports")
