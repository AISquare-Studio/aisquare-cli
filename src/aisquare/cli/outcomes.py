"""``aisquare outcomes``: one week of what the agents achieved and who steered them, as text or
JSON (``services.outcomes`` measures it)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

import typer

from aisquare.cli import usage_format as fmt
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.models import OutcomeDelta, WeeklyOutcomes
from aisquare.services import outcomes as outcomes_service

_WEEK_HELP = (
    "The week: 'last', or an ISO week such as 2026-W43. Default: this week, Monday 00:00 local "
    "time to now."
)
_GH_HELP = "Count pull requests with gh, the only network call (skipped, and said so, without gh)."

_LABELS = {
    "prs_merged": "PRs merged",
    "commits": "commits",
    "tasks_done": "tasks done",
    "agent_hours": "agent-hours",
    "turns": "turns",
    "cost_usd": "list-price cost",
    "human_share": "human-started turns",
    "agent_minutes_per_human_prompt": "agent-minutes / human prompt",
    "interrupts": "interrupts",
}


def outcomes(
    week: Annotated[str | None, typer.Option("--week", "-w", help=_WEEK_HELP)] = None,
    gh: Annotated[bool, typer.Option("--gh/--no-gh", help=_GH_HELP)] = True,
) -> None:
    """What your agents achieved in one week, and how much of it you steered.

    One ISO week on the local clock, against the week before (to the same
    point while this one runs). No day, month or all-time view, by design.
    --json gives the same figures. How each is measured:

    OUTCOMES, per repository (its remote's name, else its folder's)
      PRs          gh: PRs you authored (your agents open them as you),
                   merged or opened in the week. One GraphQL call;
                   skipped, and said so, without gh.
      commits      git log of the branches and tags of the checkouts the
                   agents worked in, by your user.email, authored in the
                   week. Merges and commits GitHub made are left out; a
                   rebased commit counts once. Lines: --numstat.
      tasks        board tasks marked done, and reopened, in the week.
    EFFORT
      agent-hours  turn durations summed, UserPromptSubmit to Stop; a turn
                   with no Stop ends at an interrupt or the next turn.
      sessions, turns, tool calls, tokens, cost: as metrics stats counts.
      files edited distinct files an Edit, Write or NotebookEdit named.
      cost per PR  the week's list-price cost / PRs merged (or / tasks).
    DIRECTION, who steered (never human tokens against agent tokens)
      turn origin  who authored the prompt that started each turn. Human:
                   typed it (slash commands too), or ran the fleet tell or
                   spawn (terminal, asq, phone). Agent: a manager's tell or
                   spawn, a watch wake, a Stop-hook continuation, a
                   compaction line, a task notification. Recorded at
                   UserPromptSubmit; older turns are inferred, and said so.
      leverage     agent-minutes and tool calls per human prompt.
      interventions  interrupts ("[Request interrupted by user]") and
                   tool uses denied at their permission prompt.
      session mode directive: at most 1 human prompt and no interrupt;
                   guided: 2-3; collaborative: 4+ (after Anthropic's
                   Economic Index, directive vs iterative use).
    VS LAST WEEK   the headline figures and their change.
    """
    now = datetime.now().astimezone()
    try:
        result = outcomes_service.week_report(week, now=now, use_gh=gh)
    except outcomes_service.WeekError as exc:
        raise typer.BadParameter(str(exc), param_hint="--week") from exc
    if get_state().json_output:
        typer.echo(json.dumps(outcomes_service.outcomes_json(result)))
        return
    for line in render_outcomes(result):
        stdout_console().print(line, markup=False, highlight=False)


# --- the text ------------------------------------------------------------------------------------


def _clock_text(moment: datetime) -> str:
    """``moment`` on the clock it was read on (the report's local one): ``Mon 26 Oct`` at
    midnight, ``Fri 30 Oct 22:40`` otherwise."""
    return moment.strftime("%a %d %b %H:%M").replace(" 00:00", "")


def _span_text(since: datetime, until: datetime, *, running: bool) -> str:
    end = f"now, {_clock_text(until)}" if running else _clock_text(until)
    return f"{_clock_text(since)} → {end}"


def _count_text(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _ratio_text(value: float | None, unit: str = "") -> str:
    return "—" if value is None else f"{value:,.1f}{unit}"


def render_outcomes(result: WeeklyOutcomes) -> list[str]:
    """The report as the lines ``aisquare outcomes`` prints."""
    if result.empty:
        return [f"Nothing recorded in {result.week}: no agent turn, no usage, no board task moved."]
    span = _span_text(result.since, result.until, running=result.in_progress)
    lines = [f"Outcomes — {result.week} ({span})", ""]
    lines += _outcomes_lines(result)
    lines += ["", *_effort_lines(result)]
    lines += ["", *_direction_lines(result)]
    lines += ["", *_delta_lines(result)]
    lines += ["", "How each figure is measured: aisquare outcomes --help"]
    return lines


def _outcomes_lines(result: WeeklyOutcomes) -> list[str]:
    o = result.outcomes
    lines = ["OUTCOMES"]
    if o.prs_merged is None and o.prs_opened is None:
        lines.append(f"  pull requests   {o.prs_note or 'not counted'}")
    else:
        prs = f"{_count_text(o.prs_merged)} merged · {_count_text(o.prs_opened)} opened"
        lines.append(f"  pull requests   {prs}" + (f"  ({o.prs_note})" if o.prs_note else ""))
    lines.append(
        f"  commits         {o.commits:,}  (+{o.lines_added:,} -{o.lines_deleted:,} lines)"
    )
    lines.append(f"  board tasks     {o.tasks_done:,} done · {o.tasks_reopened:,} reopened")
    if o.repos:
        width = max(10, *(len(r.name) for r in o.repos))
        lines.append(
            f"  {'repository':<{width}}  {'merged':>6} {'opened':>6} {'commits':>7} "
            f"{'+lines':>8} {'-lines':>8} {'done':>5} {'reopened':>8}"
        )
        for r in o.repos:
            lines.append(
                f"  {r.name:<{width}}  "
                f"{_count_text(r.prs_merged):>6} {_count_text(r.prs_opened):>6} "
                f"{r.commits:>7,} {r.lines_added:>8,} {r.lines_deleted:>8,} "
                f"{r.tasks_done:>5,} {r.tasks_reopened:>8,}"
            )
    return lines


def _effort_lines(result: WeeklyOutcomes) -> list[str]:
    e = result.effort
    timed = f"{e.timed_turns:,} of {e.turns:,} turns timed"
    if e.untimed_turns:
        timed += f"; {e.untimed_turns:,} with no end recorded"
    return [
        "EFFORT",
        f"  agent-hours          {e.agent_hours:,.1f}  ({timed})",
        f"  sessions             {e.sessions:,}",
        f"  turns                {e.turns:,}",
        f"  tool calls           {e.tool_calls:,}",
        f"  files edited         {e.files_edited:,}",
        f"  tokens               {fmt.tokens(e.tokens.total)}"
        f"  (input + output {fmt.tokens(e.tokens.input_output)})",
        f"  list-price cost      {fmt.money(e.cost_usd)}  (prices as of {result.prices_as_of})",
        "  cost per merged PR   "
        + (fmt.money(e.cost_per_merged_pr) if e.cost_per_merged_pr is not None else "—"),
        "  cost per done task   "
        + (fmt.money(e.cost_per_done_task) if e.cost_per_done_task is not None else "—"),
    ]


def _direction_lines(result: WeeklyOutcomes) -> list[str]:
    d = result.direction
    lines = ["DIRECTION — who steered"]
    if not d.turns:
        return [*lines, "  no turn recorded"]
    share = d.human_share or 0.0
    origin = (
        f"  turn origin     {fmt.percent(share)} human ({d.human_turns:,}) · "
        f"{fmt.percent(1 - share)} agent ({d.agent_turns:,}) of {d.turns:,} turns"
    )
    lines.append(origin)
    for by in ("human", "agent"):
        kinds = [(k.partition(":")[2], n) for k, n in d.by_origin.items() if k.startswith(f"{by}:")]
        if kinds:
            said = " · ".join(f"{kind} {n:,}" for kind, n in kinds)
            lines.append(f"                  {by}: {said}")
    if d.inferred_turns:
        lines.append(
            f"                  {d.inferred_turns:,} of them inferred (no origin recorded: "
            "older turns, or a session no hook saw)"
        )
    lines.append(
        f"  leverage        {_ratio_text(d.agent_minutes_per_human_prompt)} agent-minutes and "
        f"{_ratio_text(d.tool_calls_per_human_prompt)} tool calls per human prompt"
    )
    lines.append(
        f"  interventions   {d.interrupts:,} interrupts · {d.denied_permissions:,} tool uses denied"
    )
    if d.sessions:
        parts = [
            f"{fmt.percent(n / d.sessions)} {name} ({n:,})"
            for name, n in (
                ("directive", d.directive),
                ("guided", d.guided),
                ("collaborative", d.collaborative),
            )
        ]
        lines.append(f"  session mode    {' · '.join(parts)} of {d.sessions:,} sessions")
    return lines


def _delta_value(name: str, value: float | None) -> str:
    if value is None:
        return "—"
    if name == "cost_usd":
        return fmt.money(value)
    if name == "human_share":
        return fmt.percent(value)
    if name in ("agent_hours", "agent_minutes_per_human_prompt"):
        return f"{value:,.1f}"
    return f"{int(value):,}"


def _change_text(delta: OutcomeDelta) -> str:
    if delta.change is None:
        return ""
    if delta.name == "human_share":
        return f"{delta.change * 100:+.0f} pts"
    sign = "+" if delta.change > 0 else "-" if delta.change < 0 else "±"
    magnitude = _delta_value(delta.name, abs(delta.change))
    pct = f" ({delta.change_pct * 100:+.0f}%)" if delta.change_pct is not None else ""
    return f"{sign}{magnitude}{pct}"


def _delta_lines(result: WeeklyOutcomes) -> list[str]:
    span = ""
    if result.compared_since is not None and result.compared_until is not None:
        to = " to the same point" if result.in_progress else ""
        span = f" ({_span_text(result.compared_since, result.compared_until, running=False)}{to})"
    lines = [f"VS LAST WEEK — {result.compared_with}{span}"]
    for delta in result.vs_last_week:
        label = _LABELS.get(delta.name, delta.name)
        lines.append(
            f"  {label:<30} {_delta_value(delta.name, delta.this_week):>10}  "
            f"vs {_delta_value(delta.name, delta.last_week):>10}  {_change_text(delta)}"
        )
    return lines
