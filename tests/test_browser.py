"""Opening the verification link: when to try, when to say no."""

from __future__ import annotations

import threading
import webbrowser

import pytest

from aisquare.core import browser


class _Recording:
    """A stand-in for a webbrowser controller."""

    def __init__(self, name: str = "firefox") -> None:
        self.name = name
        self.opened = threading.Event()
        self.url = ""

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        self.url = url
        self.opened.set()
        return True


@pytest.mark.parametrize(
    ("environ", "platform", "isatty", "headless"),
    [
        ({"DISPLAY": ":0"}, "linux", True, False),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True, False),
        ({}, "linux", True, True),
        ({"DISPLAY": ":0"}, "linux", False, True),
        ({"DISPLAY": ":0", "SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}, "linux", True, True),
        ({"DISPLAY": ":0", "CI": "true"}, "linux", True, True),
        ({"CODESPACES": "true"}, "darwin", True, True),
        ({}, "darwin", True, False),
        ({}, "win32", True, False),
    ],
)
def test_headless_detection(
    environ: dict[str, str], platform: str, isatty: bool, headless: bool
) -> None:
    assert browser.is_headless(environ, platform=platform, stdout_isatty=isatty) is headless


def test_browser_env_print_only_values_never_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webbrowser, "get", lambda *_a, **_k: pytest.fail("must not be called"))
    for value in ("echo", "true", ":", ""):
        assert browser.open_url("https://x.example/cli", {"BROWSER": value}) is False


def test_browser_env_names_the_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    recording = _Recording("chromium")
    asked: list[str | None] = []

    def fake_get(using: str | None = None) -> _Recording:
        asked.append(using)
        return recording

    monkeypatch.setattr(webbrowser, "get", fake_get)
    # Headless markers do not matter when the user named a browser.
    assert browser.open_url(
        "https://x.example/cli", {"BROWSER": "chromium", "SSH_TTY": "/dev/pts/1"}
    )
    assert asked == ["chromium"]
    assert recording.opened.wait(2)
    assert recording.url == "https://x.example/cli"


def test_headless_never_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webbrowser, "get", lambda *_a, **_k: pytest.fail("must not be called"))
    assert browser.open_url("https://x.example/cli", {"SSH_CONNECTION": "yes"}) is False


def test_text_mode_and_generic_browsers_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for controller in (
        _Recording("lynx"),
        _Recording("/usr/bin/w3m"),
        webbrowser.GenericBrowser("foo"),
    ):
        monkeypatch.setattr(webbrowser, "get", lambda *_a, c=controller, **_k: c)
        monkeypatch.setattr(browser, "is_headless", lambda *_a, **_k: False)
        assert browser.open_url("https://x.example/cli", {}) is False


def test_a_real_browser_is_launched_in_the_background(monkeypatch: pytest.MonkeyPatch) -> None:
    recording = _Recording("firefox")
    monkeypatch.setattr(webbrowser, "get", lambda *_a, **_k: recording)
    monkeypatch.setattr(browser, "is_headless", lambda *_a, **_k: False)
    assert browser.open_url("https://x.example/cli", {}) is True
    assert recording.opened.wait(2)


def test_no_browser_at_all_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_browser(*_a: object, **_k: object) -> None:
        raise webbrowser.Error("could not locate runnable browser")

    monkeypatch.setattr(webbrowser, "get", no_browser)
    monkeypatch.setattr(browser, "is_headless", lambda *_a, **_k: False)
    assert browser.open_url("https://x.example/cli", {}) is False
