> **The owner's plan document for the captain, verbatim.** Published 2026-09-25 as
> <https://claude.ai/artifact/3FkovmNoVRLB9ocibXkgqz>, copied unchanged from the owner's
> `captain-plan.txt` on 2026-10-13 so the tree carries the source that
> `docs/plans/captain.md` (the engineering record) cites. Nothing below is edited;
> the engineering record says what changed in the build.

aisquare-cli · new feature · plan for alignment
Captain Plan
final · 2026-09-25 · name: Captain · RC: rc/captain-v1 cut from the fold · cards T1 to T6 filed on the aisquare-cli board · base: the fold branch merge/rc-fixes-into-hackathon @ b39f6fa · Research report attached below.
One agent you talk to, over headphones or in a text box, that runs the whole CLI for you: it reads every project's board and panes, tells you what needs you, delegates to the managers, spawns and briefs agents, pastes loop prompts, presses the keys that unblock a pane, and files a receipt for everything it did. Built as three loosely coupled layers so voice is one adapter among several, not the product.

1. Outcomes it drives
One triage instead of N boards. Today you are the single approver across every board and pane. "What needs me?" returns the blocked cards, questions, waiting permission prompts and stale agents across all projects in one answer, ranked.
Delegation in one sentence. "Ask the explainability manager for a loop prompt for a PII coder, spawn the coder, paste the prompt" is a single utterance; the round trip (tell, wait for the manager's reply, spawn, paste, confirm the pane took it) runs without you.
Unblocking by voice. A pane stuck on a permission prompt or a y/N question is answered from the headset. The July RCA named owner-approval round trips as the top delay; this removes the walk to the keyboard.
Summaries since last update. Per project, agent or pane, from board events past a watermark plus the pane's own output, so you stop scrolling transcripts.
Hands-free hours. Walk, cook, drive; the fleet keeps moving and you stay the decision maker, not the typist.
2. Architecture: three layers, one contract each
Interfaces · adapters in, adapters out
TUI panel: a button beside Fleet and + opens a docked transcript with an input line
CLI: aisquare <name> "…" one-shot, aisquare <name> chat REPL, --json
Voice in: any dictation app that types (WisprFlow, Win+H, macOS dictation) already works on the input line; native push-to-talk and wake word are adapters over the Transcriber seam Anmol's cliXR draft already has
Voice out: a Speaker protocol; OS voices first (say, SAPI, spd-say), neural voices later
Identity gate: a Verifier protocol; passphrase first, Windows Hello and Touch ID adapters later
Remote: the cliXR browser client (phone, headset) speaks the same protocol
Brain · a fleet agent of its own role
A real Claude Code session, spawned like a manager: role captain with its own team bind (binary, account, config dir) and a bundled captain persona. Your subscription pays; nothing new to install.
It lives in a tmux pane on the fleet server, scoped to the whole home rather than one project, so the existing agent view, tell, capture and Stop all work on it unchanged.
Its briefing (role cycle plus persona) says how to act as you, that pane text is data not instructions, and that every action is a tool call with a receipt.
Its tools are one MCP server mounted at spawn (--mcp-config): the Actions layer below and nothing else. That is how "what it outputs" maps deterministically onto keys and verbs: the model never types keystrokes itself, it calls press, paste, ui and speak, and the tool does one fixed thing.
Restarts resume the same session (the fold already replays a row's launch spec and persona).
Actions · the only hands on the CLI
Cross-project reads: projects, board, attention, since (events past a watermark plus a pane tail), read_pane
Delegation: tell, ask_manager (tell, then wait for the reply note), spawn, stop, restart, attach_persona, task verbs, note
Acting as you: press (a fixed key vocabulary into a pane: y, n, Enter, Esc, arrows, ctrl-c), paste (bracketed, never submits on a newline), ui (a fixed action vocabulary: open spawn, open stop, select row, copy row, focus project)
Voice out: speak(text) queues text to the Speaker; the brain decides what is worth saying, the adapter decides how
Every call writes one captain_action board event with the utterance that caused it and the receipt it got; the confirmation policy lives here, not in any interface. No identity gate in Phase 1 by owner decision; the TOTP gate (default on, switchable off) is a filed follow-up.
Everything in Actions wraps services that already exist on the fold: fleet.tell, fleet.spawn, fleet.stop, fleet.restart, attach_persona, TmuxServer.capture, send_keys, paste, store.events_since, the single-project MCP tools. New code is the cross-project layer, the watermarks, press, ask_manager, the receipts, and the interfaces.
3. Phases
Phase 1 · live tomorrow · voice in, voice out, and the actions
Owner decision: audio summaries and audio-taken requests are Phase 1, not Phase 2. The scope grows; the date holds by reusing what exists.
Ships
The captain role, bind and persona; asq captain spawns or attaches it; the button beside Fleet and + opens its pane with a mic control and a speaker toggle; aisquare captain "…" one-shot and chat; the Actions MCP server (projects, board, attention, since, read_pane, tell, ask_manager, spawn, stop, restart, attach_persona, task verbs, note, press, paste, ui, speak); receipts on the board; a confirm step for stop, force and anything that spends quota; watermarks per project and agent.
Voice in
Two paths, both real on day one. (1) The cliXR push-to-talk client from Anmol's draft: a browser page on the phone or desktop holds the mic (Windows owns the audio, so no WSLg flakiness), streams 16 kHz frames, the server transcribes with faster-whisper through the existing Transcriber seam and routes the text to the captain's pane. Lifted out of the XR ring into a plain page served by asq captain --voice. (2) Dictation apps (WisprFlow, Win+H, macOS) typing into the pane, submit by key or the word "send".
Voice out
The speak tool feeds a Speaker: on WSL, System.Speech through powershell.exe so audio plays natively in your headphones; say on macOS; spd-say on Linux. The captain speaks the summary and the questions, never the tool noise. Kokoro neural voices are Phase 2.
Acceptance
(1) Headphones on, push-to-talk "what needs me": the captain speaks the needs-you, blocked and question items across every project, project and agent named. (2) Spoken "ask the CLI manager for a loop prompt for P22, spawn a coder for it and paste the prompt": a live coder whose pane shows the prompt, and three receipts on the board. (3) A coder stuck on a permission prompt is unblocked by "say yes to coder3a" and the pane advances. (4) "What did coder3c do since my last update?" is spoken back from events and pane output past the watermark, and the watermark moves. (5) Every action is a board event with a seq; a refused action is said, never faked. (6) make check green, docs page, the runner replays the runbook with a real headset.
Team
Six coders on the new RC, one runner, non-author gates: T1 Actions MCP server and receipts (the contract, written first), T2 captain role, bind, persona and spawn path, T3 the voice page and Speaker adapters (lift cliXR's client and transcriber), T4 the TUI button, pane view, mic and speaker controls, T5 CLI verbs and watermarks, T6 docs, runbook, demo. T2 to T5 stack on T1's tool contract.
Phase 2 · this week · native voice and the identity gate
Hands-free: Silero VAD end-of-speech so you stop pressing the button; Moonshine for lower latency; a cloud transcriber (Deepgram, AssemblyAI) as an optional adapter. No wake word: push-to-talk plus end-of-speech is more reliable and has no always-listening issue; sherpa-onnx keyword spotting if ever wanted.
The TOTP identity gate: default on, switchable off in config, a six-digit code spoken or typed unlocks a 15-minute act-as-you window. Filed as a follow-up.
On WSL2 the microphone and speaker live in a small Windows-side voice satellite (pip install aisquare[voice] on Windows Python) that talks to the WSL brain over localhost with a Wyoming-style JSON-lines protocol; WSLg audio is too flaky to ship on. macOS and Linux capture natively.
Neural TTS with Kokoro (MIT) through sentence streaming, and barge-in (you talk, it stops). Piper and edge-tts stay out of the core: GPL.
Biometric gate adapters after TOTP: Windows Hello through the satellite (needs a Windows process and a window handle), Touch ID via pyobjc, Linux polkit or sudo re-auth.
UI actions: open dialogs, select and copy rows, drive the Spawn and Stop dialogs through the panel.
The browser push-to-talk client from cliXR as the phone and headset interface.
Phase 3 · next · autonomy and reach
A standing tick: it triages attention on its own, drafts the unblock, and speaks up only when a decision is yours.
Policies: auto-approve reads, ask on writes, never force without the gate; per-project overrides.
Memory of your decisions across days; cross-project dependencies (the senior-manager idea in #199 becomes a policy here, not a new agent).
More interfaces over the same contract: Slack or phone, the XR ring.
4. Name: Captain
Owner decision after three naming rounds (reports appended): the working name stays. asq captain, role captain, persona captain, branch rc/captain-v1. Spoken address: "Captain, what needs me". Button glyph: a rank insignia, a single star, lit in the accent colour while the captain row is live and dim otherwise.

Two easter eggs ride along, both real verbs with receipts: wololo (Age of Empires) reassigns an idle agent to a task, releasing its claim, claiming the new one and telling it; bt ("bad trip", a nod to Titanfall's BT and its Protocol 3, protect the Pilot) is the brake: it cancels any in-flight wait, clears the speaker queue, undoes the last reversible action the captain took, and says what it undid. asq captain uav is the sitrep alias for attention.

5. Where it is built
rc/captain-v1 is cut from the fold merge/rc-fixes-into-hackathon at b39f6fa (persona train, backlog train, v15 converge) and pushed; worktree /home/work/work/aisquare-cli-captain. Feature PRs land into it; it merges to main after #203, as decided.
Package layout: src/aisquare/services/captain/ (actions.py, voice.py, speaker.py), cli/captain.py, personas/captain/SKILL.md, web/captain/, plus cli/ui/ hooks for the button, the view and the ui-action receiver.
New dependencies in Phase 1: none in core. The [voice] extra carries faster-whisper plus starlette and uvicorn for the push-to-talk page (the pair cliXR and serve already use). Phase 2 adds silero-vad, moonshine, kokoro-onnx, pyotp.
Prior art borrowed, not depended on: yapcode (one voice agent over many Claude Code panes through tmux; its tool vocabulary), voicemode (voice as an MCP tool; its WSL2 notes), Home Assistant's Wyoming protocol (each stage a small server behind a tiny JSON protocol).
6. Risks and how they are held
A day of summaries grows the captain's context. It is a Claude Code session, so /compact and a restart that resumes the row cover it; the board is the durable memory.
Voice page latency. faster-whisper base.en on CPU transcribes a short request in about a second; the page shows the interim text so a misheard request is caught before it runs.
Prompt injection from panes. Pane captures are untrusted text; the brief says so, the allowlist is default-deny, destructive verbs confirm, and the gate has a TTL.
Acting as you in the wrong pane. press and paste name the project and label, refuse a busy pane the way tell does, and every keystroke is a receipt. Force actions ask for a confirm in the pane.
The brain's own permission prompts. Its tool list is exactly the Actions MCP server; nothing else is allowed, so auto mode has nothing to refuse.
WSL2 audio. The push-to-talk page keeps the mic on the Windows side (the browser), and TTS plays through PowerShell natively, so no audio crosses WSLg.
Scope creep on day one. Phase 1 has six acceptance lines; anything else is Phase 2.
7. Research report
# Research: "manager of managers" voice/text overseer for aisquare-cli

Date: 2026-09-25. Star counts, licenses, and last-push dates come from the GitHub API. Versions come from the PyPI JSON API, pulled the same day.

## 1. BRAIN: a persistent conversational agent with tools

| Option | License / maturity | Install weight | Verdict |
|---|---|---|---|
| **Claude Agent SDK (Python)**: [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/), [repo](https://github.com/anthropics/claude-agent-sdk-python) | MIT, 8.2k stars, v0.2.159 (2026-09-23) | Small pip package. It needs the `claude` CLI installed, because it drives the CLI as a subprocess | **Pick this.** `ClaudeSDKClient` keeps one live session. A stdio MCP server is a single dict entry. |
| **Headless `claude -p`** ([docs](https://code.claude.com/docs/en/headless)) | Ships with Claude Code | Zero new deps | Good fallback. You get continuity with `--resume <session_id>`, but every turn is a new process with MCP startup cost. |
| **Anthropic Messages API tool loop**: [`anthropic`](https://github.com/anthropics/anthropic-sdk-python) | MIT, 3.9k stars | Small | Most code. The native [MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector) works only with **remote HTTP/SSE** servers, not stdio. You would need the SDK's MCP helpers plus your own `mcp` client session, and you would manage history yourself. |

**How much code it takes to reach `aisquare serve --stdio`.** The Agent SDK needs the least. From the [Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python):

```python
opts = ClaudeAgentOptions(
    mcp_servers={"asq": {"type": "stdio", "command": "aisquare", "args": ["serve", "--stdio"]}},
    allowed_tools=["mcp__asq__*"],          # scoped names are mcp__<server>__<tool>
    system_prompt=OVERSEER_PROMPT,
)
async with ClaudeSDKClient(opts) as client:
    await client.query(user_text)
    async for msg in client.receive_response(): ...
```

**How continuity is kept across turns.**
- `ClaudeSDKClient` keeps one conversation open across `query()` calls. The docs say it "maintains a conversation session across multiple exchanges."
- To survive restarts, use `resume="<session_id>"` or `continue_conversation=True`.
- In headless mode, take `session_id` from `--output-format json` and pass it back with `--resume`. The docs also note that `--resume` accepts a `.jsonl` transcript path.
- Headless mode supports `--mcp-config`, `--allowedTools` with permission-rule syntax, `--permission-mode dontAsk`, and `--bare`. Bare mode skips `~/.claude` hooks and CLAUDE.md, and it needs `ANTHROPIC_API_KEY` because it ignores the subscription login.
- **Caveat for this project.** If you do not use `--bare`, the brain session loads the project hooks, including aisquare's own SessionStart briefing. It also shows no trust dialog for `.mcp.json`. Run the brain from a dedicated working directory, or with `--bare` plus explicit `--mcp-config`.

## 2. VOICE IN (STT)

| Engine | License / maturity | Weight | Verdict |
|---|---|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT, 25.6k stars, v1.2.1 (2025-10) | CTranslate2 wheels. Models are 75 MB (tiny) to 1.5 GB | Best local accuracy per CPU cycle. The de-facto default. |
| [pywhispercpp](https://github.com/absadiki/pywhispercpp) ([whisper.cpp](https://github.com/ggml-org/whisper.cpp), 53.9k stars) | MIT, 347 stars, v1.5.1 (2026-08) | Wheels include ggml. Metal on macOS | Lightest native whisper. The binding is small but active. |
| [Moonshine](https://github.com/moonshine-ai/moonshine) (`moonshine-voice`, `useful-moonshine-onnx`) | MIT for code and English models, 11.1k stars, v0.1.5 (2026-08) | ONNX-only, tiny models | Lowest latency for short commands. **Strong Phase-2 pick** for a live headset. |
| [Vosk](https://github.com/alphacep/vosk-api) | Apache-2.0, 15.1k stars. The PyPI release is from 2022-12 | 50 MB models | Streaming and grammar-capable, but dated accuracy. Skip. |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0, 15.0k stars, v1.13.8 (2026-09) | One wheel covers STT, TTS, VAD, and keyword spotting | **The Swiss-army option.** One dependency on every OS. |
| [RealtimeSTT](https://github.com/KoljaB/RealtimeSTT) | MIT, 10.1k stars, v1.1.2 (2026-08) | Pulls faster-whisper, torch, Silero, and optional wake words | Fastest way to a working mic-to-text loop. Torch makes it heavy. |
| Cloud: [Deepgram Nova-3](https://deepgram.com/learn/introducing-nova-3-speech-to-text-api), [AssemblyAI Universal-Streaming](https://www.assemblyai.com/universal-streaming), [OpenAI gpt-4o-transcribe](https://developers.openai.com/api/docs/models/gpt-4o-transcribe) | Proprietary | HTTP or WebSocket only | Streaming prices are roughly [$0.005–0.008/min](https://convertaudiototext.com/blog/deepgram-nova-3-explained), [$0.15/hr billed per open session](https://www.assemblyai.com/docs/faq/how-does-universal-streaming-session-based-pricing-work), and [~$0.017/min](https://costgoat.com/pricing/openai-transcription). Use as an optional plugin. |

**WSL2 microphone reality.**
- **It works, but it is flaky.** WSLg exposes the Windows mic as PulseAudio `RDPSource` at `unix:/mnt/wslg/PulseServer`.
- `arecord` and `parec` capture from it after you install `libasound2-plugins` and add a `~/.asoundrc` containing `pcm.!default { type pulse }`. Sources: [claude-code#32497](https://github.com/anthropics/claude-code/issues/32497) and [#33701](https://github.com/anthropics/claude-code/issues/33701).
- Known problems:
  - RDP relay reconnects make mic reads fail intermittently ([copilot-cli#4716](https://github.com/github/copilot-cli/issues/4716)).
  - Playback crackles from underruns ([wslg#1429](https://github.com/microsoft/wslg/issues/1429)).
  - `pulseaudio.service` breaks on some Ubuntu 24.04 setups ([wslg#1378](https://github.com/microsoft/wslg/issues/1378)).
  - Claude Code's own `/voice` hard-blocks WSL, and the issue was closed as not planned.
- **Practical answer.** Run capture on the Windows side. Either use dictation, where Windows owns the mic, or run a tiny Windows-side Python helper that sends text over localhost to the WSL process. Treat WSLg capture as best-effort.

## 3. VOICE OUT (TTS)

| Engine | License / maturity | Latency / quality | Verdict |
|---|---|---|---|
| [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (model [Kokoro-82M](https://github.com/hexgrad/kokoro), Apache-2.0) | MIT, 2.7k stars, v0.6.1 (2026-08) | About 300 MB, or about 80 MB quantized. Near real time on CPU. Best open quality | **Default local voice.** Python 3.10–3.13. |
| [Piper](https://github.com/OHF-Voice/piper1-gpl) (`piper-tts`) | **GPL-3.0** since the move to OHF. The old [MIT repo](https://github.com/rhasspy/piper) was archived in 2025. Voice licenses vary | Very fast, robotic-to-OK quality | GPL is contagious if you bundle it. Offer it as an optional external binary only. |
| sherpa-onnx TTS | Apache-2.0 | Runs Kokoro, Piper-VITS, and Matcha models | Good if you already use sherpa for STT. |
| [edge-tts](https://github.com/rany2/edge-tts) | **GPL-3.0**, 12k stars. Unofficial use of Microsoft Edge's read-aloud service | Excellent neural voices, needs network | Great quality at zero cost, but it could break at any time. Optional plugin only. |
| OS built-ins: `say` (macOS), SAPI via [pyttsx3](https://github.com/nateshmbhat/pyttsx3) (MPL-2.0), `spd-say`/`espeak-ng` | Built in | Instant, lower quality | **Phase-1 zero-dependency speaker.** From WSL, shell out to `powershell.exe` with `System.Speech` so the sound plays natively on Windows and avoids WSLg crackle. |

[RealtimeTTS](https://github.com/KoljaB/RealtimeTTS) (MIT, 4k stars) wraps most of these engines with sentence-chunked streaming, which cuts time-to-first-audio.

## 4. WAKE WORD, PUSH-TO-TALK, VAD

- **[openWakeWord](https://github.com/dscripka/openWakeWord).** Apache-2.0 code, 2.8k stars, v0.6.0 from 2024-02. It ships pretrained models, and some carry non-commercial CC-BY-NC terms. It is the Home Assistant default. Verdict: usable, but stale.
- **[Porcupine](https://github.com/Picovoice/porcupine).** The SDK is Apache-2.0 but needs an AccessKey. The **free tier ended on 2026-06-30** ([HA forum](https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744)), and signup requires a company email. Verdict: **avoid.**
- **sherpa-onnx keyword spotting.** Apache-2.0. Verdict: the best license-clean wake word if you need one.
- **[Silero VAD](https://github.com/snakers4/silero-vad).** MIT, 10.3k stars, v6.2.3 from 2026-09. It runs on ONNX, with no torch required. Verdict: **the VAD to use.**
- **[py-webrtcvad](https://github.com/wiseman/py-webrtcvad).** The last release was in 2017, and it needs a C build on Windows. Verdict: skip.
- **Hotkeys.**
  - [pynput](https://pynput.readthedocs.io/en/latest/limitations.html) is LGPL-3.0. It needs an X server and `$DISPLAY`, gives only Xwayland-limited function under Wayland, needs root for uinput, and needs Accessibility permission on macOS.
  - [keyboard](https://github.com/boppreh/keyboard) is MIT but needs root on Linux.
  - **Neither can see Windows keys from inside WSL2.**
  - Verdict: in Phase 1, push-to-talk is the dictation app's own hotkey, or a key binding inside the TUI such as `ctrl+space` toggling a record worker. A global hotkey needs the Windows-side helper.
- **Recommendation.** Skip wake words. Headset push-to-talk plus Silero end-of-speech detection is more reliable and has no always-listening privacy issue. Claude Code's own `/voice` made the same choice ([guide](https://claudefa.st/blog/guide/mechanics/voice-mode)).

## 5. FULL PIPELINES

| Framework | License / maturity | Verdict |
|---|---|---|
| [pipecat](https://github.com/pipecat-ai/pipecat) (`pipecat-ai` 1.11) | BSD-2, 15.8k stars, daily commits | Has a `LocalAudioTransport` over PyAudio ([example: locat](https://github.com/vipyne/locat)). It has good turn-taking and barge-in. **The only one worth considering, for Phase 2.** |
| [LiveKit Agents](https://github.com/livekit/agents) | Apache-2.0, 14.3k stars | Built around WebRTC rooms and a server. **Overkill** for a local CLI. |
| [Vocode](https://github.com/vocodedev/vocode-core) | MIT, 3.8k stars, last push 2024-11 | Stale and telephony-focused. Skip. |
| [Kyutai Unmute](https://github.com/kyutai-labs/unmute), [Modal QuiLLMan](https://github.com/modal-labs/quillman) | MIT | GPU- or server-bound demos. Useful to read, not to depend on. |
| Vapi, Retell, and similar | SaaS | They are aimed at phone calls. Wrong shape. |

Awesome lists: [awesome-voice-agents](https://github.com/yzfly/awesome-voice-agents), [awesome-whisper](https://github.com/sindresorhus/awesome-whisper), [Awesome-Text-to-Speech](https://github.com/ishandutta2007/Awesome-Text-to-Speech), [awesome-voice-ai](https://github.com/ALLBOTSIO/awesome-voice-ai), and [awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators) for competitors.

## 6. DICTATION AND OS INTEGRATION

- **WisprFlow.**
  - Its API is invite-only ([platform](https://platform.wisprflow.ai/), [quickstart](https://api-docs.wisprflow.ai/quickstart)). It accepts base64 16 kHz WAV. I found no webhook.
  - The desktop app works by keystroke or paste injection. It pastes into the active window through the clipboard, using Shift+Insert in some editors.
  - Per [its WSL/terminal page](https://docs.wisprflow.ai/articles/6478598909-using-flow-with-linux-wsl-and-terminal-applications), **direct paste works in Windows Terminal running a WSL shell.** It does *not* work in WSLg GUI windows or over SSH. There is no native Linux app.
  - Caveat: Flow's auto-formatting also applies in terminals, so capitalization and punctuation will change.
  - The unofficial [wisprflow-sdk](https://github.com/ThisisShashwat/wisprflow-sdk) reverse-engineers the client. Do not depend on it.
- **Windows Win+H voice typing** works in native Windows apps, including Windows Terminal. It is [not supported in WSLg GUI apps](https://github.com/microsoft/WSL/discussions/10686).
- **macOS Dictation** works in any text field, including Terminal and iTerm.
- **[Talon](https://talonvoice.com/)** is free, closed source, and scriptable in Python. It covers macOS, Windows, and Linux. It is powerful but has a real learning curve. Treat it as a user-side power tool, not a dependency.
- **Practical rule, confirmed.** Any dictation app that types into the focused field gives a Textual `Input` or `TextArea` voice for free, as long as the TUI runs in a native terminal window. That means Windows Terminal, iTerm, or a GNOME terminal. So Phase 1 needs no audio code at all. One caution: dictated text may carry a trailing newline or smart punctuation. Normalize it, and **never let dictated Enter auto-submit a destructive command.**

## 7. IDENTITY GATE FOR "act as me"

| Platform | Mechanism | 1-day realistic? |
|---|---|---|
| Windows | `winrt-Windows.Security.Credentials.UI` (v3.2.1, [pywinrt](https://github.com/pywinrt/pywinrt), MIT) calls `UserConsentVerifier.RequestVerificationAsync` ([docs](https://learn.microsoft.com/en-us/uwp/api/windows.security.credentials.ui.userconsentverifier.requestverificationasync)). Desktop apps should use `RequestVerificationForWindowAsync` with an HWND ([discussion](https://github.com/microsoft/WindowsAppSDK/discussions/1265)) | **Partly.** It must run as *Windows* Python. From WSL, shell out to `powershell.exe` or a small Windows-side helper. A console app with no HWND can put the dialog behind other windows. Budget half a day. |
| macOS | [`pyobjc-framework-LocalAuthentication`](https://pypi.org/project/pyobjc-framework-LocalAuthentication/) (MIT, v12.2.2) calls `LAContext.evaluatePolicy_localizedReason_reply_` ([sample](https://github.com/lukaskollmer/python-touch-id/blob/master/touchid.py)) | **Yes**, about 40 lines. Use `LAPolicyDeviceOwnerAuthentication` so it falls back to the password. |
| Linux | `pkcheck --allow-user-interaction --enable-internal-agent` ([polkit](https://www.freedesktop.org/software/polkit/docs/latest/polkit.8.html)), `sudo -v`, or `fprintd-verify` | **Yes, crudely.** `sudo -k && sudo -v` is the most portable re-auth. Polkit needs a custom action file. |
| Portable | A TOTP code via [pyotp](https://pypi.org/project/pyotp/) (MIT), or a local passphrase stored as an Argon2 or scrypt hash | **Yes, and it is the Phase-1 gate.** It works identically over voice by speaking the six digits. |

Design the gate as a `Verifier` protocol with an `available()` method and a `verify(reason)` method. Pick OS biometrics when the platform offers them, and fall back to TOTP. Grant a time-boxed "act-as-me" token, for example 15 minutes, and log every privileged action to the board.

## 8. KEYBOARD ACTING-AS-ME

- **Panes, the primary path.** Use tmux `send-keys -l` for literal text, `load-buffer`/`paste-buffer -p` for multi-line prompts where bracketed paste stops Claude Code from submitting on each newline, and `capture-pane -p -S -N` to read. The CLI already does this, and so does [yapcode](https://github.com/nithiink/yapcode).
- **Inside the Textual UI.** On a running app you get [`App.run_action("name")`](https://textual.textualize.io/api/app/), `App.simulate_key(key)`, and `post_message()`.
  - Textual is **not thread-safe.** Call from other threads through `app.call_from_thread`. `post_message` is the thread-safe exception.
  - `Pilot` ([docs](https://textual.textualize.io/api/pilot/)) exists only under `run_test()`, so it is headless and meant for tests, not production.
  - Limits: `simulate_key` goes to the focused widget, so focus must be driven first. Modal screens capture input. There is no public "click at x,y" outside Pilot.
  - **Better design:** expose the UI's verbs, such as stop agent or spawn, as *actions or services the overseer calls directly*, not as synthesized keystrokes. The P21 Stop dialog already routes through `services.fleet.stop`.
- **OS automation, last resort.**
  - [pyautogui](https://github.com/asweigart/pyautogui) is BSD-3 with a last push in 2024-08. [xdotool](https://github.com/jordansissel/xdotool) is X11-only and broken on Wayland. AppleScript `System Events` needs an Accessibility grant.
  - From WSL2, pyautogui can reach only WSLg windows, not the Windows desktop.
  - Verdict: do not ship this in Phase 1.

## 9. NAMES

| Candidate | Collisions found | Status |
|---|---|---|
| **Conductor** | [conductor.build](https://www.conductor.build/docs/guides/parallel-agents/run-multiple-claude-code-sessions) (Melty Labs, YC) runs parallel Claude Code agents. Netflix/Orkes Conductor is a workflow engine | **Taken, same niche** |
| **Maestro** | [RunMaestro/Maestro](https://github.com/RunMaestro/Maestro) is an "agent orchestration command center" for Claude Code and Codex. There are several other Maestro orchestrators, plus mobile.dev's Maestro test tool | **Taken, same niche** |
| **Overseer** | [agent-overseer](https://github.com/troyshu/agent-overseer) manages coding agents. There is also [dmmulroy/overseer](https://github.com/dmmulroy/overseer) and the Overseerr media app | Crowded |
| **Warden** | [Warden](https://thesimonho.github.io/warden/) runs multiple agents in worktrees | **Taken, same niche** |
| **Tower** | [tower.dev](https://tower.dev/) is a Python pipeline and agent platform that owns the `tower` PyPI name. There are also [Git Tower](https://www.git-tower.com/) and [shellicar/tower](https://github.com/shellicar/tower), an "agent platform" | Taken |
| **Sentinel** | [HashiCorp Sentinel](https://developer.hashicorp.com/sentinel) and Microsoft Sentinel | Taken, and it implies security |
| Bridge | Generic and overloaded, e.g. [Tower's "bridge protocol"](https://github.com/shellicar/tower) | Weak |
| Marshal | Free on PyPI, but it collides with Python's stdlib `marshal` module | Confusing |
| Regent | Minor `regent` PyPI package (privileged tasks) | Mostly free |
| Concierge | Old PyPI SSH tool, and many "AI concierge" SaaS products | Generic |

Discovered collisions to avoid: `crowsnest` on PyPI is literally "one session that watches your other Claude Code sessions." `coxswain` is "the agent platform." `purser` is taken too.

**Three fresh names:**
1. **Chamberlain.** The officer who runs a household on the owner's behalf, which fits the "act as me" role. It is not on PyPI, and GitHub has only a few near-zero-star hits.
2. **Bosun.** The ship officer who directs the crew for the captain. The `bosun` PyPI name is a stub, and Stack Exchange's Bosun monitoring tool is archived. It should live under the CLI as `asq bosun`.
3. **Chief of Staff (`asq cos`).** A plain-English role that says "manager of managers who briefs you." It is descriptive, not trademark-able, and has zero collision risk as a subcommand.

## 10. PRIOR ART

- **[mbailey/voicemode](https://github.com/mbailey/voicemode)** (MIT, 1.4k stars, active). An MCP server that gives Claude Code voice using Whisper STT and Kokoro TTS. **Borrow its WSL2 microphone docs and its "voice is an MCP tool" pattern.** The overseer could also mount it directly.
- **[yapcode](https://github.com/nithiink/yapcode)** (MIT). One realtime voice agent drives many Claude Code sessions with tmux `send-keys` and `capture-pane`, through tools such as `tell_claude` and `start_session`. **Nearly the same product.** Borrow its tool vocabulary, its co-driving of a shared pane, and its single ordered event log.
- **[agentlabs-voice](https://github.com/patilvinay/agentlabs-voice), [claude-whisper](https://github.com/Ashton-Sidhu/claude-whisper), [voice-to-claude](https://github.com/enesbasbug/voice-to-claude), [agent-tts](https://github.com/kiliman/agent-tts).** Small push-to-talk and read-aloud tools for terminal agents. They confirm that PTT plus reading the last answer aloud is the working minimum.
- **Claude Code `/voice`.** Native since March 2026. It is push-to-talk only and is blocked on WSL. Each manager pane can already take voice this way on macOS and Windows-native.
- **[Codeman](https://github.com/Ark0N/Codeman)** (768 stars). A "mission control for AI coding agents" dashboard. Borrow its fleet-status UX ideas.
- **Home Assistant Assist and [Wyoming](https://www.home-assistant.io/integrations/wyoming/).** A pipeline of wake, STT, intent, and TTS with **each stage a separate small server behind a tiny JSON-over-TCP protocol.** This is the right model for the loose coupling asked for: interface adapters on one side, a CLI-facing brain on the other.
- **[Leon](https://github.com/leon-ai/leon)** (MIT, 17.5k stars) and [OVOS](https://github.com/OpenVoiceOS/ovos-core): skill-based assistants, too heavy to embed. [OpenInterpreter 01](https://github.com/openinterpreter/01) is AGPL with a last push in 2024-11. Do not borrow its code.

## Recommendations

### (a) Phase 1, 24 hours: text first, free voice through dictation, optional TTS

1. **Brain.** A `ClaudeSDKClient` held in a Textual worker. It mounts `aisquare serve --stdio` as MCP server `asq` with an allowlist of `mcp__asq__*`. Store the `session_id` in the board so `resume=` survives restarts. Run it from a dedicated working directory so project hooks and CLAUDE.md do not brief the overseer as if it were a worker.
2. **Contract.** The interface layer and the brain talk through one in-process API, for example `Overseer.ask(text) -> AsyncIterator[event]`. Events include `text`, `tool_call`, `needs_auth`, and `done`. The TUI panel is the first adapter. Any future voice or HTTP adapter uses the same API.
3. **Interface.** A TUI panel with a `TextArea` and a transcript. Voice input comes free from WisprFlow, Win+H, or macOS Dictation typing into it. Add a dictation-safe submit: a key press or the spoken word "send", never a bare newline.
4. **"What changed" summary.** A new MCP tool, or a brain prompt, diffs board events and pane captures since a stored watermark.
5. **Actions.** Reuse the existing tools for telling agents, spawning, and pasting loop prompts. Any privileged "act as me" tool is gated.
6. **Gate.** A TOTP code via pyotp, plus Touch ID through pyobjc on macOS where available. Grant a 15-minute token and write an audit row for each privileged action.
7. **TTS, optional and off by default.** Use OS built-ins: `say`, `powershell.exe` with System.Speech from WSL, or `spd-say`. Speak only the summary or final answer, never the tool noise.

New dependencies: `claude-agent-sdk`, plus optional extras `pyotp` and `pyobjc-framework-LocalAuthentication` on macOS.

### (b) Phase 2: native voice

- **Stack.** Microphone through `sounddevice`, then Silero VAD, then Moonshine or faster-whisper `base.en` locally. Deepgram or AssemblyAI is an optional cloud STT plugin. Then the brain, then Kokoro-onnx through RealtimeTTS sentence streaming, with barge-in that cancels TTS on speech.
- **Framework choice.** Pipecat with `LocalAudioTransport` is the upgrade path if you need turn-taking. Avoid it for the first cut.
- **On WSL2.** Ship a tiny **Windows-side voice satellite** (`pip install aisquare[voice]` on Windows Python). It owns the mic and speaker, plus Windows Hello through winrt. It talks to the WSL brain over localhost using a Wyoming-style JSON-lines protocol. This also removes the WSLg audio flakiness.
- **Wake word.** Only if requested, and use sherpa-onnx keyword spotting rather than Porcupine.

### (c) Risks

- **WSL2 audio is unreliable.** RDPSource drops out and Claude Code itself refuses voice on WSL. Mitigation: Phase 1 avoids audio entirely, and Phase 2 uses the Windows-side satellite.
- **The overseer can do damage while holding owner privileges.** Dictated or misheard commands, and prompt injection from pane captures, could trigger destructive actions. Mitigations:
  - Default-deny tool allowlist.
  - Explicit confirmation for destructive verbs.
  - Treat pane text as untrusted data.
  - A TTL on the auth token.
  - An audit log.
- **The Agent SDK depends on the `claude` CLI binary** and changes fast (v0.2.x). Pin the version, and keep headless `claude -p --resume` as the fallback.
- **Session context grows** over a long day of summaries. Plan periodic compaction, or restart with a board-derived summary.
- **License traps.** Piper and edge-tts are GPL-3.0. openWakeWord's pretrained models are non-commercial. Porcupine's free tier is gone. Keep all of them out of the core dependencies.
- **Dictation text quirks.** Formatting, trailing newlines, and clipboard clobbering come from WisprFlow. Normalize input and never auto-submit.
- **Name collisions.** Conductor, Maestro, Warden, and Overseer are already agent-fleet products. Pick Chamberlain, Bosun, or Chief of Staff.
- **Windows Hello needs a Windows-side process and an HWND.** Do not promise it in Phase 1.
8. Naming round 2 report
# Naming round 2: pop-culture names for the fleet overseer

Date: 2026-09-25.
- PyPI status comes from the PyPI JSON API. "Stub" means an old package that is unrelated, and nearly all of them are abandoned. It matters only if the overseer ships as its own package, not as `asq <name>`.
- GitHub checks searched repos whose names contain the candidate alongside "agent" or "claude".
- Existing `asq` subcommands were checked so none clash: init, status, doctor, inject, remember, why, log, login, logout, whoami, note, board, recall, launch, serve, ui, auth, agents, accounts, context, project, config, team, task, fleet, persona, explainability.

**How to read the ranking.** The overseer is addressed by voice through push-to-talk and dictation, so the name must survive speech-to-text and must not be an everyday word. "Control", "Mother", "Friday" and "Q" all fail that test. It also must not be an existing agent-fleet product.

## The owner's shortlist

| Name | Source / world | Why it fits a headset manager of managers | Spoken / command | Collisions | Verdict |
|---|---|---|---|---|---|
| **Commodore** | Naval rank above captain, commanding a squadron of ships. Also the C64 for retro gamers | The rank *is* "manager of managers": captains run ships, and the commodore runs the captains. Here the managers are the captains | "Commodore, spawn a coder on billing." / `asq commodore` | PyPI `commodore` is a 2018 stub. No agent tools found. The trademark belongs to the revived [Commodore International](https://www.timeextension.com/news/2025/12/commodore-international-says-commodore-industries-trademarks-are-invalid), which makes hardware and retro gaming and is in a live trademark fight. That is a problem only if it becomes a standalone product name | **Clear as a subcommand** |
| **Admiral** | Naval flag rank | Fits, but it outranks the owner, who should be the admiral | "Admiral, status report." / `asq admiral` | [Armada](https://armadago.ai/) names its multi-agent orchestrator component "Admiral", which is the same niche. PyPI has a 2018 stub, and R has a pharma `{admiral}` package | Collides in niche |
| **Fleet Command** | Homeworld. Karan S'jet is wired into the mothership as the fleet's voice | Iconic for strategy gamers, and literally "the voice of the fleet" | "Fleet Command, what changed?" / `asq fleet command`? | **It collides internally with `asq fleet`.** There are also [Fleet by Autonomous](https://www.autonomous.ai/fleet), a voice device that dispatches Claude Code agents, and [oguzhnatly/fleet](https://github.com/oguzhnatly/fleet), a fleet CLI for Claude Code. PyPI is free | Reject as a command. It could work as the persona's call sign |
| **Flagship** | The fleet's lead ship | It is the ship, not the officer, and it is awkward to address | "Flagship, ..." / `asq flagship` | PyPI `flagship` is Flagship.io's feature-flag SDK (v3.0.1, 2025-11), a live dev tool | Taken in dev space |
| **Number One** | Star Trek TNG: Riker, the first officer | **The owner is the captain, and Number One runs the ship for them.** "Make it so" is baked in | "Number One, tell billing to ship." / `asq one` or `asq number-one` | PyPI is free under both spellings. There are no notable repos. "Number One" is a generic phrase, and Paramount's marks cover Star Trek, not the phrase | **Clear** |
| **Skipper** | Madagascar penguins, and slang for captain | "Skipper" means the captain, which is the owner, not the overseer | "Skipper, ..." / `asq skipper` | [Zalando Skipper](https://github.com/zalando/skipper) is an HTTP router with 3.3k stars. PyPI has a 2014 stub | Wrong rank, and mild collision |
| **Helm** | The ship's steering station | Short, but it is a station rather than a character | "Helm, ..." / `asq helm` | [Kubernetes Helm](https://github.com/helm/helm) has 30k stars, and [helm-agent.com](https://helm-agent.com/) is a task board for AI coding agents | **Taken** |
| **TARS** | Interstellar's robot, with adjustable honesty and humor settings | Superb fit: a dry-witted voice and a "humor 75%" setting as a persona dial | "TARS, what changed?" / `asq tars` | [ByteDance UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop), a multimodal AI **agent** stack with 39k stars. Also [UI-TARS](https://github.com/bytedance/UI-TARS) with 11.5k stars and [Tencent Tars RPC](https://github.com/TarsCloud/Tars) with 10k stars | **Taken in AI agents** |
| **Ghost** | Destiny: the companion floating at your shoulder | The voice in your ear, a perfect companion fit | "Ghost, ..." / `asq ghost` | [Ghost CMS](https://github.com/TryGhost/Ghost) has 55k stars, and the Ghostty terminal is heavily paired with Claude Code | Taken, and it confuses terminal users |
| **EDI** | Mass Effect: the Normandy's AI, which runs ship systems and talks to Shepard | Ship AI and crew liaison. It is pronounced "Eddie", so it is easy to say | "EDI, brief me." / `asq edi` | PyPI `edi` is a 2015 stub. "EDI" also means Electronic Data Interchange, which is generic. There are no agent tools. Speech-to-text will write it as "Eddie", so accept both | **Clear, with a speech-to-text alias** |
| **Cortana** | Halo | The iconic AI in Master Chief's ear | "Cortana, ..." | **Microsoft owns it.** PyPI `cortana` says "This package name is reserved by Microsoft Corporation." It was also Windows' assistant | **Taken, as expected** |
| **Navi** | Zelda: "Hey, listen!" | Companion fit, but famous for being annoying | "Navi, ..." / `asq navi` | [denisidoro/navi](https://github.com/denisidoro/navi) is an interactive **command-line** cheatsheet tool with 17.6k stars | Taken in CLI space |
| **Wingman** | Top Gun and flight games | It is a peer, not a boss, so it is the wrong direction | "Wingman, ..." | [Wingman AI by ShipBit](https://www.wingman-ai.com/) is a **voice AI for gamers** that triggers keystrokes, exactly this product shape. There is also [claude-code-wingman](https://github.com/yossiovadia/claude-code-wingman), which orchestrates Claude Code over tmux | **Taken, same shape** |
| **Handler** | Spy fiction: the handler runs field agents | The semantics are exact, since our workers are literally "agents" | "Handler, pull the billing agent." / `asq handler` | PyPI has a 2015 stub. No agent products were found. In code, "handler" is overloaded because of event handlers | Clear, but ambiguous in docs |
| **Control** | Le Carré's head of the Circus, and also Remedy's game *Control* | Runs the spymasters, which fits | "Control, ..." | It is an everyday word, and PyPI `control` is the live python-control library. [claude-control](https://github.com/sverrirsig/claude-control) is a dashboard for multiple Claude Code agents with 133 stars | Reject |
| **Q** | Bond's quarermaster, and Star Trek's Q | It fits the tool-giver, but it is one letter | "Q, ..." | [Amazon Q Developer CLI](https://github.com/aws/amazon-q-developer-cli), an agentic terminal chat (now Kiro CLI). PyPI `q` is a popular debug tool. Speech-to-text hears "queue" or "cue" | **Taken, and unhearable** |
| **Mother** | Alien: MU-TH-UR 6000, the Nostromo's computer | Ship computer, but it betrays the crew. Bad omen for a trust-me agent | "Mother, ..." | An everyday word. There are small "mother-claude" repos | Reject |
| **Holly** | Red Dwarf: the ship computer with an IQ of 6000, now senile | Funny for fans, but the joke is incompetence | "Holly, ..." | A common first name, which is bad for dictation. PyPI has a 2023 stub | Reject |
| **KITT** | Knight Rider: the car AI in your ear | Voice-in-ear classic | "KITT, ..." | NBCUniversal holds a "KNIGHTRIDER KITT" mark ([Justia](https://trademarks.justia.com/771/63/knightrider-kitt-77163239.html)) and has [enforced the voice](https://www.iptrademarkattorney.com/trademark-attorney-knight-rider-kitt-navigation-trademark-lawsuit-license/). Speech-to-text writes "kit" | **Trademark risk** |
| **Overlord** | StarCraft Zerg overlord, and generic villainy | It controls units, but the tone reads hostile | "Overlord, ..." | Several "Claude-Overlord, Claude Agent Manager" repos, each with 0 stars | Reject on tone |
| **Warchief** | Warcraft: the Horde's leader over the clan chiefs | Chief of chiefs, a genuine manager-of-managers rank that gamers love | "Warchief, spawn a coder." / `asq warchief` | PyPI is free, and no repos were found | Clear, but combative |
| **Nexus** | Many worlds, including Blade Runner and StarCraft | Hub metaphor, no character | "Nexus, ..." | [Sonatype Nexus](https://github.com/sonatype/nexus-public), plus [nexus-agents](https://github.com/nexus-substrate/nexus-agents), a control plane for AI coding agents | **Taken in niche** |

## Additional candidates of my own

| Name | Source / world | Why it fits | Spoken / command | Collisions | Verdict |
|---|---|---|---|---|---|
| **Adama** | Battlestar Galactica: Admiral Adama commands the whole ragtag fleet | A fleet commander over ship captains, with "So say we all" as a confirm phrase | "Adama, what changed overnight?" / `asq adama` | **PyPI is free, and GitHub has no hits.** It is a distinctive word that speech-to-text handles well | **Clear** |
| **Ender** | Ender's Game: Ender commands fleets remotely through a headset, giving orders to squad leaders who command the ships | **The closest story match to this feature**: a headset-driven commander of commanders | "Ender, pull the billing lead." / `asq ender` | PyPI is free, and there are no agent repos. The risk is the author's public controversies, which some users will notice | Clear, with a reputational caveat |
| **Mazer** | Ender's Game: Mazer Rackham, the veteran voice in Ender's ear | Advisor in the ear | "Mazer, ..." / `asq mazer` | PyPI `mazer` is a deprecated Ansible Galaxy tool from 2019. It is obscure outside fans | Clear, but low recognition |
| **Normandy** | Mass Effect's ship | It is the ship, not the voice. EDI is the better choice from that world | "Normandy, ..." | PyPI has a 2021 stub | Weak |
| **Roci** | The Expanse: the Rocinante | Beloved, but it is a ship | "Roci, ..." | PyPI has `roci` and `rocinante` stubs | Weak |
| **Jarvis** | Iron Man | The canonical voice butler | "Jarvis, ..." | Marvel, [Google's Project Jarvis](https://9to5google.com/2024/10/26/google-jarvis-agent-chrome/), and dozens of claude-jarvis repos | **Taken** |
| **Friday** | Iron Man: Jarvis's successor | Same fit | "Friday, ..." | Many "Friday AI" products. It is also a weekday, and "Friday, ship it" dictates ambiguously | Reject |
| **Overwatch** | Blizzard's game, and military overwatch | A watcher over teams | "Overwatch, ..." | A Blizzard trademark, plus several "overwatch" Claude Code repos | Taken |

## Top 5, ranked

1. **Commodore** (`asq commodore`). It is the literal naval rank for commanding captains, which is what the managers are. The C64 nod works for gamers, and the name is clear in the agent space and on the command line.
2. **Number One** (`asq one`). It casts the owner as the captain and the overseer as the first officer who runs the ship. "Make it so" doubles as the confirm phrase, and the name is free everywhere.
3. **Adama** (`asq adama`). BSG's fleet admiral, commanding captains across a whole fleet. It has no collisions at all and is a unique, speech-to-text-friendly word.
4. **EDI** (`asq edi`). Mass Effect's ship AI that talks in Shepard's ear. It is short to type and clear of agent products, but it needs an "Eddie" speech-to-text alias.
5. **Handler** (`asq handler`). In spy fiction, the handler runs agents, and our workers are agents. It is clear of products but overloaded in code vocabulary.

**Honorable mentions.**
- **Ender** has the best story fit, since it is literally headset fleet command, but carries the author caveat.
- **Warchief** is chief of chiefs but combative.
- **Fleet Command** works well as the persona's *spoken call sign* even though it cannot be the subcommand.

**Avoid these for collisions:** Conductor, Maestro, Warden, Overseer (from round 1), TARS, Wingman, Helm, Q, Cortana, KITT, Navi, and Nexus.
9. Naming round 3 report
# Naming round 3: Witcher, Counter-Strike, Call of Duty, Battlefield, Age of Empires, StarCraft, Warcraft, Titanfall, Rainbow Six

Date: 2026-09-25. The checks work the same way as in round 2:
- PyPI JSON API.
- GitHub repos with the name plus "agent" or "claude".
- Known dev tools and trademarks.
- Existing `asq` subcommands. None of these names clash with one.

**The STT column** says whether a speech-to-text engine such as Whisper or WisprFlow writes the name back correctly as the first word of a sentence.
- **Good** means it is a real English word or clear letters.
- **Alias** means it needs a fuzzy-match alias list.
- **Poor** means it is regularly mangled.

| Name | World | Why it fits a headset manager of managers | Spoken / command | STT | Collisions | Verdict |
|---|---|---|---|---|---|---|
| **Adjutant** | StarCraft II: the Terran ship AI that briefs you ("Nuclear launch detected"). In real armies, the adjutant is the staff officer who relays the commander's orders to units | **Exact job description.** It briefs the commander, then passes orders down to unit leaders | "Adjutant, what changed?" / `asq adjutant` | Good | PyPI has a 2011 SC2 replay-parser stub. [OpenStack Adjutant](https://github.com/openstack/adjutant) has 23 stars and is niche. [claude-adjutant](https://github.com/chrisohalloran/claude-adjutant), an executive-assistant prompt pack with 17 stars, is small. The word itself is generic military English | **Clear** |
| **Commander** | Battlefield 2 and 4 Commander mode: sees the map, sends UAV scans, orders squads | Strong fit, and players know the role | "Commander, spawn a coder." / `asq commander` | Good | [commander.js](https://github.com/tj/commander.js) has 28k stars and is the Node CLI library. PyPI has a 2012 stub. Several claude-commander repos exist | Crowded in CLI space |
| **Overlord** | CoD MW2 command callsign, and the StarCraft Zerg overseer unit | The HQ voice on the radio | "Overlord, ..." | Good | Several small Claude-Overlord agent-manager repos. The tone is hostile | Reject (as in round 2) |
| **UAV** | CoD killstreak: "UAV online" reveals the map | It is a *recon scan*, a good name for the "what changed" feature, not the persona | "UAV." / `asq uav` | Good as letters | PyPI is free. There are drone repos. The term is generic | **Use as a verb or feature name**, for example `asq uav` for a sitrep |
| **IGL** | Counter-Strike in-game leader, who calls strats for the team | Exact fit for CS players, but opaque to everyone else | "IGL, ..." / `asq igl` | Alias ("eye gee el") | PyPI is free, with no repos | Clear, but niche |
| **Six** | Rainbow Six: "Six" is the director in your ear | Fits the voice in your ear | "Six, ..." / `asq six` | Poor (it is a number, "6") | Python's [`six`](https://github.com/benjaminp/six) library gets **865M downloads a month**. Bad in a Python tool | **Reject** |
| **BT** | Titanfall 2: BT-7274, the Vanguard titan bonded to its Pilot. "Protocol 3: protect the Pilot." "Trust me." | The owner is the Pilot, and BT is the loyal partner that does the heavy lifting. It pairs naturally with EDI: short, beloved, AI-in-your-ear | "BT, brief me." / `asq bt` | Alias ("Beatty", "B.T.") | PyPI `bt` is a live backtesting library. BT is also the British Telecom brand, and "BT" means behavior trees in game AI. It has no agent-tool collisions | Clear as a subcommand |
| **Dijkstra** | The Witcher: Sigismund Dijkstra, Redania's spymaster who runs agents | Semantically perfect as a spymaster over agents | "Dijkstra, ..." / `asq dijkstra` | **Poor** ("Dykstra", "Dexter") | Every coder knows the shortest-path algorithm, and PyPI has a 2020 stub | Reject (speech-to-text, algorithm confusion) |
| **Vesemir** | The Witcher: the old master of Kaer Morhen who trains and directs the witchers | A mentor over the monster-hunters | "Vesemir, ..." / `asq vesemir` | **Poor** | Free everywhere | Clear but hard to say or hear |
| **Price** | CoD: Captain Price, who runs Task Force 141 | A leader of operators, with "Bravo Six, going dark" | "Price, ..." | Poor (common word: "price of...") | PyPI is free | Reject |
| **Wololo** | Age of Empires: the monk's conversion chant | A meme, not a role. It converts units rather than leading them | "Wololo" | Alias | PyPI has a PyTorch model-conversion framework from 2025 | Fun easter egg, not a name |
| **Scout** | AoE's scout cavalry, and CS's Scout rifle | A recon role, not a leader | "Scout, ..." | Good | PyPI `scout` is a live sqlite search server (v4.0, 2026), and the word is widespread | Reject |
| **Executor** | StarCraft: Protoss fleet commander rank, and Tassadar's flagship | The rank fits well | "Executor, ..." | Good | `concurrent.futures.Executor` is a core Python type. PyPI has a 2020 stub and there are claude-executor repos | Reject in a Python codebase |
| **Warchief** | Warcraft: leader of the Horde over the clan chiefs | Chief of chiefs | "Warchief, spawn a coder." / `asq warchief` | Good | Free on PyPI and GitHub | **Clear** |
| **Peon** | Warcraft III: "Work, work." | It is a worker, which is the wrong direction | - | Good | [peon-ping](https://github.com/PeonPing/peon-ping) has **5k stars**: Warcraft peon voice notifications *for Claude Code* | **Taken in Claude Code space.** Also the wrong rank |
| **Ghost** | CoD: Simon "Ghost" Riley, and Destiny's Ghost | Voice in your ear | "Ghost, ..." | Good | Ghost CMS has 55k stars, and the Ghostty terminal is common among Claude Code users (round 2) | Taken |
| **Sarge / Sergeant** | Generic military: Halo's Johnson, Red vs Blue's Sarge | Runs a squad, one rank too low | "Sarge, ..." | Good | PyPI `sarge` is a live subprocess library, and `sergeant` is a task-queue library | Taken in Python |

## Additional candidates of my own

| Name | World | Why it fits | Spoken / command | STT | Collisions | Verdict |
|---|---|---|---|---|---|---|
| **Raynor** | StarCraft: Jim Raynor, commander of Raynor's Raiders, who briefs his crew on the Hyperion | A commander of commanders, beloved by fans | "Raynor, what changed?" / `asq raynor` | Alias ("Rainer", "Raynor") | **Free** on PyPI and GitHub | **Clear** |
| **Horner** | StarCraft II: Matt Horner, Raynor's second in command who runs the Hyperion's bridge | The StarCraft "Number One", running the ship for the captain | "Horner, ..." / `asq horner` | Good | Free on PyPI and GitHub | Clear, but low recognition |
| **Overmind** | StarCraft: the Zerg hive mind directing its cerebrates | A hive mind over sub-commanders | "Overmind, ..." | Good | [DarthSim/overmind](https://github.com/DarthSim/overmind) is a **tmux process manager** with 3.7k stars, the same tech space. PyPI `overmind` is an agent-optimisation package released 2026-09 | **Taken** |
| **Artanis** | StarCraft II: Hierarch who commands the Spear of Adun fleet | A fleet commander | "Artanis, ..." | Alias ("Artemis") | PyPI has a 2025 web-framework stub | Weak |
| **Thrall** | Warcraft: a Horde warchief | Fits as warchief | - | Good | The word means "slave". Bad connotation | Reject |
| **Roche** | The Witcher: Vernon Roche, commander of the Blue Stripes special forces | A special-ops commander | - | Poor ("Rosh") | The Roche pharma trademark, and PyPI `roche` is live (2026-09) | Reject |
| **Soap** | CoD: John "Soap" MacTavish | An operator, not a leader | - | Good | The SOAP protocol and a PyPI stub | Reject |

## Top 5, ranked

1. **Adjutant** (`asq adjutant`, **good STT**). It is StarCraft's briefing AI and also the real military officer who relays a commander's orders to units. That is the exact job, and nothing notable collides with it.
2. **BT** (`asq bt`, **alias STT**: "Beatty" or "B.T."). Titanfall's titan in your ear: the owner is the Pilot, and "Protocol 3: protect the Pilot" is a natural guardrail motto. It is the closest match to the owner's taste for EDI.
3. **Warchief** (`asq warchief`, **good STT**). The Warcraft chief of chiefs, free everywhere, and a word speech-to-text never gets wrong.
4. **Raynor** (`asq raynor`, **alias STT**: "Rainer"). The StarCraft commander who briefs his raiders on the Hyperion, and free everywhere.
5. **Horner** (`asq horner`, **good STT**). Raynor's second in command who runs the bridge, the StarCraft "Number One", and free everywhere.

**Feature-name ideas.** Use `asq uav` or "UAV online" for the "what changed since last update" sitrep. Use "Wololo" as an easter egg to *convert* an idle agent to a new task.

**Speech-to-text friendly.**
- **Good:** Adjutant, Warchief, Horner, Commander, UAV.
- **Needs an alias list:** BT, Raynor, EDI (from round 2), and IGL.
- **Poor:** Dijkstra, Vesemir, Six, Price, and Roche.
