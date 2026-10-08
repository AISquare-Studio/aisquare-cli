# The README's GIF, and a test of the walkthrough it shows.
#
# Every Wait below must find its text on screen within WaitTimeout, or vhs
# exits 1 and the render fails: a step that stops working stops the GIF. Each
# text is also checked against the one file that prints it on every test run
# (tests/test_demo_tape.py, PRINTED_BY), so changed copy fails there first.
#
# Render it with `make demo`, which runs docs/demo/render.sh: this tape in the
# image docs/demo/Dockerfile builds, writing out/demo.gif, out/demo.txt and
# out/welcome.png. Not with a vhs on your own machine: the hidden opening
# sources docs/demo/seed.sh, which writes a sample project into HOME and puts a
# stand-in `claude` first on PATH, and which refuses to run outside the image.
#
# The Welcome walkthrough, from an empty home to a manager and two coders, all
# from the keyboard: asq in ~/acme-api, use that folder (init and doctor run),
# connect Claude Code, start the manager, start the coders. The agent is a
# stand-in (docs/demo/stand-in/claude), because nothing is signed in where this
# renders; it asks no trust question, so the walkthrough goes straight from the
# manager to the coders. Opening the manager's pane would need F12 to come
# back, and vhs has no F12.

Output out/demo.gif
Output out/demo.txt

Require asq
Require tmux
Require git

Set Shell "bash"
Set Width 1280
Set Height 640
Set FontSize 16
Set Padding 24
Set Framerate 24
Set TypingSpeed 75ms
Set CursorBlink false
Set WaitTimeout 60s

# Without it Textual draws in 256 colours (measured: the footer turns navy).
Env COLORTERM "truecolor"

# An empty demo home, the sample project, and the stand-in first on PATH.
Hide
Type ". docs/demo/seed.sh"
Enter
Wait+Screen@15s /seeded:/
# asq offers the folder it starts in, so it starts in the sample project.
Type "cd ~/acme-api && clear"
Enter
Wait
Show

Sleep 500ms
Type "asq"
Sleep 300ms
Enter
Wait+Screen /Pick the folder your agents will work in/
# The "Use acme-api" row comes from a worker after the first paint; until it is
# drawn, Tab would land on the path box (review of #256).
Wait+Screen /· this folder/
Sleep 2.5s

# From the sidebar, Tab reaches "Use acme-api": init and doctor run, as + does.
Tab
Sleep 600ms
Enter
Wait+Screen /Choose another/
Sleep 1.5s

# Each finished step hands the keyboard to the next one's button: Connect puts
# aisquare's hooks into the stand-in's config...
Enter
Wait+Screen /the manager gets its instructions through/
Sleep 1.5s

# ...then Start manager.
Enter
Wait+Screen /answer Claude Code's question/
Sleep 2.5s

# The keyboard is on Open the manager; the next button is Start the coders.
Tab
Sleep 600ms
Enter

# The manager and both coders are live, each in its own tmux window, the coders
# in worktrees. render.sh also checks the last snapshot shows coder-1, coder-2
# and step 2's ".../stand-in/claude", the screen's disclosure of the stand-in.
Wait+Screen /Your fleet is up/
Sleep 4s
Screenshot out/welcome.png
Sleep 1s
