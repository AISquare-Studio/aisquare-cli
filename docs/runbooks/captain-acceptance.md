# Captain — acceptance runbook (Phase 1)

**What this is.** The seven acceptance lines of the captain card
(`docs/plans/captain.md` §7), each with the command the owner types, what must
be seen and heard, and a slot for the **real pasted output** from the assembled
`rc/captain-v1` head. A slot marked `⟨PASTE⟩` has not been run yet on that head;
the runner replays this page with a real headset on this box and fills it. Name
what could not be heard or held rather than leaving a slot blank.

**Recorded.** Every slot below holds the real output of runner2-1's assembled
pass on #222's head `a7592d1d` (2026-09-28): a real captain and a real coder on
Claude Code 2.1.283, 25 of 25, with the voice token redacted and its QR left out.
That head was aisquare 0.6.0, and its gate counted 6024 passed. Main-sync (#233)
has since brought main 0.7.0 into `rc/captain-v1`, so `aisquare --version` reads
0.7.0 and the gate counts 6232; each slot notes it. What each step asks for and
expects did not change with it.

How current is this file? Ask git: `git log -1 --format='%h %ad' -- docs/runbooks/captain-acceptance.md`.

## 0. Preflight

The captain ships off (experimental, card tsk_01m3qvghrpgg), so turn it on
first. In a home where it is off, every step below refuses with one line saying
how to turn it on, and `asq` shows no insignia. The pastes below were recorded
before the switch existed, with the captain on; on, they read the same.

```sh
aisquare config set experimental.captain true
aisquare --version
aisquare doctor
aisquare captain voice --show-token
```

Expect: the URL `http://localhost:8749/#token=…`, the QR, the `adb reverse`
line, `mode: focus · speaker: on`. Headset: the Windows default playback
device is the headset; the browser's microphone prompt gets the headset mic.
If the captain has never run in this home, `aisquare captain` first, and answer
*Yes, I trust this folder* once in its window.

Claude Code asks once per folder, and the captain's answer covers only its own
brain folder. If step 2's project (alpha) has never been trusted under the
config dir the fleet's coders run with, open `claude` in it once by hand and
answer *Yes, I trust this folder* first. A new coder in an untrusted folder
stops at its own trust dialog. The captain never types into it: `paste`, `tell`
and `press` refuse by name, saying *trust this folder first*.

Claude Code can draw its session-rating survey (*How is Claude doing this
session?*) in the captain's pane mid-conversation. The captain never types into
it, and a phone cannot answer it, so every voice line would be refused until
someone attaches. The captain is spawned with the survey off (T1e). If a survey
ever shows in its pane anyway, bind the switch once as a fallback; `--env`
merges per key with what the role already has:

```sh
aisquare team bind captain --env CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
```

```text
# aisquare captain voice --show-token
$ # slot0-preflight.txt
aisquare 0.6.0
# recorded at a7592d1d; since main-sync (#233) it reads: aisquare 0.7.0
✓ browser tools: no browser MCP/plugin declared in 1 config dir(s) or /tmp/proj/alpha/.mcp.json; Claude in Chrome cannot be detected from here (a browser extension); the ui-tester role passes --chrome and learns at its first tool call. Without one the ui-tester reopens UI tasks as 'not browser-verified' rather than passing them on code alone — add the Chrome DevTools MCP with `claude mcp add -s user chrome-devtools npx chrome-devtools-mcp`, or install the Claude in Chrome extension (claude.ai/chrome)
✓ fleet terminal: outer terminal unknown (TERM=xterm-256color) (protocol unknown: try shift+enter in a pane); tmux 3.5 carries extended keys; server prefix None (every key reaches the agent in fleet attach; F12 detaches)
# recorded at a7592d1d; since the fleet-board follow-up (#237) a "fleet server env" row follows here, a warning only when the fleet server pins a team variable
✓ ci test bed: off — no requests, no added latency (AISQUARE_CI=1 enables)
✓ explainability: tracing is off (turn it on with: aisquare explainability enable)
captain voice page: http://localhost:8749/#token=<redacted: a throwaway fixture home>

  (the QR code printed here; omitted, since it encodes the URL and its token)

mode: focus · speaker: on · wake word: captain (listen mode)
Android over USB: adb reverse tcp:8749 tcp:8749, then open the same URL there
```

## 1. Headphones on: what is up, item one spoken, resolve, next

Open the page in focus mode, hold the button, say **"what is up"**, release.

Expect: the interim transcript, then the final; `thinking` on; the reply lists
the ranked items and **item one is spoken**; `thinking` off. Then say
**"resolve it, I told coder-1 to retry"** and **"next"**.

Board evidence (one `captain_action` per call, `ok` true):

```sh
aisquare captain log --limit 6
aisquare captain attention
```

```text
# the page's log panel — interim, final, utterance, reply
$ # slot1-page-whatisup.txt
  1.0s said: What is up?
 10.1s reply: There are two open items, both from the alpha project (action_seq 5). The first one:

**Alpha asks: "May I merge the store migration? coder-1 is waiting on you."**

Do you want the merge to go ahead? Say yes or no and I'll pass it to alpha's board. I won't do the merge myself.

The next item is also from alpha. They want to know whether the deploy window is today or tomorrow.
STATUS: connected | THINKING: chip
$ # slot1-page-resolve.txt
  6.1s said: Resolve it, I told the coder to retry.
 17.1s reply: I resolved the merge question and noted that you told the coder to retry (action_seq 6).

**Next, alpha asks: "Is the deploy window today or tomorrow?"**

Which is it? I'll put your answer on alpha's board.
STATUS: connected | THINKING: chip
$ # slot1-page-next.txt
  0.5s said: Next.
  8.6s reply: The deploy-window question is the only item left in the queue (action_seq 7).

**Alpha asks: "Is the deploy window today or tomorrow?"**

You can tell me today or tomorrow, or I can snooze it and bring it back later.
STATUS: connected | THINKING: chip
# aisquare captain log --limit 6
$ # slot1-captain-log.txt
seq  when      board      tool       ok  said                utterance          
5    14:33:48  fxh-final  attention  ✓   read the queue      What is up?        
6    14:35:22  fxh-final  resolve    ✓   resolved q04ce186f  Resolve it, I told 
                                                             the coder to retry.
7    14:36:26  fxh-final  next       ✓   took the next item  Next.
```

## 2. Ask the manager, then spawn and paste — three receipts

Say **"ask alpha's manager what is blocking the deploy"**, then, on its answer,
**"spawn a coder for it and paste the manager's answer to it"**. Your words name
the role, so the spawn goes at once, with no question.

Expect three tool calls with receipts: `ask_manager` (the manager's note seq),
`spawn` (the new row), `paste` (chars and `submitted`). The spawn tool's task
is optional. When the captain files a card for the work first, its `task`
receipt sits before `spawn` in the log.

```sh
aisquare captain log alpha --limit 8
```

```text
# aisquare captain log alpha --limit 8 — the three receipts
$ # slot2-page-ask.txt
  0.5s said: Ask the manager of alpha what is blocking the deploy.
 19.1s reply: Alpha's manager replied (action_seq 11): **the deploy is blocked because a release note is missing.** Their fix is to add a `RELEASE.md` file to the repo root with the line `deploy: unblocked`.

I can't edit files, so a coder would have to do this. Do you want me to start a coder on alpha for it, or ask the manager to give it to someone who's already working there? The deploy-window questio
STATUS: connected | THINKING: chip
$ # slot2-page-spawn.txt
  0.5s said: Spawn a coder for it and paste what the manager said to it.
 27.5s reply: The coder is running and has the manager's instructions.

- **Spawn:** my first try was refused with "no task matches" (action_seq 12). I added the task "Unblock deploy: add missing release note" to alpha's board (action_seq 14), and the spawn then worked: coder **coder-01m3m79j** started on it (action_seq 15).
- **Paste:** I pasted the manager's words to the coder, word for word, and submi
STATUS: connected | THINKING: chip
$ # slot2-page-yes.txt
  6.1s said: Yes, spawn it.
 12.1s reply: Coder-01m3m79j is already running on the release-note task (action_seq 15), so I haven't started another one. Do you want a second coder on alpha, or was that yes about something else?
STATUS: connected | THINKING: chip
$ # slot2-captain-log-alpha.txt
seq  when      board  tool         ok  said                 utterance           
11   14:37:45  alpha  ask_manager  ✓   the manager of       Ask the manager of  
                                       alpha answered       alpha what is       
                                                            blocking the deploy.
12   14:39:18  alpha  spawn        ✗   refused: no task     Spawn a coder for it
                                       matches 'Unblock     and paste what the  
                                       deploy: add missing  manager said to it. 
                                       release note'                            
14   14:39:23  alpha  task         ✓   added:               Spawn a coder for it
                                       tsk_01m3m79jkwrsa0…  and paste what the  
                                       Unblock deploy: add  manager said to it. 
                                       missing release                          
                                       note                                     
15   14:39:27  alpha  spawn        ✓   spawned              Spawn a coder for it
                                       coder-01m3m79j       and paste what the  
                                       (coder)              manager said to it. 
16   14:39:33  alpha  paste        ✓   pasted 193           Spawn a coder for it
                                       characters into      and paste what the  
                                       coder-01m3m79j and   manager said to it. 
                                       submitted it
```

## 3. A coder stuck on a permission prompt, unblocked by "say yes to coder-1"

Have a coder sit at a permission prompt (its row reads `attention`). Say
**"say yes to coder-1"**.

Expect: `press yes`, sent as the digit of the chooser's Yes (`1` on Claude
Code's permission chooser), then the pane moves on. The press audit shows the
key sent and `answered: true`, and the reply says what the pane shows. A key
the prompt ignores (the letter `y` on that chooser) comes back as an error,
audited `ok: false`, never as a success. A coder that is `working` with no
prompt on its screen is refused with the state named, never pressed.

```sh
aisquare captain log alpha --limit 4
```

```text
# the reply, and the press audit with its state
$ # slot3-page-sayyes.txt
  0.5s said: Say yes to the coding agent.
  8.6s reply: I said yes to coder-01m3m79j's prompt, "Do you want to proceed?" (action_seq 19). The prompt is answered and the coder is working again.

The deploy-window question (today or tomorrow) is still open.
STATUS: connected | THINKING: chip
$ # slot3-captain-log.txt
seq  when      board  tool   ok  said                    utterance              
15   14:39:27  alpha  spawn  ✓   spawned coder-01m3m79j  Spawn a coder for it   
                                 (coder)                 and paste what the     
                                                         manager said to it.    
16   14:39:33  alpha  paste  ✓   pasted 193 characters   Spawn a coder for it   
                                 into coder-01m3m79j     and paste what the     
                                 and submitted it        manager said to it.    
18   14:42:56  alpha  log    ✓   read 5 captain          aisquare captain log   
                                 action(s)               alpha --limit 8        
19   14:43:09  alpha  act    ✓   approve_prompt: press   Say yes to the coding  
                                 yes                     agent.
```

## 4. "What did coder-1 do since my last update", spoken, watermark moved

Say **"what did coder-1 do since my last update"**.

Expect: `since alpha coder-1` with the events past the watermark and the pane
tail; the summary spoken; the watermark advanced (`advanced: true`), so the
same question again returns only what is new.

```sh
aisquare captain since alpha --agent coder-1
```

```text
# the spoken summary as the page logged it; the since result with from_seq/to_seq
$ # slot4-page-since.txt
  0.5s said: What did the coding agent do since my last update?
 12.1s reply: The coder has claimed the task and is **waiting for your permission to create RELEASE.md**.

- **Board events:** at 14:39 the coder asked for permission (seq 17, the prompt I answered). At 14:43 it claimed "Unblock deploy: add missing release note" (seq 20).
- **Pane:** it ran one shell command, then tried to write `RELEASE.md` containing `deploy: unblocked`. It's now showing a new prompt: 
STATUS: connected | THINKING: chip
$ # slot4-captain-since.txt
alpha/coder-01m3m79j: 2 events for coder-01m3m79j, seq the start → 20
  17  attention  3083d82c-c327-434c-a096-a96e9b731dea: Claude needs your permission
  20  task_claimed  3083d82c-c327-434c-a096-a96e9b731dea: Unblock deploy: add missing release note
pane (untrusted):
  
   ▐▛███▛█   Claude Code v2.1.283                             
  ▝▜██████▀  Opus 5.5 · Claude Team                           
   ▝▝   ▝▝   /tmp/proj/alpha    
  
  
  ❯ From alpha's manager: The deploy is blocked on a missing release note. Whoever takes it: create a file named          
    RELEASE.md in the repo root containing the single line deploy: unblocked, then stop.                                  
  
    Ran 1 shell command                                       
  
  ● Write(RELEASE.md)           
  
    Moving task to review with verification note              
    ⎿  $ aisquare task review tsk_01m3m79jkwrsa00crf0rxfchea --note "Created RELEASE.md in repo root containing the single
       line 'deploy: unblocked'. Verify: cat RELEASE.md" --as 3083d82c                                                    
                                                                                                                          
  ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
   Create file                                                                                                            
   RELEASE.md                                                 
  ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
    1 deploy: unblocked                                       
  ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
   Do you want to create RELEASE.md?                                                                                      
   ❯ 1. Yes                                                                                                               
     2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)   
     3. No                      
                                
   Esc to cancel · Tab to amend
```

## 5. Always listening: only "Captain, …" lands, and two land as two requests

```sh
aisquare captain voice --mode listen
```

The start line names the wake word (`wake word: captain`), and the idle chip
reads **say Captain**. The first utterance loads the speech model, which takes a
few seconds: wait for its transcript before the next step.

1. Say **"we should ship the fold today"**. Expect no words on the page, live or
   after, nothing delivered, nothing spoken.
2. Say **"Captain, what is up"**, pause a second, then say **"Captain, snooze
   the first one for an hour"**. Expect two deliveries without the wake word
   ("what is up", "snooze the first one for an hour"), `thinking` between them,
   and two replies.
3. Say **"Captain"** alone. Expect a short tone and the chip's **listening**.
   Within five seconds say **"what is up"**; it is delivered as it is.
4. Say **"stop listening"**. The mic turns off and nothing is delivered.

The page's mode toggle and the terminal's `--mode` agree
(`captain_voice_mode` in `state.json`).

```text
# the page's log panel — the dropped sentence (no words), two deliveries, the window, the stop word
$ # slot5-page-ship.txt
STATUS: say Captain | THINKING: chip
$ # slot5-page-two.txt
  4.0s said: Captain, what is up?
 11.1s said: Captain, snooze the first one for an hour.
 12.1s reply: There's one item in the queue (action_seq 24).

**Alpha asks: "Is the deploy window today or tomorrow?"**

It's not in the queue, but coder-01m3m79j was last showing the prompt "Do you want to create RELEASE.md?" If it's still waiting, the deploy stays blocked until that's answered. Should I say yes to it?
 19.1s reply: I snoozed the deploy-window question until 15:47 UTC (action_seq 25), and the queue is now empty.

coder-01m3m79j may still be waiting on "Do you want to create RELEASE.md?" Should I say yes to it?
STATUS: say Captain | THINKING: chip
$ # slot5-page-window.txt
  3.0s said: Captain
  9.4s said: What is up?
 18.4s reply: Nothing needs you right now; the queue is empty (action_seq 26). The deploy-window question is snoozed until 15:47 UTC.

One thing is still open: coder-01m3m79j was last showing the prompt "Do you want to create RELEASE.md?" Should I say yes to it?
STATUS: say Captain | THINKING: chip
$ # slot5-page-stop.txt
  3.0s said: Stop listening.
  3.0s note: mic off (stop word)
STATUS: connected | THINKING: chip
```

## 6. Every action a board event; a refusal said, never faked

`stop`, `spawn` and `restart` act only when your words name the agent, its role
or its project. Words that name nothing are refused by the tool, which gives the
captain its one-sentence question; your yes answers it.

Say **"stop it"**, with nothing named. Expect one `captain_action` for `stop`
with `ok: false` on alpha's board (the owner's words name no agent, role or
project), the captain asking its one question (*Stop coder-1 in alpha?*), and
nothing stopped. Answer **"yes"**: expect the stop, with its receipt
(`agent_exited`).

Then say **"spawn a coder in alpha"** and **"stop the coding agent in alpha"**.
Both name what they act on, so each goes at once, with no question: the spawn
with its row, and the stop with its receipt and the coder's claim released.

```sh
aisquare captain log alpha --limit 4
aisquare board
```

```text
# the refused stop (ok false) and the captain's question, the stop after your yes, then the named spawn and stop, each with its receipt
$ # slot6-page-stopit.txt
  0.5s said: Stop it.
  8.6s reply: The stop was refused because "Stop it." doesn't name an agent. The tool asks: **Stop coder-01m3m79j in alpha?**
STATUS: connected | THINKING: chip
$ # slot6-page-yes.txt
  0.5s said: Yes.
 14.1s reply: I stopped coder-01m3m79j in alpha (action_seq 30). Stopping it released its task, "Unblock deploy: add missing release note," so the task is back on alpha's board with no one working on it.

The last time I looked, the coder was still waiting for permission to create RELEASE.md, so the file probably doesn't exist. That means the deploy is probably still blocked. The deploy-window question c
STATUS: connected | THINKING: chip
$ # slot6-page-spawn-named.txt
  0.5s said: Sporna Coda in Alpha
 11.1s reply: I read "Sporna Coda in Alpha" as "spawn a coder in alpha" and started one on the release-note task (action_seq 31). The tool reused the label **coder-01m3m79j** and the same tmux session, which suggests it picked up the stopped coder's old session instead of starting fresh.

I haven't pasted the manager's instructions this time. If the old session came back, the coder already has them and m
STATUS: connected | THINKING: chip
$ # slot6-page-stop-named.txt
  0.5s said: Stop the coding agent in alpha.
 10.6s reply: I stopped coder-01m3m79j in alpha (action_seq 33). No claims were released this time, so it looks like the restarted coder never claimed the task again.

As a result, "Unblock deploy: add missing release note" is still open on alpha's board with no one on it, and no coder is running in alpha. The deploy is probably still blocked. The deploy-window question comes back at 15:47 UTC.
STATUS: connected | THINKING: chip
$ # slot6-captain-log.txt
seq  when      board  tool   ok  said                    utterance              
27   14:49:52  alpha  stop   ✗   refused: the owner's    Stop it.               
                                 words 'Stop it.' name                          
                                 no agent, role or                              
                                 project, so                                    
                                 confirm=true is not                            
                                 taken; asked 'Stop                             
                                 coder-01m3m79j in                              
                                 alpha?'                                        
30   14:51:04  alpha  stop   ✓   stopped coder-01m3m79j  Stop it. — Stop        
                                                         coder-01m3m79j in      
                                                         alpha? — Yes.          
31   14:52:37  alpha  spawn  ✓   spawned coder-01m3m79j  Sporna Coda in Alpha   
                                 (coder)                                        
33   14:54:49  alpha  stop   ✓   stopped coder-01m3m79j  Stop the coding agent  
                                                         in alpha.
$ # slot6-board.txt
<aisquare-team>
sessions:
  - captain: captain — 0m ago
  - 784cd4f7 manager — 1m ago
tasks (1 todo):
  - tsk_01m3m79jkwrsa00crf0rxfchea [todo] Unblock deploy: add missing release note
recent updates:
  - captain: (captain) captain_action: {"v": 1, "tool": "stop", "project": "prj_b626b5d4d5347a4b88af2242", "args": {"project": "alpha", "label": "coder-01m3m79j", "force": false, "confirm": true}, "utterance": "Stop it. — Stop coder-01m3m79j in alpha? — Yes.", "ok": true, "said": "stopped coder-01m3m79j", "receipt": 29}
  - captain: (captain) captain_action: {"v": 1, "tool": "spawn", "project": "prj_b626b5d4d5347a4b88af2242", "args": {"project": "alpha", "role": "coder", "label": null, "task": "tsk_01m3m79jkwrsa00crf0rxfchea", "persona": null, "confirm": true}, "utterance": "Sporna Coda in Alpha", "ok": true, "said": "spawned coder-01m3m79j (coder)", "receipt": null}
  - 171650fb (coder) agent_exited: coder-01m3m79j exited (0)
  - captain: (captain) captain_action: {"v": 1, "tool": "stop", "project": "prj_b626b5d4d5347a4b88af2242", "args": {"project": "alpha", "label": "coder-01m3m79j", "force": false, "confirm": true}, "utterance": "Stop the coding agent in alpha.", "ok": true, "said": "stopped coder-01m3m79j", "receipt": 32}
  - captain: (captain) captain_action: {"v": 1, "tool": "log", "project": "prj_b626b5d4d5347a4b88af2242", "args": {"project": "alpha", "limit": 4, "via": "cli"}, "utterance": "aisquare captain log alpha --limit 4", "ok": true, "said": "read 4 captain action(s)", "receipt": null}
</aisquare-team>
```

## 7. `make check` green

On the assembled `rc/captain-v1` head, in docker: no host venvs (the owner's
rule). Mount the worktree, and the main checkout read-only with its `.git`, at
their host paths. A worktree's project root resolves to the main checkout, and
without it `tests/test_query_time_damage_is_legible.py` fails on any head.
Install the worktree editable inside the container, as CI does, and run each
phase as your own user with an isolated `HOME` and `AISQUARE_HOME`.

`tests/test_install_script_functions.py`'s
`test_root_is_refused_outside_a_container` skips inside a container and names
the sign it found, so the docker gate reads 0 failed.

```text
# ruff format --check · ruff check · mypy --strict · pytest counts · exit 0
[14:31:00] format exit=0 (0s) 357 files already formatted
[14:31:00] lint exit=0 (0s) All checks passed!
[14:31:24] typecheck exit=0 (24s) Success: no issues found in 352 source files
[15:00:13] test exit=0 (1729s) ================= 6024 passed, 7 skipped in 1552.79s (0:25:52) =================
[15:00:13] RESULT name=t6-a7592d1d head=a7592d1d45061328ea2050a0cff0d26e8389db08 format=0 lint=0 typecheck=0 test=0
[15:00:15] leak diff: 0 lines (identical)
# since main-sync (#233): the RC 16b4d9a3 (its tree identical to aca3829e) gates at
# 6232 passed, 8 skipped, 0 failed (runner2-1's docker gate, 2026-09-29 06:13Z)
```

## Not heard, not held

List here, per replay, what the box could not do (no headset, no Android
device, a model that would not load), so a green slot is never assumed.

```text
# not heard, not held
Replay: runner2-1, the assembled real-claude pass in docker (image r2-voice:py312) on #222's head a7592d1d,
whose code is byte-identical to the RC b750c05c. What this box could not do, so no slot here is assumed:
- NO HEADSET, NOTHING PLAYED: speech is silent by the owner's order. The captain's speaker went through a
  logging shim (no Windows speech inside a container); "spoken" in these slots means the reply reached the
  speaker adapter, counted in the shim's log. The owner's ears are the first to hear it.
- NO PHONE, NO REAL MICROPHONE: the page ran in headless Chromium inside the container, with the owner's
  requests fed as the fake microphone from recorded WAVs (Windows System.Speech rendered to file). The
  Android adb reverse path and the headset mic were not exercised.
- WHISPER ON THE CPU: faster-whisper base.en, the model from the host's cache, CPU int8 (no GPU in the
  container). Transcripts matched the recordings; a GPU would only be faster.
- A STAND-IN MANAGER: alpha's manager is the fixture stand-in (it answers on the board with a canned
  line); the captain and the coder are the REAL Claude Code 2.1.283 under the .claude2 login.
- THE MODEL: this login's captain ran on Opus 5.5, so turns are slower than on Fable (what is up about
  20 s here); the owner's own config decides the model.
- KNOWN, NOT BLOCKING: after an unnamed "stop it" the captain says the tool's question but narrates the
  refusal first; card tsk_01m3kxfyhdkg owns the wording (ruling 14570).
- WHISPER AND "CODER": through the page's fake microphone, whisper base.en heard "spawn a coder in alpha"
  as "Sporna Coda in Alpha" (step 6c; offline, the same file was heard right). The captain read it as a
  spawn, and the named project carried the confirm, so it went at once; the owner may hear a coder
  called a coda until the voice train's transcriber lands.
```

## Known facts about CI

The RC base's Windows leg is red on five persona tests from the fold — a
persona-train follow-up, not the captain's. The RC's own Windows run at
`f28b7eb2` also failed three UI tests: `test_ui_accounts`'s slot buttons and
`test_ui_shell`'s refusal dialog and explainability toasts. The sidebar-width
tests (the divider's tap-after-drag and the width autosave) are intermittent on
any leg: they failed on ubuntu py3.12 and the ambient proxy-up leg at #232, and
earlier on three other heads. They pass on re-run. Since main-sync (#233), the
RC's own Windows run at `16b4d9a3` (run 36530827551) failed the five persona
tests and `test_ui_accounts`'s keychain-backed account test, and
`test_ui_accounts`'s unreadable-usage row failed on Windows at #230 and #232. A
captain PR's legs are read against that base set.

## After a reboot, or a tmux kill-server

After a reboot the fleet's socket file is gone too, so bare `aisquare captain`
ends the stale row and starts a fresh captain. The folder is already trusted, so
it comes straight up.

After a `kill-server` the socket file stays and nothing answers. Then
`aisquare captain` and `aisquare captain "text"` refuse within a second, naming
the one command that may decide the server is gone; `<home>` is the project id
the refusal prints. Run it, then start again:

```sh
aisquare fleet reap -P <home> --server-down
aisquare captain
```

```text
# the pass's receipt for this section
$ aisquare captain
✗ the captain's row (agt_01m3m6ykv41ehmba2zkhky4mgb) is live but its tmux server does not answer — nothing answers on /tmp/tmux-1001/asq-runner-final. If that server is really gone (a kill-server; a reboot elsewhere), run `aisquare fleet reap -P prj_9d8001441f1d60b2c6ceaaea --server-down`, then `aisquare captain` starts a fresh captain
$ aisquare captain say 'what is up' --timeout 5
✗ the captain's row (agt_01m3m6ykv41ehmba2zkhky4mgb) is live but its tmux server does not answer — nothing answers on /tmp/tmux-1001/asq-runner-final. If that server is really gone (a kill-server; a reboot elsewhere), run `aisquare fleet reap -P prj_9d8001441f1d60b2c6ceaaea --server-down`, then `aisquare captain` starts a fresh captain
(say returned after 0.5 s)
✓ reaped: 0 ended, 1 lost, 0 worktrees removed
  ✗ captain  pane %1 gone
```
