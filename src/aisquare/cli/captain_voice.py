"""``aisquare captain voice`` — serve the captain's voice page (card T3).

One leaf on the captain group, in its own module so T2's ``say``/``chat`` and
T5's verbs in ``cli/captain.py`` and ``cli/captain_verbs.py`` never collide with
it. It prints the URL (token in the fragment), a QR for the phone and the ``adb
reverse`` line, then serves :func:`aisquare.services.captain.voice.serve` on
loopback until Ctrl-C. ``--show-token`` prints and exits. The dependency guard
is the ``serve``/``xr`` idiom: a missing extra is one sentence with the install
line, never a traceback out of uvicorn.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state

VOICE_INSTALL = "pip install 'aisquare-cli[voice]'"
DEFAULT_PORT = 8749


def register(app: typer.Typer) -> None:
    """Put ``voice`` on the ``captain`` group (``cli.captain``), one line there."""
    app.command("voice")(voice_page)


def _find_spec(name: str) -> object | None:
    """Indirection so a test can take a module away without patching importlib itself."""
    import importlib.util

    return importlib.util.find_spec(name)


def voice_dependency_error() -> str | None:
    """Why the voice page cannot be served here, or ``None`` when it can."""
    from aisquare.services.captain.voice import REQUIRED_MODULES

    missing = []
    for name in REQUIRED_MODULES:
        try:
            if _find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    if not missing:
        return None
    return f"the voice extra is not installed (missing {', '.join(missing)}) — {VOICE_INSTALL}"


def voice_page(
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="The page's port.")
    ] = DEFAULT_PORT,
    host: Annotated[
        str,
        typer.Option("--host", help="Loopback only: the token is the only lock on this page."),
    ] = "127.0.0.1",
    mode: Annotated[
        str | None,
        typer.Option(
            "--mode",
            help="focus (hold to talk) or listen (always listening); saved as the mode for "
            "every page (state.json captain_voice_mode). Not given: the saved mode, else focus.",
        ),
    ] = None,
    speaker: Annotated[
        str | None, typer.Option("--speaker", help="on or off: whether replies are spoken.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Whisper model: base.en (default) or small.en.")
    ] = None,
    show_token: Annotated[
        bool, typer.Option("--show-token", help="Print the URL, QR and token; do not serve.")
    ] = False,
) -> None:
    """Serve the voice page: hold to talk or always listening, replies spoken back.

    Open the printed URL in a browser on this PC (localhost is the secure
    context the microphone needs), or on an Android phone after the printed
    `adb reverse` line. Runs until Ctrl-C.
    """
    from aisquare.services.captain import speaker as speaker_mod
    from aisquare.services.captain import voice
    from aisquare.services.mcp_server import serve_token

    if mode is not None and mode not in voice.MODES:
        fail(f"--mode must be one of {', '.join(voice.MODES)}", error="bad_mode")
    if host not in voice.LOOPBACK_HOSTS:
        fail(
            f"--host {host} is not one of {', '.join(sorted(voice.LOOPBACK_HOSTS))}: the token "
            "is the only lock on this page, so it binds loopback only (a phone reaches it "
            "through adb reverse over USB)",
            error="not_loopback",
        )
    if speaker is not None and speaker not in ("on", "off"):
        fail("--speaker takes on or off", error="bad_speaker")
    try:
        chosen = speaker_mod.machine_voice()  # a bad [captain] speaker: one line, no traceback
    except ValueError as exc:
        fail(str(exc), error="bad_speaker_config")
    problem = voice_dependency_error()
    if problem is not None and not show_token:
        fail(problem, error="voice_not_installed")
    # Nothing is written before every refusal above has had its say.
    if speaker is not None:
        speaker_mod.set_speaker(speaker == "on")
    if mode is not None:
        voice.set_voice_mode("listen" if mode == "listen" else "focus")  # the key is its home
    effective: voice.Mode = voice.voice_mode() or "focus"
    token = serve_token()
    url = voice.voice_url(port, token)
    report: dict[str, Any] = {
        "url": url,
        "port": port,
        "host": host,
        "mode": effective,
        "speaker": speaker_mod.speaker_on(),
        "adb_reverse": voice.adb_reverse(port),
        "serving": not show_token,
    }
    if get_state().json_output:
        typer.echo(json.dumps(report, ensure_ascii=False))
    else:
        console = stdout_console()
        console.print(f"captain voice page: {url}")
        qr = voice.qr_lines(url)
        if qr:
            console.print()
            for line in qr:
                console.print(f"  {line}")
            console.print()
        console.print(f"mode: {effective} · speaker: {'on' if report['speaker'] else 'off'}")
        console.print(f"Android over USB: {voice.adb_reverse(port)}, then open the same URL there")
        if problem is not None:
            console.print(f"note: {problem}")
    if show_token:
        return
    voice.serve(
        token=token,
        port=port,
        host=host,
        mode=effective,
        hooks=voice.Hooks(
            transcriber_factory=lambda: voice.transcriber(model),
            on_thinking=_print_thinking,
            voice=chosen,
        ),
    )


def _print_thinking(on: bool) -> None:
    """The CLI side of the thinking signal: the terminal says it too (the card's line).

    Under ``--json`` stdout is the report and nothing else, so the line goes to stderr.
    """
    if get_state().json_output:
        stderr_console().print("thinking…" if on else "idle", style="dim")
        return
    stdout_console().print("thinking…" if on else "idle", style="yellow" if on else "dim")
