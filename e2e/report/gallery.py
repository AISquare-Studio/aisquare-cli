"""gallery.html: one self-contained page a reviewer opens from the Actions artifact.

What it shows, top to bottom: a verdict banner (colour, counts, duration, what
failed), the environment and versions, run notes, a journey index, then each
journey as a story with a timeline of step cards, each card a screenshot framed
as the terminal window it came from beside its checks; the watchlist of known
bugs last. A click on a screenshot opens a lightbox (←/→ between shots, Esc).

Rules, each for a reason:

- **One file; pictures inlined as data: URIs.** An artifact arrives as a zip
  and gets opened wherever it lands, often without the files beside it.
- **Readable without JavaScript.** The script only adds the lightbox. Every
  picture is also a link to its PNG (``../shots/…``), so where scripts are off
  a click still shows it full size, whenever the artifact is unzipped.
- **No network.** No fonts, CDNs or analytics: an artifact is public, must not
  phone home, and must look the same offline.
- **Everything escaped.** run.json carries text the product printed; it is
  data, and it passes through ``html.escape`` on the way in, every byte.
- **Paths are trusted only inside the run directory.** run.json names files by
  relative path; one that is absolute or climbs out with ``..`` is shown as
  missing rather than read, so a crafted run.json cannot inline ``~/.ssh`` into
  a page that gets uploaded.
- **Deterministic.** No build timestamp: the same run.json gives the same
  bytes, so two reports can be diffed.
"""

from __future__ import annotations

import base64
import html
import re
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from e2e.contracts import Journey, Status, Step, Verdict
from e2e.report.facts import (
    VERDICT_MEANING,
    WATCH_LEGEND,
    Facts,
    JourneyFacts,
    Links,
    duration,
    headline,
    short_step,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TEXT_LIMIT = 64 * 1024
"""Screen text beyond this is linked, not inlined: a screen is a few KB."""
_HERE = Path(__file__).resolve().parent
_VERDICT_MARK: dict[Verdict, str] = {
    "PASSED": "✓",
    "RC_FAILED": "✗",
    "ENV_FAILED": "!",
    "GATE_ERROR": "?",
}
_VERDICT_COLOUR: dict[Verdict, str] = {
    "PASSED": "#3fb950",
    "RC_FAILED": "#f85149",
    "ENV_FAILED": "#e3a52f",
    "GATE_ERROR": "#a371f7",
}
# A watch item's status as a tone: an open known bug is amber news, not a red failure.
_WATCH_TONE: dict[Status, str] = {
    "failed": "w-open",
    "passed": "w-fixed",
    "error": "s-error",
    "skipped": "s-skipped",
}


@dataclass(frozen=True)
class Page:
    html: str
    shots_inlined: int
    inlined_bytes: int
    missing: tuple[str, ...]
    """Paths run.json names that are not (safely) in the run directory."""


def render(facts: Facts, run_dir: Path, links: Links) -> Page:
    builder = _Builder(facts, run_dir.resolve(), links)
    page = builder.page()
    return Page(page, builder.inlined, builder.inlined_bytes, tuple(builder.missing))


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def _rich(text: str) -> str:
    """Escaped text whose `backtick spans` (titles quote commands) become <code>."""
    parts = _CODE_SPAN.split(text)  # odd indexes are the insides of the spans
    return "".join(
        f"<code>{_e(part)}</code>" if index % 2 else _e(part) for index, part in enumerate(parts)
    )


def _plain(text: str) -> str:
    """For attributes and script-read data, where markup would show: backticks dropped."""
    return _e(_CODE_SPAN.sub(r"\1", text))


def _anchor(text: str) -> str:
    """An id attribute (and fragment) from a journey or step id."""
    return re.sub(r"[^A-Za-z0-9._:-]", "-", text) or "x"


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG's IHDR, which the format puts first."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{count} B"  # unreachable; keeps the return type total


def _clock(stamp: str, pattern: str) -> str:
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return stamp
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
        return moment.strftime(pattern) + " UTC"
    return moment.strftime(pattern)


@dataclass
class _Builder:
    facts: Facts
    run_dir: Path
    links: Links
    inlined: int = 0
    inlined_bytes: int = 0
    missing: list[str] = field(default_factory=list)

    # -- files ------------------------------------------------------------------------

    def inside(self, relative: str | None) -> Path | None:
        """The file a run.json path names, if it is a file inside the run directory."""
        if not relative:
            return None
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            self.missing.append(relative)
            return None
        resolved = (self.run_dir / candidate).resolve()
        if not resolved.is_relative_to(self.run_dir) or not resolved.is_file():
            self.missing.append(relative)
            return None
        return resolved

    @staticmethod
    def href(relative: str) -> str:
        """A link from report/ to a file beside it in the artifact."""
        return "../" + quote(relative.replace("\\", "/"), safe="/")

    # -- the page ---------------------------------------------------------------------

    def page(self) -> str:
        facts = self.facts
        verdict = facts.verdict
        title = f"E2E {verdict} · {facts.run.run_id}"
        icon = "data:image/svg+xml," + quote(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
            f"<circle cx='8' cy='8' r='7' fill='{_VERDICT_COLOUR[verdict]}'/></svg>"
        )
        body = "\n".join(
            [
                self.topbar(),
                '<main class="wrap" id="top">',
                self.banner(),
                self.environment(),
                self.index(),
                *(self.journey(j) for j in facts.journeys),
                self.watchlist(),
                self.footer(),
                "</main>",
                self.lightbox(),
            ]
        )
        css = (_HERE / "gallery.css").read_text(encoding="utf-8")
        script = (_HERE / "gallery.js").read_text(encoding="utf-8")
        return (
            "<!doctype html>\n"
            '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="color-scheme" content="dark">\n'
            '<meta name="referrer" content="no-referrer">\n'
            f"<title>{_e(title)}</title>\n"
            f'<link rel="icon" href="{_e(icon)}">\n'
            f"<style>\n{css}</style>\n</head>\n"
            f'<body class="v-{verdict}">\n{body}\n<script>\n{script}</script>\n</body>\n</html>\n'
        )

    def topbar(self) -> str:
        chips = "".join(
            f'<a class="jchip s-{j.status}" href="#{_e(_anchor(j.journey.id))}" '
            f'title="{_plain(j.journey.title)} · {j.status}"><i></i>{_e(j.journey.id)}</a>'
            for j in self.facts.journeys
        )
        if self.facts.watch:
            states = {w.status for w in self.facts.watch}
            tone = "w-open" if "failed" in states else "w-fixed" if states == {"passed"} else ""
            tone = tone or "s-skipped"  # not checked (or errored): no news either way
            chips += f'<a class="jchip {tone}" href="#watchlist"><i></i>watchlist</a>'
        return (
            '<nav class="topbar"><div class="wrap">'
            '<a class="brand" href="#top">aisquare <span>E2E</span></a>'
            f'<span class="vchip">{_e(self.facts.verdict)}</span>'
            f'<div class="jchips">{chips}</div>'
            f'<span class="runid">{_e(self.facts.run.run_id)}</span>'
            "</div></nav>"
        )

    def banner(self) -> str:
        facts = self.facts
        run = facts.run
        counts = facts.counts
        total_steps = counts.total
        kpis = "".join(
            f"<div><dt>{name}</dt><dd>{value}</dd></div>"
            for name, value in (
                (
                    "Journeys",
                    f"{facts.journeys_passed}<small> / {len(facts.journeys)} passed</small>",
                ),
                ("Steps", f"{counts.passed}<small> / {total_steps} passed</small>"),
                ("Duration", _e(duration(facts.duration_s))),
                ("Started", f"<small>{_e(_clock(run.started_at, '%Y-%m-%d %H:%M'))}</small>"),
            )
        )
        segments = "".join(
            f'<span class="s-{status}" style="flex:{amount}" title="{amount} {status}"></span>'
            for status, amount in counts.as_dict().items()
            if amount
        )
        pills = "".join(
            f'<li class="s-{status}"><i></i><b>{amount}</b> {status}</li>'
            for status, amount in counts.as_dict().items()
        )
        failing = ""
        if facts.failures:
            items = []
            for failure in facts.failures:
                target = (
                    failure.step.id
                    if failure.step
                    else (failure.journey.id if failure.journey else None)
                )
                name = f"<code>{_e(failure.test)}</code>"
                if target:
                    name = f'<a href="#{_e(_anchor(target))}">{name}</a>'
                kind = failure.verdict_class
                items.append(
                    f'<li><span class="cls k-{kind}">{kind}</span>'
                    f'<span>{name} <span class="msg">{_e(failure.message)}</span></span></li>'
                )
            failing = (
                '<div class="failing"><h2>What failed</h2><ul>' + "".join(items) + "</ul></div>"
            )
        watch = ""
        if facts.watch:
            still = sum(1 for w in facts.watch if w.status == "failed")
            fixed = sum(1 for w in facts.watch if w.status == "passed")
            watch = (
                '<ul class="pills"><li class="w-open"><i></i>'
                f'<a href="#watchlist">Watchlist</a>: <b>{still}</b> still open</li>'
                f'<li class="w-fixed"><i></i><b>{fixed}</b> fixed · promote</li></ul>'
            )
        return (
            '<header class="banner">'
            '<div class="banner-top">'
            f'<div class="vmark" aria-hidden="true">{_VERDICT_MARK[facts.verdict]}</div>'
            '<div class="vtext">'
            f'<p class="eyebrow">aisquare nightly E2E · {_e(run.env.track)} · {_e(run.env.os)}</p>'
            f"<h1>{_e(facts.verdict)}</h1>"
            f'<p class="why">{_e(VERDICT_MEANING[facts.verdict])}{self.counted()}</p>'
            "</div>"
            f'<dl class="kpis">{kpis}</dl>'
            "</div>"
            f'<div class="tally"><div class="bar" role="img" aria-label="step outcomes">'
            f"{segments}</div>"
            f'<ul class="pills">{pills}</ul>{watch}</div>'
            f"{failing}"
            "</header>"
        )

    def counted(self) -> str:
        """The headline after the meaning, where it adds a count rather than repeating it."""
        line = headline(self.facts)
        if self.facts.verdict in ("PASSED", "RC_FAILED") or line == "nothing was tested":
            return f' <span class="msg">({_e(line)})</span>'
        return ""

    def environment(self) -> str:
        run = self.facts.run
        env = run.env
        commit = f"<code>{_e(env.commit)}</code>"
        commit_url = self.links.commit_url(env.commit)
        if commit_url:
            commit = f'<a href="{_e(commit_url)}">{commit}</a>'
        run_id = f"<code>{_e(run.run_id)}</code>"
        if self.links.run_url:
            run_id += f' · <a href="{_e(self.links.run_url)}">workflow run</a>'
        rows = [
            ("OS", _e(env.os)),
            ("Runner", _e(env.runner)),
            ("Track", _e(env.track)),
            ("Agent", _e(env.agent)),
            ("aisquare", f"<code>{_e(env.aisquare)}</code>"),
            ("Commit", commit),
            ("Run", run_id),
            (
                "Time",
                f"{_e(_clock(run.started_at, '%Y-%m-%d %H:%M:%S'))} → "
                f"{_e(_clock(run.finished_at, '%H:%M:%S'))}",
            ),
        ]
        facts_html = "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in rows)
        versions = (
            "".join(
                f"<tr><th>{_e(name)}</th><td>{_e(value)}</td></tr>"
                for name, value in sorted(env.versions.items(), key=lambda kv: kv[0].lower())
            )
            or '<tr><td class="empty">none recorded</td></tr>'
        )
        notes = ""
        if self.facts.notes:
            notes = (
                '<section class="panel" aria-labelledby="notes-h"><h2 id="notes-h">Notes</h2>'
                '<ul class="notes">'
                + "".join(f"<li>{_rich(note)}</li>" for note in self.facts.notes)
                + "</ul></section>"
            )
        return (
            '<div class="grid2">'
            '<section class="panel" aria-labelledby="env-h"><h2 id="env-h">Environment</h2>'
            f'<dl class="kv">{facts_html}</dl></section>'
            '<section class="panel" aria-labelledby="ver-h"><h2 id="ver-h">Versions</h2>'
            f'<table class="versions">{versions}</table></section>'
            f"</div>{notes}"
        )

    def index(self) -> str:
        rows = []
        for j in self.facts.journeys:
            dots = "".join(
                f'<a class="s-{status}" href="#{_e(_anchor(step.id))}" '
                f'title="{_e(step.id)} · {status}"></a>'
                for step, status in j.steps
            )
            rows.append(
                f'<tr><td><span class="badge s-{j.status}">{j.status}</span></td>'
                f'<td><a class="jtitle" href="#{_e(_anchor(j.journey.id))}">'
                f"<code>{_e(j.journey.id)}</code>{_rich(j.journey.title)}</a></td>"
                f'<td><div class="stepdots">{dots}</div></td>'
                f'<td class="num">{j.steps_passed}/{len(j.steps)}</td>'
                f'<td class="num">{_e(duration(j.journey.duration_s))}</td></tr>'
            )
        if not rows:
            rows.append('<tr><td colspan="5" class="empty">No journeys ran.</td></tr>')
        return (
            '<section class="panel index" aria-labelledby="index-h"><h2 id="index-h">Journeys</h2>'
            "<table><thead><tr><th>Result</th><th>Journey</th><th>Steps</th>"
            '<th class="num">Passed</th><th class="num">Time</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )

    # -- journeys and their steps -----------------------------------------------------

    def journey(self, facts: JourneyFacts) -> str:
        journey = facts.journey
        badge = f'<span class="badge s-{facts.status}">{facts.status}</span>'
        meta = [
            f"{facts.steps_passed}/{len(facts.steps)} steps passed",
            _e(duration(journey.duration_s)),
        ]
        return self._journey_section(
            journey,
            tone=f"s-{facts.status}",
            heading=f'<span class="jid">{_e(journey.id)}</span><h2>{_rich(journey.title)}</h2>',
            badge=badge,
            meta=meta,
            steps=facts.steps,
            watch=False,
        )

    def _journey_section(
        self,
        journey: Journey,
        *,
        tone: str,
        heading: str,
        badge: str,
        meta: list[str],
        steps: tuple[tuple[Step, Status], ...],
        watch: bool,
    ) -> str:
        video = self.video(journey)
        if video:
            meta.append(video)
        parts = [
            f'<section class="journey {tone}" id="{_e(_anchor(journey.id))}">',
            f'<header class="jhead">{heading}{badge}'
            f'<div class="jmeta">{"".join(f"<span>{m}</span>" for m in meta)}</div></header>',
        ]
        if journey.story:
            parts.append(f'<p class="story">{_rich(journey.story)}</p>')
        if journey.error:
            label = "Skipped" if journey.status == "skipped" else "Error"
            if watch and journey.status == "failed":
                label = "Reproduced"  # an open known bug, doing what the QA report said
            parts.append(f'<p class="jerror"><b>{label}</b><code>{_e(journey.error)}</code></p>')
        if steps:
            cards = "".join(
                self.step(journey, step, status, number, watch=watch)
                for number, (step, status) in enumerate(steps, start=1)
            )
            parts.append(f'<ol class="timeline">{cards}</ol>')
        else:
            parts.append('<p class="empty">No steps recorded.</p>')
        parts.append("</section>")
        return "".join(parts)

    def video(self, journey: Journey) -> str:
        if not journey.video:
            return ""
        path = self.inside(journey.video)
        if path is None:
            return f'<span class="missing" title="{_e(journey.video)}">▶ video missing</span>'
        size = human_bytes(path.stat().st_size)
        return f'<a href="{_e(self.href(journey.video))}">▶ video</a> <small>{size}</small>'

    def step(
        self, journey: Journey, step: Step, status: Status, number: int, *, watch: bool
    ) -> str:
        tone = _WATCH_TONE[status] if watch and status != "passed" else f"s-{status}"
        label = "still open" if watch and status == "failed" else status
        short = short_step(journey, step)
        anchor = _anchor(step.id)
        checks = "".join(
            f'<li class="{"ok" if check.ok else "ko"}"><span class="mk">'
            f'{"✓" if check.ok else "✗"}</span><div><span class="cn">{_rich(check.name)}</span>'
            + (f'<span class="cd">{_rich(check.detail)}</span>' if check.detail else "")
            + "</div></li>"
            for check in step.checks
        )
        checks_html = (
            f'<ul class="checks">{checks}</ul>'
            if checks
            else '<p class="nochecks">No checks recorded.</p>'
        )
        note = f'<p class="note">{_e(step.note)}</p>' if step.note else ""
        links: list[str] = []
        text_html = ""
        if step.text:
            text_path = self.inside(step.text)
            if text_path is None:
                links.append('<span class="missing">screen text missing</span>')
            else:
                links.append(f'<a href="{_e(self.href(step.text))}">screen text</a>')
                text_html = self.screen_text(text_path)
        if step.shot:
            links.append(f'<a href="{_e(self.href(step.shot))}">PNG</a>')
        links.append(
            f'<span title="{_e(step.started_at)}">{_e(_clock(step.started_at, "%H:%M:%S"))}</span>'
        )
        frame = self.terminal(step, tone, label, short)
        return (
            f'<li class="step {tone}" id="{_e(anchor)}">'
            f'<span class="node" aria-hidden="true">{number}</span>'
            f"{frame}"
            '<div class="sbody">'
            f'<div class="shead"><span class="badge {tone}">{_e(label)}</span>'
            f'<span class="dur">{_e(duration(step.duration_s))}</span></div>'
            f'<h3><a href="#{_e(anchor)}">{_rich(step.title)}</a></h3>'
            f'<code class="sid">{_e(step.id)}</code>'
            f"{checks_html}{note}{text_html}"
            f'<p class="slinks">{" · ".join(links)}</p>'
            "</div></li>"
        )

    def terminal(self, step: Step, tone: str, label: str, short: str) -> str:
        bar = (
            '<figcaption class="term-bar">'
            '<span class="lights" aria-hidden="true"><i></i><i></i><i></i></span>'
            f'<span class="term-title">{_rich(step.title)}</span>'
            f'<span class="term-tag">{_e(short)}</span></figcaption>'
        )
        return f'<figure class="term">{bar}{self.picture(step, tone, label)}</figure>'

    def picture(self, step: Step, tone: str, label: str) -> str:
        if not step.shot:
            why = (
                "skipped: nothing to show" if step.status == "skipped" else "no screenshot recorded"
            )
            return f'<div class="noshot">{why}</div>'
        path = self.inside(step.shot)
        if path is None:
            return f'<div class="noshot">screenshot missing<br><code>{_e(step.shot)}</code></div>'
        data = path.read_bytes()
        size = png_size(data)
        if size is None:
            return f'<div class="noshot">not a PNG<br><code>{_e(step.shot)}</code></div>'
        self.inlined += 1
        self.inlined_bytes += len(data)
        encoded = base64.b64encode(data).decode("ascii")
        return (
            f'<a class="shot" href="{_e(self.href(step.shot))}" data-step="{_e(step.id)}" '
            f'data-title="{_plain(step.title)}" data-tone="{_e(tone)}" data-label="{_e(label)}">'
            f'<img src="data:image/png;base64,{encoded}" width="{size[0]}" height="{size[1]}" '
            f'alt="{_e("Screenshot: " + step.title)}" loading="lazy" decoding="async">'
            '<span class="zoom" aria-hidden="true">⤢ enlarge</span></a>'
        )

    def screen_text(self, path: Path) -> str:
        with path.open("rb") as handle:
            raw = handle.read(TEXT_LIMIT + 1)
        clipped = len(raw) > TEXT_LIMIT
        text = raw[:TEXT_LIMIT].decode("utf-8", errors="replace")
        more = " (first 64 KB; the file has the rest)" if clipped else ""
        return (
            f'<details class="screen"><summary>Screen text{more}</summary>'
            f"<pre>{_e(text)}</pre></details>"
        )

    # -- the watchlist ----------------------------------------------------------------

    def watchlist(self) -> str:
        if not self.facts.watch:
            return ""
        still = sum(1 for w in self.facts.watch if w.status == "failed")
        fixed = sum(1 for w in self.facts.watch if w.status == "passed")
        items = []
        for item in self.facts.watch:
            journey = item.journey
            tone = _WATCH_TONE[item.status]
            heading = (
                f'<span class="jid finding">{_e(item.finding)}</span>'
                f"<h2>{_rich(journey.title)}</h2>"
            )
            badge = f'<span class="badge {tone}">{_e(item.watch_label)}</span>'
            meta = [_e(duration(journey.duration_s))]
            items.append(
                self._journey_section(
                    journey,
                    tone=f"witem {tone}",
                    heading=heading,
                    badge=badge,
                    meta=meta,
                    steps=item.steps,
                    watch=True,
                )
            )
        return (
            '<section class="watchlist" id="watchlist" aria-labelledby="watch-h">'
            '<header class="wl-head"><h2 id="watch-h">Watchlist '
            "<small>known bugs from the QA report</small></h2>"
            f'<span class="wl-count"><b>{still}</b> still open · <b>{fixed}</b> fixed</span>'
            f'</header><p class="story">{_e(WATCH_LEGEND)}</p>'
            f"{''.join(items)}</section>"
        )

    # -- the rest ---------------------------------------------------------------------

    def footer(self) -> str:
        logs_dir = self.run_dir / "logs"
        logs = ""
        if logs_dir.is_dir():
            names = sorted(
                p.relative_to(self.run_dir).as_posix() for p in logs_dir.rglob("*") if p.is_file()
            )
            if names:
                shown = " · ".join(
                    f'<a href="{_e(self.href(name))}">{_e(name.removeprefix("logs/"))}</a>'
                    for name in names[:40]
                )
                extra = f" · … {len(names) - 40} more" if len(names) > 40 else ""
                logs = f"<p>Logs: {shown}{extra}</p>"
        missing = ""
        if self.missing:
            listed = ", ".join(f"<code>{_e(m)}</code>" for m in sorted(set(self.missing))[:20])
            missing = (
                f'<p class="missing">Named in run.json but not in the run directory: {listed}</p>'
            )
        return (
            "<footer>"
            f"<p>Built by <code>e2e/report/build.py</code> from <code>run.json</code> "
            f"(<code>{_e(self.facts.run.schema)}</code>). {self.inlined} screenshots inlined "
            f"({human_bytes(self.inlined_bytes)}); video, screen-text and log links resolve "
            "when this page sits in <code>report/</code> of the unzipped artifact.</p>"
            f"{logs}{missing}</footer>"
        )

    @staticmethod
    def lightbox() -> str:
        return (
            '<dialog id="lightbox" class="lightbox" aria-label="Screenshot viewer">'
            # Focus lands here on open, not on the first link, which would wear a focus ring.
            '<div class="lb" tabindex="-1" autofocus>'
            '<div class="lb-top"><span class="badge lb-badge"></span>'
            '<span class="lb-caption"></span><span class="lb-count"></span>'
            '<a class="lb-file" href="#" target="_blank" rel="noopener">open PNG</a>'
            '<button type="button" class="lb-btn lb-close" aria-label="Close (Esc)">✕</button>'
            "</div>"
            '<div class="lb-stage"><figure class="term"><figcaption class="term-bar">'
            '<span class="lights" aria-hidden="true"><i></i><i></i><i></i></span>'
            '<span class="term-title lb-title"></span><span class="term-tag"></span>'
            '</figcaption><img class="lb-img" alt=""></figure></div>'
            '<div class="lb-hint"><kbd>←</kbd> <kbd>→</kbd> between screenshots · '
            "<kbd>Esc</kbd> closes · click the picture for actual pixels</div>"
            '<button type="button" class="lb-btn lb-nav lb-prev" aria-label="Previous (←)">'
            "&lsaquo;</button>"
            '<button type="button" class="lb-btn lb-nav lb-next" aria-label="Next (→)">'
            "&rsaquo;</button>"
            "</div></dialog>"
        )
