# Hackathon overnight log — decisions for the morning audit

> Manager session `8e92f6af`, fleet `solar-sparrow`, branch `rc/hackathon-v1`.
> The owner authorised, on 2026-09-15 evening: *"drive this to completion
> overnight … you are allowed to do anything on the rc branch, don't touch
> main, don't touch infra."* The owner audits this log in the morning, then
> merges to `main` and deploys. Every entry: when, what, why, evidence.
> Questions the owner must answer are collected at the bottom.

## Operating rules for the night

1. **Scope.** Anything on `rc/hackathon-v1`. Never `main`. Never infra: no
   server, account, CI-config or tmux-server changes. Docs commits by the
   manager are limited to `docs/plans/*` and go straight to the branch.
2. **Merge gate.** A PR merges into `rc/hackathon-v1` only when all three hold:
   the runner (`runner2-1`) marked the task **done with evidence**; CI is
   **green** on the PR (`.github/workflows/ci.yml` runs on PRs to `rc/**`); the
   manager's own **read-only review** found nothing blocking. Merge commit
   (`gh pr merge <n> --merge`), never squash on a shared branch, never
   force-push. The merge is logged here with the PR, the SHA and the evidence
   cited.
3. **Dependencies.** A task is told to its coder the minute its `needs` are
   merged into `rc/hackathon-v1`, not merely in review.
4. **Reopen policy.** Exact reason and repro, never "please fix". A task that
   bounces twice is re-specified or split by the manager.
5. **Owner calls.** Decided now with a stated assumption and a recommendation,
   logged under *Morning audit*; nothing waits on the owner overnight unless it
   is genuinely unsafe to proceed.
6. **Motivation is part of the job.** Every landed PR gets a specific cheer on
   the board; every reopen carries "you've got this".

## Fleet

| Label | Seat | Plays | Queue |
| --- | --- | --- | --- |
| `coder3a-1` | coder3a · `.claude3` · Opus 5 | coder-persona-core | P1 → P2 → P5 → P8 |
| `coder3b-1` | coder3b · `.claude3` · Opus 5 | coder-spawn-dialog | P3 → P6 → P4 → P7 |
| `runner2-1` | runner2 · `.claude2` · Opus 5 | the runner | verifies every review |
| `manager` | 8e92f6af · Fable 5.1 | manager | contracts, sequencing, merges into rc |

Cap is four agents per project; reviewer and validator lanes are the manager's,
read-only.

## Timeline

| When (local) | Event | Decision / why | Evidence |
| --- | --- | --- | --- |
| 2026-09-15 evening | Owner authorises overnight autonomy on `rc/hackathon-v1`; no `main`, no infra. | Manager loop re-scheduled with the merge policy above; this log created. | `docs/plans/hackathon-loop-prompts.md`, cron job re-created |
| 2026-09-15 evening | `coder3a-1` claimed P1 `tsk_01m2hmms453d9veehg44p5xzx0`; `coder3b-1` claimed P3 `tsk_01m2hkecm1tgegphnqa9kt398s`. | As assigned. | board events |
| 2026-09-15 evening | `runner2-1` in NEEDS YOU state with nothing in review. | Pane read; see next entry. | `tmux -L asqui capture-pane -t %4` |
| 2026-09-15 evening | `runner2-1`'s pane shows Claude Code's one-time "Teach auto mode about your environment?" dialog (options 1/2/3). | Direct keystrokes into a pane (`tmux send-keys`) are denied to the manager by the auto-mode classifier; `fleet tell` refuses an *attention* pane and files a note instead. Decision: observe — its own loop job `b3e8f801` may still fire; nothing is in review for hours. Plan B if it is still stuck when the first review lands: `fleet stop runner2-1`, respawn the bound seat (`fleet spawn runner2 --label runner2-1`), and re-deliver the runner prompt from `docs/plans/hackathon-loop-prompts.md` with `fleet tell` once it is waiting. | pane capture; `fleet tell` receipt "it is attention — filed as board note #6953" |
| 2026-09-15 evening | `make check` from the ROOT checkout fails one test: `tests/test_documented_commands.py::test_the_document_list_has_not_gone_stale` sweeps every markdown file under the repo and finds the coders' worktrees under `.aisquare-worktrees/`. | Not a defect in any PR: inside a worktree, in the runner's verify worktree and in CI (clean checkout) it passes. Filed **P9** `tsk_01m2hrgbsf3s0dwcx11mjap123` (S): the sweep skips the configured `worktree_dir` and nested worktrees; assigned to whichever coder frees up first. | pytest output at fd487d5 |
| 2026-09-15 evening | Both coders deep in their first tasks (`coder3a-1` on P1 in `.aisquare-worktrees/p1-persona-core`, `coder3b-1` on P3 in `.aisquare-worktrees/p3-spawn-dialog`), exploring the code before writing. | No action; they were told their queues. | pane captures |

## Morning audit — questions and calls for the owner

_(appended as they arise; each with the manager's interim decision)_

1. **Runner onboarding dialog.** `runner2-1`'s pane may still show Claude
   Code's "Teach auto mode about your environment?" prompt in the morning. The
   manager cannot press a key in an agent's pane (classifier denies
   `tmux send-keys`). Recommendation: press `3` (Don't show again) in that pane,
   or add a Bash permission rule for `tmux -L asqui send-keys` so the manager
   can clear such dialogs itself. Interim: observe; plan B (respawn + re-tell)
   only if a review is waiting on the runner.
2. **Doc guard vs. fleet worktrees (P9).** A root-checkout `make check` fails
   while agent worktrees exist. Interim decision: filed P9 (small), does not
   gate any PR because CI and the worktrees pass. Your call: merge P9 into rc
   with the rest, or leave for after the hackathon.

