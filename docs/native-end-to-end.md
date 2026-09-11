# Reproduce the native feature integration check

Run the deterministic process test from the repository's development environment:

```sh
.venv/bin/pytest -q tests/test_native_end_to_end.py
```

Each operation launches the actual installed `asq` console script. The test uses
a temporary Git project, an isolated `AISQUARE_HOME`, real SQLite task/brief
records, and a real pytest process that first fails and then passes. It does not
mock the CLI services. Each persona operation runs in a new process, exercising
saved settings after restart.

To judge a clean wheel installation with the same scenario:

```sh
AISQUARE_E2E_CLI=/absolute/path/to/clean-venv/bin/asq .venv/bin/pytest -q tests/test_native_end_to_end.py
```

The test-driving development environment needs pytest. The clean CLI environment
only needs the wheel and its runtime dependencies: the test command is passed an
explicit Python executable from the test-driving environment.

The story under test is deliberately small:

1. Create a local project and enable the team board and native working mode.
2. Register synthetic coder/tester identities through the real session-start
   hook. These are board records, not paid AI sessions.
3. Record two login requirements and link one ordinary coding task to them.
4. Claim the task, then prove that missing evidence prevents verification.
5. Switch Studio/Mission Control, export/import a local character, use a role
   override, switch Off and back, and read the settings from fresh processes.
6. Prove that these persona actions leave canonical task, brief and event-log
   JSON byte-for-byte unchanged.
7. Submit the coding task for review and run real pytest through `asq exec`.
   One login test fails; its original output and nonzero exit status are saved.
8. Attach the failed report, reopen the existing task, and refuse premature done.
9. Correct the source, record its changed revision, rerun pytest, and attach
   fresh successful reports for both requirements.
10. Verify the brief and mark the task done. Recover the old failed report
    without starting another command.
11. Change the source again and confirm the old proof is stale.

This is **process integration evidence**, not a claim that a real language model
planned/coded this example or that a phone/browser check ran. It makes no network
requests, installs no agent plugins, creates no cloud accounts, and sends no
Explainability traffic. Separate UI, lifecycle, packaging and provider smoke
checks cover those different boundaries.

## Package validation record — 2026-09-11

The scenario passed against both the editable checkout and a clean wheel install
on macOS arm64 / Python 3.12. The wheel was built **from the generated sdist**,
installed in a separate temporary virtual environment with runtime dependencies
only, and passed `pip check` and `asq --version` before the scenario ran. Both
Studio and Mission Control JSON packs and all native feature modules were present
in the sdist, wheel, and installed package.

The clean-install check caught an undeclared Click import that the development
environment concealed. That import was replaced with the local standard-library
editor implementation before the successful package run; no extra plugin or
Click dependency was installed to mask the problem.

The successful package snapshot's SHA-256 values were:

- Wheel: `b25af1d6cc4ea39ed30b3d9fd423a15084501d90ba2675d48c86ef5bfd121ecd`
- Sdist: `57a90b3388200f4d805ce2594efc40227cd5dbfee660adb29725f5c36f166279`

These identify that tested snapshot, not a promise that later source edits are
covered. Rebuild and repeat the clean-install check after runtime changes.
