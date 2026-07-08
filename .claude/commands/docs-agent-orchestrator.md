---
description: Rename "Team bus" → "Agent Orchestrator" across the docs and sync them with the v0.2.0 README
---

# Docs update: Team bus → Agent Orchestrator

We renamed the multi-session coordination feature. **"Team bus" is retired
everywhere**; the feature is now the **Agent Orchestrator**. The
aisquare-cli repo (README, CHANGELOG, code strings, release title) has
already been updated — the docs in this repo still use the old term. Your
job is to bring the docs in line.

## Source of truth

Pull the latest `main` of `AISquare-Studio/aisquare-cli` and treat its
`README.md` as canonical for terminology, feature framing, command
examples, and the board-key table. The CHANGELOG's `[0.2.0]` section has
the release framing. Do not invent behavior not present there.

## Vocabulary rules

| Old | New | Notes |
| --- | --- | --- |
| Team bus / team-bus | **Agent Orchestrator** | as the feature name; "the orchestrator" as the short noun mid-sentence |
| "the bus" (shared state being read/written) | "the board" | e.g. "every session on the board" |
| "bus cursor" | "stream cursor" | the event-stream `seq` |
| section headings like "Team bus" | "Orchestrate a team of agents" (or "Agent Orchestrator") | match the README's heading style |

**Unchanged — do NOT rename these** (shipped API):

- CLI commands: `aisquare team …`, `aisquare task …`, `note`, `board`, `recall`, `serve`
- Env vars: `AISQUARE_TEAM`, `AISQUARE_ROLE`, `AISQUARE_TEAM_HUB`, `AISQUARE_TEAM_DELTA`, `AISQUARE_TEAM_LEASE_MIN`, `AISQUARE_BRAIN*`
- Words "team" and "teammate" by themselves are fine — only "bus" is retired

## What to do

1. Inventory first: `grep -rni "team bus\|team-bus\|teambus" .` plus a
   word-bounded pass for standalone `bus` (`grep -rwn -i "bus" .`) —
   judge each standalone hit in context (some may be unrelated).
2. Apply the vocabulary rules. Reword sentences where a straight
   substitution reads badly — these are docs, they should read well.
3. Fix structural fallout: page titles, nav labels, slugs/anchors
   (e.g. `#team-bus` links), image alt text, and any cross-links into the
   aisquare-cli README's old anchor (`#team-bus-…` → the new
   `#orchestrate-a-team-of-agents` section).
4. Command examples: verify against the v0.2.0 README rather than
   rewriting from memory — the commands themselves did not change.
5. Verify: re-run the greps from step 1 (expect zero feature-name hits;
   justify any standalone `bus` you deliberately kept), and build the
   docs site if this repo has a build step.
6. Report: list every file touched, any ambiguous case you decided (with
   your reasoning), and anything you deliberately left for a human.

Do not commit — leave the changes for review.
