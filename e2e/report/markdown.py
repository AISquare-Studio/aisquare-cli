"""summary.md and step-summary.md: the run as GitHub-flavoured markdown.

``summary.md`` is the body of an issue or a PR comment, exactly as posted: a
heading with the verdict, one line of environment, a journeys table, what
failed, the watchlist (a table, then one collapsed ``<details>`` of evidence
per known bug), then one ``<details>`` per journey (open when it failed) with a
table of its steps, each with its screenshot and checks. ``step-summary.md`` is
the same without pictures, for the job page, whose markdown cannot reach an
artifact's files; it ends by saying where the pictures are.

Four things here exist because GitHub would otherwise get them wrong:

- **Escaping per context.** Table cells are markdown, so a ``|`` in a check
  detail would split the row and a ``*`` would start emphasis; ``<summary>``
  is raw HTML, where a backslash escape prints as a backslash. Each context gets
  its own escaper, and both neutralise ``@name`` and ``#123``: a nightly issue
  quoting "#248" from a note would add a cross-reference to issue 248 every
  night, and an ``@`` would ping someone.
- **Backticks.** Titles and checks quote commands in backticks, and a code span
  does not survive the escaping above (nor work inside HTML), so each span
  becomes an escaped ``<code>`` element instead of printing its backticks.
- **The 65 536-character limit.** GitHub refuses a longer body outright, so the
  renderer measures (in UTF-16 units, the stricter count) and sheds detail until
  it fits, one tier at a time: passed journeys first (their pictures, then their
  steps, then the section), then fixed watch items, open ones, errored journeys,
  and failed journeys last. A tier is shed completely before the next is touched,
  so a failure keeps its pictures while any passed journey still has text. A
  note says what was left out and where it is.
- **Relative pictures.** Without an image base the ``<img>`` paths are relative
  to report/, where this file is written, so a local preview shows them.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote

from e2e.contracts import Status, Step
from e2e.report.facts import (
    STATUS_EMOJI,
    VERDICT_EMOJI,
    WATCH_EMOJI,
    WATCH_LEGEND,
    Facts,
    JourneyFacts,
    Links,
    duration,
    headline,
    short_step,
)

GITHUB_LIMIT = 65_536
"""Characters GitHub accepts in an issue or comment body."""
BUDGET = 65_000
"""What the renderer aims under, leaving room for anything a poster adds."""
IMAGE_WIDTH = 720

FULL, NO_IMAGES, FAILING_ONLY, OMITTED = range(4)
"""How much of a ``<details>`` survives, from all of it to none of it."""

_MD_SPECIAL = re.compile(r"([\\`*_\[\]~|#])")
_REFERENCE = re.compile(r"([@#])(?=[A-Za-z0-9])")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")

StepRows = tuple[tuple[Step, Status], ...]


@dataclass(frozen=True)
class _Section:
    """One ``<details>``: a journey, or a watch item's evidence."""

    facts: JourneyFacts
    watch: bool

    @property
    def shed_rank(self) -> int:
        """Lower sheds first: passed journeys, then known bugs, then failures."""
        status = self.facts.status
        if self.watch:
            return 2 if status == "failed" else 1
        return {"passed": 0, "skipped": 1, "error": 3, "failed": 4}[status]


def size(text: str) -> int:
    """Length as GitHub's limit is most strictly read: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def summary(facts: Facts, *, image_base: str | None, links: Links, budget: int = BUDGET) -> str:
    """The issue / PR-comment body, screenshots included."""
    return _render(facts, images=True, image_base=image_base, links=links, budget=budget)


def step_summary(facts: Facts, *, links: Links, budget: int = BUDGET) -> str:
    """The job-page summary: no pictures, and where to find them."""
    return _render(facts, images=False, image_base=None, links=links, budget=budget)


def _render(
    facts: Facts, *, images: bool, image_base: str | None, links: Links, budget: int
) -> str:
    sections = [_Section(j, watch=False) for j in facts.journeys]
    sections += [_Section(w, watch=True) for w in facts.watch]
    renderer = _Renderer(facts, tuple(sections), images=images, image_base=image_base, links=links)
    levels = [FULL if images else NO_IMAGES] * len(sections)
    text = renderer.assemble(levels)
    if size(text) <= budget:
        return text
    for rank in sorted({section.shed_rank for section in sections}):
        tier = [i for i in reversed(range(len(sections))) if sections[i].shed_rank == rank]
        for level in (NO_IMAGES, FAILING_ONLY, OMITTED):
            for index in tier:
                if levels[index] < level:
                    levels[index] = level
                    text = renderer.assemble(levels)
                    if size(text) <= budget:
                        return text
    return _cut(text, budget, renderer.shortened_note(levels))


def _cut(text: str, budget: int, note: str) -> str:
    """The last resort, for a run whose head alone is too long: cut at a line, close up."""
    room = max(0, budget - size(note) - 64)
    kept = text[:room]
    while size(kept) > room:  # a character outside the BMP counts twice
        kept = kept[: len(kept) - (size(kept) - room)]
    if "\n" in kept:
        kept = kept[: kept.rindex("\n")]
    kept += "\n\n</details>" * max(0, kept.count("<details") - kept.count("</details>"))
    return f"{kept}\n\n{note}\n"


@dataclass
class _Renderer:
    facts: Facts
    sections: tuple[_Section, ...]
    images: bool
    image_base: str | None
    links: Links

    def assemble(self, levels: list[int]) -> str:
        parts = [self.head()]
        if any(level > (FULL if self.images else NO_IMAGES) for level in levels):
            parts.append(self.shortened_note(levels))
        kept = [
            (section, level)
            for section, level in zip(self.sections, levels, strict=True)
            if level < OMITTED
        ]
        if self.facts.watch:
            parts.append(self.watch_table())
            parts.extend(self.details(s, level) for s, level in kept if s.watch)
        parts.extend(self.details(s, level) for s, level in kept if not s.watch)
        parts.append(self.tail())
        return "\n\n".join(part for part in parts if part) + "\n"

    # -- head ---------------------------------------------------------------------------

    def head(self) -> str:
        facts = self.facts
        run = facts.run
        # An HTML comment a later poster can find its own comment by; "--" would end it.
        marker = re.sub(r"-{2,}|>", "-", f"aisquare-e2e run={run.run_id} verdict={facts.verdict}")
        title = f"## {VERDICT_EMOJI[facts.verdict]} E2E {facts.verdict}: {_md(headline(facts))}"
        commit = f"<code>{_md(run.env.commit)}</code>"
        commit_url = self.links.commit_url(run.env.commit)
        if commit_url:
            commit = f"[{commit}]({commit_url})"
        env = " · ".join(
            [
                f"<b>{_md(run.env.os)}</b>",
                f"runner <code>{_md(run.env.runner)}</code>",
                f"track <code>{_md(run.env.track)}</code>",
                f"agent {_md(run.env.agent)}",
                f"aisquare <code>{_md(run.env.aisquare)}</code>",
                f"commit {commit}",
                duration(facts.duration_s),
            ]
        )
        counts = facts.counts
        tally = (
            f"<b>{counts.passed}</b>/{counts.total} steps passed · {counts.failed} failed · "
            f"{counts.error} error · {counts.skipped} skipped"
        )
        rows = ["| Journey | Result | Steps | Time |", "| --- | --- | --- | --- |"]
        rows.extend(
            f"| <b>{_md(j.journey.id)}</b> {_md(j.journey.title)} "
            f"| {STATUS_EMOJI[j.status]} {j.status} "
            f"| {j.steps_passed}/{len(j.steps)} | {duration(j.journey.duration_s)} |"
            for j in facts.journeys
        )
        if not facts.journeys:
            rows.append("| <i>no journeys ran</i> | | | |")
        blocks = [f"<!-- {marker} -->\n{title}", f"{env}<br>\n{tally}", "\n".join(rows)]
        if facts.failures:
            shown = facts.failures[:10]
            lines = [
                f"- {VERDICT_EMOJI[f.verdict_class]} <code>{_md(f.test)}</code> "
                f"{_md(_clip(f.message, 300))}"
                for f in shown
            ]
            if len(facts.failures) > len(shown):
                lines.append(f"- … and {len(facts.failures) - len(shown)} more")
            blocks.append("<b>What failed</b>\n\n" + "\n".join(lines))
        return "\n\n".join(blocks)

    def watch_table(self) -> str:
        rows = ["| Finding | Check | State |", "| --- | --- | --- |"]
        rows.extend(
            f"| <code>{_md(w.finding)}</code> | {_md(w.journey.title)} "
            f"| {WATCH_EMOJI[w.status]} {_md(w.watch_label)} |"
            for w in self.facts.watch
        )
        return f"<b>Watchlist</b> · <sub>{_md(WATCH_LEGEND)}</sub>\n\n" + "\n".join(rows)

    # -- one journey or watch item ------------------------------------------------------

    def details(self, section: _Section, level: int) -> str:
        facts = section.facts
        journey = facts.journey
        if section.watch:
            # Collapsed: the table above says which bugs are open; this is the evidence.
            is_open = ""
            line = (
                f"<code>{_h(facts.finding)}</code> {_h(journey.title)} · "
                f"{WATCH_EMOJI[facts.status]} {_h(facts.watch_label)}"
            )
        else:
            is_open = " open" if facts.status in ("failed", "error") else ""
            line = (
                f"<b>{_h(journey.id)}</b> {_h(journey.title)} · {STATUS_EMOJI[facts.status]} "
                f"{facts.status} · {facts.steps_passed}/{len(facts.steps)} steps · "
                f"{duration(journey.duration_s)}"
            )
        body: list[str] = []
        if journey.story:
            body.append(f"> {_md(journey.story)}")
        steps = facts.steps
        # A watch item's error line repeats its failing check, which the table shows.
        if journey.error and not (section.watch and steps):
            label = "Skipped" if facts.status == "skipped" else "Error"
            body.append(f"<b>{label}:</b> <code>{_md(_clip(journey.error, 600))}</code>")
        if level >= FAILING_ONLY:
            steps = tuple((s, st) for s, st in steps if st in ("failed", "error"))
        if steps:
            body.append(self.step_table(facts, steps, with_images=self.images and level == FULL))
        elif not facts.steps:
            body.append("<i>No steps recorded.</i>")
        hidden = len(facts.steps) - len(steps)
        if hidden:
            body.append(f"<i>{hidden} passed step(s) left out for size; the gallery has them.</i>")
        return (
            f"<details{is_open}>\n<summary>{line}</summary>\n\n"
            + "\n\n".join(body)
            + "\n\n</details>"
        )

    def step_table(self, facts: JourneyFacts, steps: StepRows, *, with_images: bool) -> str:
        journey = facts.journey
        if with_images:
            rows = ["| Step | Screenshot |", "| --- | --- |"]
        else:
            rows = ["| Step | Time | Checks |", "| --- | --- | --- |"]
        for step, status in steps:
            name = (
                f"{STATUS_EMOJI[status]} <b>{_md(short_step(journey, step))}</b> "
                f"{_md(step.title)}<br><code>{_md(step.id)}</code>"
            )
            checks = "<br>".join(_check(c.name, c.ok, c.detail) for c in step.checks)
            checks = checks or "<i>no checks</i>"
            if step.note:
                checks += f"<br><i>📝 {_md(_clip(step.note, 240))}</i>"
            if with_images:
                picture = self.image(step.shot, step.id)
                rows.append(f"| {name} · {duration(step.duration_s)}<br>{checks} | {picture} |")
            else:
                rows.append(f"| {name} | {duration(step.duration_s)} | {checks} |")
        return "\n".join(rows)

    def image(self, shot: str | None, step_id: str) -> str:
        if not shot:
            return "<i>no screenshot</i>"
        path = quote(shot.replace("\\", "/").lstrip("/"), safe="/")
        base = self.image_base.rstrip("/") if self.image_base else ".."
        src = html.escape(f"{base}/{path}", quote=True)
        alt = html.escape(step_id, quote=True)
        return f'<img src="{src}" width="{IMAGE_WIDTH}" alt="{alt}">'

    # -- tail ---------------------------------------------------------------------------

    def tail(self) -> str:
        facts = self.facts
        parts: list[str] = []
        if facts.notes:
            notes = "\n".join(f"- {_md(_clip(note, 400))}" for note in facts.notes[:12])
            parts.append(f"<b>Notes</b>\n\n{notes}")
        artifact = _md(self.links.artifact_name)
        where = (
            "Every screenshot, its screen text and the videos: <code>report/gallery.html</code> "
            f"in the <code>{artifact}</code> artifact"
        )
        if self.links.run_url:
            where += f" · [workflow run]({self.links.run_url})"
        where += f" · run <code>{_md(facts.run.run_id)}</code>"
        parts.append(f"<sub>{where}</sub>")
        if not self.images:
            # The job page cannot show an artifact's pictures, so its last line says
            # where they are.
            parts.append(
                f"Screenshots: download the `{self.links.artifact_name}` artifact and open "
                "report/gallery.html"
            )
        return "\n\n".join(parts)

    def shortened_note(self, levels: list[int]) -> str:
        start = FULL if self.images else NO_IMAGES
        cut = [
            s.facts.finding if s.watch else s.facts.journey.id
            for s, level in zip(self.sections, levels, strict=True)
            if level > start
        ]
        what = ", ".join(cut) if cut else "some journeys"
        return (
            f"> ✂️ Shortened to fit GitHub's {GITHUB_LIMIT:,}-character limit: detail left out "
            f"for {_md(what)}. Everything is in <code>report/gallery.html</code> in the "
            f"<code>{_md(self.links.artifact_name)}</code> artifact."
        )


def _check(name: str, ok: bool, detail: str) -> str:
    if ok:
        return f"✓ {_md(name)}"
    return f"✗ <b>{_md(name)}</b>" + (f": {_md(_clip(detail, 240))}" if detail else "")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _md(text: str) -> str:
    """Text that renders as itself in markdown, also in a table cell; `spans` as <code>."""
    parts = _CODE_SPAN.split(text)  # odd indexes are the insides of backtick spans
    return "".join(
        f"<code>{_md_plain(part)}</code>" if index % 2 else _md_plain(part)
        for index, part in enumerate(parts)
    )


def _md_plain(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _MD_SPECIAL.sub(r"\\\1", text)
    text = _REFERENCE.sub("\\1\u2060", text)
    return text.replace("\r", "").replace("\n", "<br>")


def _h(text: str) -> str:
    """Text for raw HTML (``<summary>``), where a markdown escape would print."""
    parts = _CODE_SPAN.split(" ".join(text.split()))
    return "".join(
        f"<code>{_h_plain(part)}</code>" if index % 2 else _h_plain(part)
        for index, part in enumerate(parts)
    )


def _h_plain(text: str) -> str:
    return _REFERENCE.sub("\\1\u2060", html.escape(text, quote=True))
