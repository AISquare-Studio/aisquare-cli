"""The voice page starts listening after a gesture, and keeps a typed request it could not send.

Review of #240, finding 14, in ``web/captain/index.html``.

- In listen mode the page makes its AudioContext from the websocket's ``hello``, with no
  user gesture. Chrome's autoplay policy leaves such a context suspended, ``startStream``
  marked the page streaming anyway and never came back: the chip said "listening" and no
  audio frame reached the server until the mic was muted and unmuted. Now any gesture
  resumes a suspended context, ``startStream`` streams only once the context runs, says
  what it waits for until then, and runs again when the context's state changes.
- A typed request was shown as said and its box cleared BEFORE ``send()`` looked at the
  socket, and ``send()`` dropped it in silence when the socket was not open (the 1.5 s
  reconnect gap). Now ``send()`` answers whether it went, and the text is shown and cleared
  only when it did; when it did not, the page says so and the text stays in the box.

No browser runs here and nothing may play audio, so these read the page's script as the
voice suite's own page test does (``test_the_page_is_served_from_the_package_and_carries_
the_worklet``): by its text and its structure. They pin what the script says; that Chrome
then resumes the context and frames flow is not something this suite can show.
"""

from __future__ import annotations

import re

from aisquare.services.captain import voice

GESTURES = ("pointerdown", "pointerup", "touchend", "keydown")
"""What grants a page its user activation: a mouse press, a touch's release, a key."""


def _script() -> str:
    """The page's one script, as the server serves it."""
    page = voice.page_bytes().decode("utf-8")
    start = page.index('<script type="module">')
    return page[start : page.index("</script>", start)]


def _block(script: str, opening: str) -> str:
    """The ``{ … }`` block that follows ``opening``, by counting braces."""
    assert opening in script, f"the page has no {opening!r}"
    start = script.index("{", script.index(opening))
    depth = 0
    for at in range(start, len(script)):
        if script[at] == "{":
            depth += 1
        elif script[at] == "}":
            depth -= 1
            if depth == 0:
                return script[start : at + 1]
    raise AssertionError(f"the block after {opening!r} never closes")


def _order(text: str, *marks: str) -> list[int]:
    """Where each mark first is, every one of them present."""
    found = [text.find(mark) for mark in marks]
    assert -1 not in found, dict(zip(marks, found, strict=True))
    return found


# --- the audio --------------------------------------------------------------------------


def test_any_gesture_resumes_a_suspended_context() -> None:
    script = _script()
    resume = _block(script, "function resumeContext()")
    assert "ctx.state === 'suspended'" in resume and "ctx.resume()" in resume
    registered = re.search(
        r"for \(const gesture of \[([^\]]*)\]\) "
        r"window\.addEventListener\(gesture, resumeContext, true\);",
        script,
    )
    assert registered is not None, "the handler is on the window, in the capture phase"
    assert re.findall(r"'([a-z]+)'", registered.group(1)) == list(GESTURES)


def test_listen_mode_streams_only_once_the_context_runs_and_says_what_it_waits_for() -> None:
    start = _block(_script(), "async function startStream()")
    built, gate, streaming = _order(
        start, "await ensureGraph()", "ctx.state !== 'running'", "streaming = true"
    )
    assert built < gate < streaming, "the state is asked after the graph and before streaming"
    waiting = _block(start, "if (ctx.state !== 'running')")
    assert "return;" in waiting, "no streaming flag, no 'listening' chip over a suspended context"
    assert "setStatus('click or press a key to start the mic')" in waiting
    assert "listening" not in waiting.split("setStatus(")[1].split(")")[0]


def test_the_stream_starts_when_the_context_starts_running() -> None:
    """Whatever resumed it — the gesture, or a browser that needed none — the next change
    of the context's state runs ``startStream`` again, while the page is still to listen."""
    waiting = _block(
        _block(_script(), "async function startStream()"), "if (ctx.state !== 'running')"
    )
    again = re.search(
        r"ctx\.addEventListener\('statechange', \(\) => \{ if \(listening\) startStream\(\); \}, "
        r"\{ once: true \}\);",
        waiting,
    )
    assert again is not None, waiting


# --- the typed request ------------------------------------------------------------------


def test_send_answers_whether_the_socket_took_it() -> None:
    send = _block(_script(), "function send(obj)")
    refused, sent, took = _order(
        send, "return false;", "ws.send(JSON.stringify(obj));", "return true;"
    )
    assert refused < sent < took
    assert re.search(r"if \(!ws \|\| ws\.readyState !== 1\) return false;", send)


def test_a_typed_request_is_shown_and_cleared_only_after_it_was_sent() -> None:
    typed = _block(_script(), "$('typed').addEventListener('submit'")
    asked, unsent, left, cleared, shown = _order(
        typed,
        "if (!send(",
        "say('note', '✗ not sent",
        "return; }",
        "textInput.value = ''",
        "say('said', text)",
    )
    assert asked < unsent < left < cleared < shown
    assert typed.count("textInput.value = ''") == 1 and typed.count("say('said', text)") == 1
    assert typed.count("send(") == 1, "one send, and its answer decides"


def test_an_unsent_request_stays_in_the_box_and_the_page_says_so() -> None:
    typed = _block(_script(), "$('typed').addEventListener('submit'")
    asked, cleared = _order(typed, "if (!send(", "textInput.value = ''")
    refusal = typed[asked:cleared]
    said = re.search(r"say\('note', '([^']*)'\)", refusal)
    assert said is not None, refusal
    assert "not sent" in said.group(1) and "still in the box" in said.group(1)
    assert "return;" in refusal[said.end() :], "it leaves before the box is cleared"


def test_the_typed_stop_word_mutes_the_page_only_when_it_was_sent() -> None:
    """``stop listening`` typed during a reconnect showed the page muted while the server,
    which never heard it, kept listening."""
    typed = _block(_script(), "$('typed').addEventListener('submit'")
    asked, muted = _order(typed, "if (!send(", "muted = true")
    assert asked < muted
    assert "{ t: 'stop' }" in typed and "{ t: 'text', text }" in typed
