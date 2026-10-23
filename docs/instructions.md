# Which instructions an agent follows

A fleet agent hears from several sources at once: Claude Code itself, your own
`CLAUDE.md` files, aisquare's briefing, and aisquare's memory. When two of them
disagree, for example a global *"use subagents for parallel work"* against the
manager's *"spawn coder agents"*, nothing ranks them. This page lists where each
instruction comes from and how to settle a conflict.

## Where each instruction comes from

In the order an agent receives them:

| # | Channel | What it carries | Where it lands |
| --- | --- | --- | --- |
| 1 | Claude Code's system prompt | Claude Code's own instructions, plus the agent's **persona** when `[persona] system_prompt = true` (the default): aisquare passes it with `--append-system-prompt-file` | the system prompt |
| 2 | Your `CLAUDE.md` files | `~/.claude/CLAUDE.md`, the project's `CLAUDE.md` and the rest of the hierarchy, which Claude Code loads itself | a user message after the system prompt |
| 3 | aisquare's `SessionStart` hook | the `<aisquare-team>` briefing: the role's standing cycle, the board, the snapshot pointer, and the persona block (shown here too when the persona also rides the system prompt) | a system reminder at session start |
| 4 | aisquare's per-prompt hook | the teammate delta (what changed on the board), and for a manager the Stop hook's wake-up reason | a system reminder at each prompt |
| 5 | aisquare's memory | entries from your user pool and the project's pool, injected with the briefing. `aisquare agents connect` copied each section of `~/.claude/CLAUDE.md` into the user pool once, so those sections can arrive a second time through this channel | inside the session-start output (3) |

Claude Code's own agent teams are off in fleet launches (`[fleet]
disable_native_agent_teams = true`), so a fleet agent's helpers are the ones
`fleet spawn` starts and the board shows.

## A conflict is removed, not ranked

Claude Code's memory docs say every discovered `CLAUDE.md` is concatenated into
context, none overriding another, and that *"if two instructions contradict each
other, Claude may pick one arbitrarily"*. The same holds between your
`CLAUDE.md` and aisquare's briefing: no channel above wins by position. So the
fix for a conflict is to remove it, not to say one rule is stronger.

The usual one is subagents. A role cycle tells a manager to delegate through the
board and `fleet spawn`, where every helper is visible, has its own worktree and
can be stopped. A global rule to use subagents for parallel work sends the same
manager down Claude Code's in-process subagents instead. Scope your rule to
sessions that are not fleet agents. A snippet for `~/.claude/CLAUDE.md`:

```text
When you are briefed as an aisquare fleet agent (an `<aisquare-team>` block),
delegate through the board and `fleet spawn` as it says; use subagents only for
read-only research inside your own task.
```

## What reaches a subagent

A Claude Code subagent gets its own system prompt plus basic environment
details. It still loads the `CLAUDE.md` hierarchy, but it gets **none** of the
session's hook context: no `<aisquare-team>` briefing, no board, no delta. A
rule that must reach a subagent belongs in `CLAUDE.md`, not in a persona or the
board.

## Finding and clearing conflicts

- **In Claude Code:** `/doctor prompt-audit` (Claude Code 2.1.283 or later)
  reads the instruction files a session loads and names the ones that
  contradict each other.
- **In aisquare's memory:** the sections `aisquare agents connect` copied from
  `~/.claude/CLAUDE.md` are user-pool entries tagged with the agent's name
  (`claude-code`). Edit `CLAUDE.md` first: connecting again adds back every
  section the pool does not already hold. Then list the pool and remove the
  stale entry:

```sh
aisquare context list
aisquare context remove a3f2
aisquare context preview      # exactly what agents will be shown now
```

- **A persona:** a persona is a role's voice, not a second set of rules.
  `aisquare persona show <name>` prints what an agent is briefed with
  ([Personas](personas.md)).

## The size of the briefing

Claude Code caps a hook's output at **10,000 characters**. Over the cap, Claude
receives only a file path and a preview of the first 2,000 characters, which is
less than one manager cycle. The persona's body is part of that briefing
(channel 3) even when it also rides the system prompt (channel 1), so it counts
against the cap with the role cycle, the board and the memory entries: keep a
persona short.

## Further reading

- Claude Code, [How Claude remembers your project](https://code.claude.com/docs/en/memory)
- Claude Code, [Hooks](https://code.claude.com/docs/en/hooks): the 10,000-character cap
- Claude Code, [Subagents](https://code.claude.com/docs/en/sub-agents)
- Claude Code, [CLI reference](https://code.claude.com/docs/en/cli-reference): `--append-system-prompt-file` appends; `--system-prompt` replaces the default prompt and drops its tool guidance
- [Orchestration](orchestration.md) · [Personas](personas.md) · [Memory](memory.md)
