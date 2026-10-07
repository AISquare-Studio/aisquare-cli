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
# Today's flow from an empty home: open asq, onboard the sample project
# ~/acme-api, start its manager. The agent is a stand-in
# (docs/demo/bin/claude), because nothing is signed in where this renders.

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
Type "clear"
Enter
Wait
Show

Sleep 500ms
Type "asq"
Sleep 300ms
Enter
Wait+Screen /aisquare fleet/
Sleep 3s

# The + beside Fleet is the first row the cursor reaches.
Down
Sleep 600ms
Enter
Wait+Screen /Onboard a project/
Sleep 1.5s

# Past the directory tree to the path box.
Tab
Tab
Sleep 400ms
Type "~/acme-api"
Wait+Screen /will register/
Sleep 1.5s
Enter

# init and doctor run, then the project opens on its Manager tab.
Wait+Screen /has no manager yet/
Sleep 2.5s

# Focus is still on the Onboard view's path box, hidden now, so Tab starts
# again from the top: the sidebar, the tab strip, then Start manager. Measured;
# Shift+Tab lands on the sidebar too, not on the button.
Tab
Sleep 300ms
Tab
Sleep 300ms
Tab
Sleep 600ms
Enter

# The manager's window opens in tmux and runs the stand-in. The hold lets the
# spawn toast time out (6 s), so the still shows the fleet and not the toast.
Wait+Screen /stand-in agent/
Sleep 6.5s
Screenshot out/welcome.png
Sleep 1s
