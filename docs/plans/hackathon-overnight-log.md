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

## Morning audit — questions and calls for the owner

_(appended as they arise; each with the manager's interim decision)_

