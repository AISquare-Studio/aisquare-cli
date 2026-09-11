"""``asq exec -- COMMAND`` and original-report recovery; no external plugin."""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
from typing import Annotated, Literal

import typer

from aisquare.cli.common import fail
from aisquare.core.state import get_state
from aisquare.services import command_reports as service

app = typer.Typer(
    help="Read saved command reports without running commands again.", no_args_is_help=True
)


def _emit_bytes(data: bytes, *, stderr: bool = False) -> None:
    stream = sys.stderr if stderr else sys.stdout
    if stream.isatty():
        stream.write(service.safe_text(data))
        stream.flush()
    else:
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            stream.write(data.decode("utf-8", errors="backslashreplace"))
            stream.flush()


def _emit_report(
    report: service.CommandReport,
    *,
    raw: bool,
    stream: Literal["stdout", "stderr", "both"] = "both",
) -> None:
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
    truncated = [
        f"{name} retained {record.retained_bytes}/{record.observed_bytes} bytes"
        for name, record in (("stdout", report.stdout), ("stderr", report.stderr))
        if record.truncated
    ]
    truncation = f"; TRUNCATED: {', '.join(truncated)}" if truncated else ""
    typer.echo(
        f"\n[asq report {report.id}; exit {report.exit_code}; {report.format}{truncation}]\n"
        f"Originals: asq reports show {report.id} --raw (saved bytes; never reruns).",
        err=True,
    )


def exec_command(
    ctx: typer.Context,
    raw: Annotated[
        bool, typer.Option("--raw", help="Disable compaction; still save original bytes.")
    ] = False,
    session: Annotated[
        str | None, typer.Option("--session", help="Actual board session ID.")
    ] = None,
    task: Annotated[str | None, typer.Option("--task", help="Related task ID.")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Related project ID.")] = None,
    max_output_bytes: Annotated[
        int,
        typer.Option(
            "--max-output-bytes",
            min=1,
            max=service.MAX_STREAM_LIMIT,
            help="Retained bytes per stream; truncation is always reported.",
        ),
    ] = service.DEFAULT_STREAM_LIMIT,
) -> None:
    """Run a finite non-interactive command once, without a shell or changed flags.

    Put global output flags BEFORE exec, then options, then -- COMMAND ARGS.
    Only recognised git status / pytest progress is compacted. Other formats
    pass through. Pipes replace the child's output terminal; use direct launch
    for interactive programs. Reports retain the first 1 MiB per stream by
    default, for 14 days / newest 64 reports. AISQUARE_REPORTS=off disables
    compaction. This does not intercept other commands or built-in agent tools.
    """
    if not ctx.args:
        fail("Provide a command: asq exec -- COMMAND [ARGS...]", error="usage", exit_code=2)
    try:
        report = service.run_command(
            ctx.args,
            compact=not raw,
            session_id=session,
            task_id=task,
            project_id=project,
            max_output_bytes=max_output_bytes,
        )
        _emit_report(report, raw=raw)
    except (OSError, ValueError) as exc:
        fail(
            f"Command report failed: {exc}. "
            "The command may already have run; inspect before retrying.",
            error="command_report_failed",
        )
    if report.signal is not None:
        # Preserve signal termination for subprocess callers, not merely a
        # coincidentally equal numeric exit status. Output/record is saved first.
        if report.signal not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(report.signal, signal.SIG_DFL)
        os.kill(os.getpid(), report.signal)
    raise typer.Exit(report.exit_code)


@app.command("show")
def show(
    report_id: Annotated[str, typer.Argument(help="Complete saved report ID.")],
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Read original bytes; JSON uses base64 for exact recovery."),
    ] = False,
    stream: Annotated[
        str, typer.Option("--stream", help="stdout, stderr, or both (separate output streams).")
    ] = "both",
) -> None:
    """Read a saved result. Success means retrieval worked, regardless of original exit."""
    if stream not in {"stdout", "stderr", "both"}:
        fail("--stream must be stdout, stderr, or both.", error="usage", exit_code=2)
    try:
        report = service.load_report(report_id)
        chosen: Literal["stdout", "stderr", "both"] = stream  # type: ignore[assignment]
        _emit_report(report, raw=raw, stream=chosen)
    except (OSError, ValueError) as exc:
        fail(f"Cannot read report: {exc}", error="report_unavailable")


@app.command("list")
def list_() -> None:
    """List retained reports, newest first. Stored commands are not executed."""
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
            typer.echo(f"{report.id}  exit={report.exit_code}  {report.format}  {argv}")


@app.command("stats")
def stats() -> None:
    """Measured report bytes and labelled estimates, not total agent-token savings."""
    try:
        reports = service.list_reports()
    except (OSError, ValueError) as exc:
        fail(str(exc), error="reports_unavailable")
    payload = {
        "retained_report_count": len(reports),
        "observed_bytes": sum(r.stdout.observed_bytes + r.stderr.observed_bytes for r in reports),
        "retained_bytes": sum(r.stdout.retained_bytes + r.stderr.retained_bytes for r in reports),
        "display_body_bytes": sum(r.display_stdout_bytes + r.display_stderr_bytes for r in reports),
        "compaction_removed_bytes": sum(
            int(r.metrics()["compaction_removed_bytes"]) for r in reports
        ),
        "estimated_retained_tokens": sum(
            int(r.metrics()["estimated_retained_tokens"]) for r in reports
        ),
        "estimated_display_body_tokens": sum(
            int(r.metrics()["estimated_display_body_tokens"]) for r in reports
        ),
        "token_estimate_method": "UTF-8 byte count / 4, rounded up per report; not provider usage",
        "scope": (
            "Retained report bodies only, excludes receipts, prompts and other tools. "
            "Truncation is not compaction."
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
        int, typer.Option("--keep", min=0, help="Keep at most this many completed reports.")
    ] = service.RETAIN_REPORTS,
    days: Annotated[
        int, typer.Option("--days", min=0, help="Keep reports no older than this many days.")
    ] = service.RETAIN_DAYS,
) -> None:
    """Apply retention to completed reports; removes saved outputs, not project files."""
    try:
        removed = service.prune_reports(keep=keep, days=days)
    except (OSError, ValueError) as exc:
        fail(str(exc), error="reports_unavailable")
    if get_state().json_output:
        typer.echo(json.dumps({"removed": removed}))
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
