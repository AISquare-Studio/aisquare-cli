# Cross-account resume and shared memory: the check

What `aisquare fleet switch` relies on when it moves an agent to another Claude
account (`[accounts] on_limit = "switch"`), and what `[accounts] share =
["memory"]` relies on, checked against a real Claude Code (P2-W7, #198).

**Last run:** 2026-10-23, Claude Code 2.1.309, macOS, accounts 2 and 4: both pass.

## With the test

Two signed-in Claude config directories, a few short `claude -p` turns (about
$0.10 in all), the project folders it made removed afterwards:

```text
AISQUARE_E2E_CLAUDE_A=~/.aisquare/claude-accounts/2 \
AISQUARE_E2E_CLAUDE_B=~/.aisquare/claude-accounts/4 \
pytest -m e2e tests/test_cross_account_e2e.py
```

## By hand

In a scratch git repository, with `A` and `B` two signed-in config directories and
`S` a fresh UUID:

```text
CLAUDE_CONFIG_DIR=$A claude -p "Remember the marker ZEBRA-7. Reply OK." --session-id $S
CLAUDE_CONFIG_DIR=$B claude -p "What marker did I ask you to remember?" \
  --resume $A/projects/<project key>/$S.jsonl
```

Expect: the second answer is `ZEBRA-7`, its `session_id` is `$S`, the new turns
are appended to `$A/projects/<project key>/$S.jsonl`, and `$B/projects/<project
key>/` holds no `$S.jsonl`. The project key is the folder's path with every
character but a letter or a digit a `-`.

Shared memory, with `M` a directory outside any `.claude` path:

```text
CLAUDE_CONFIG_DIR=$A claude -p "Save to your auto memory: the deploy codeword is PELICAN-42." \
  --settings '{"autoMemoryDirectory": "'$M'"}' --allowedTools Read,Write,Edit
CLAUDE_CONFIG_DIR=$B claude -p "What is the deploy codeword? Use your memory." \
  --settings '{"autoMemoryDirectory": "'$M'"}' --allowedTools Read
```

Expect: `$M` holds `MEMORY.md` and a topic file naming the codeword, and the
second answer is `PELICAN-42`; the same question on `B` without `--settings` does
not know it.

If a resume fails on another build, `aisquare fleet switch <label> --fresh`
starts a new session with a hand-off prompt instead.
