# Review notes: merging 0.5.0 into the Windows CI branch

Written for the reviewer of PR #65. It covers the merge conflict resolutions,
the product changes the Windows lane uncovered, and the test ports — with the
reasoning that is not visible in the diff, and the things worth pushing back on.

Verification is at the bottom. Read that first if you only want to know whether
this is green.

---

## 1. Why this got bigger than a merge

The branch was opened to add a `windows-latest` leg to CI. While it was open,
0.5.0 landed on `main` with the explainability integration: **~130 new test
files, none of which a Windows runner had ever executed**. Merging brought them
onto a lane that runs them for the first time, and 26 were red.

Leaving them red was the first plan, and it was wrong: a lane that is red the
day it arrives is a lane the team learns to ignore, which costs exactly the
protection the lane was added for. So they are ported here.

Separately, three genuine product defects fell out of the port. Those are the
part of this change that ships to users, and they are the part most worth your
attention.

## 2. The merge conflicts (5 files)

Two were real design collisions rather than adjacent edits.

### `lifecycle.py` + `mcp_server.py` — two fixes to the same file, in opposite directions

`main` centralised `~/.aisquare/credentials` behind one read-merge-write helper,
because `init --api-key` and `serve_token()` had two formats that erased each
other. This branch, meanwhile, taught those same two writers to restrict the
file on NTFS.

Taking either side alone loses something real:

- `main`'s `store()` ends in `chmod(0o600)`, which is a no-op on NTFS — so the
  centralisation would have silently un-fixed the permission bug.
- this branch's `write_text` is a whole-file replace, which is the data loss the
  helper exists to stop.

**Resolution:** keep the single writer, and route its permission step through
`paths.restrict_to_owner`. `store()` now returns `(data, restricted)` so `init`
and `serve` can still say when the restriction did not apply, rather than
implying a guard that is not there. One writer, both facts.

> Worth challenging: `store()` returning a tuple is a slightly awkward signature.
> The alternative was a second function, which is precisely the "two callers
> agreeing by careful editing" that the helper's own docstring warns against.

### `test_delivery_bulk.py` — the stronger probe wins

`main` replaced the machine-global `pgrep -fc` daemon count with a `/proc` walk
scoped to the test's own `AISQUARE_HOME`. That is strictly better (a sibling
checkout running its own suite can no longer fail this test) and it **cannot be
reproduced on Windows**: telling *our* daemon from someone else's needs each
process's environment, and `Win32_Process` carries only the command line.

**Resolution:** took `main`'s version whole and dropped this branch's PowerShell
count, rather than reintroducing the cross-checkout flakiness `main` had just
removed. Its two self-tests are `/proc`-only for the same reason.

The remaining three (`CHANGELOG.md`, `test_harness.py`) were adjacent edits.

## 3. Product changes — the part that ships

### 3.1 A config write failed because someone was *reading* the file

**`src/aisquare/core/config.py`**

`os.replace` is atomic on POSIX and a concurrent reader keeps its own inode. On
NTFS, `MoveFileEx` refuses to replace a file that **any** other handle has open —
including one opened purely for reading — and for the width of that rename, the
reader takes an `Access is denied` of its own.

Both directions were measured under a read/write storm, not inferred:

| side | raises | `errno` | `winerror` |
|------|--------|---------|------------|
| `os.replace` over an open target | `PermissionError` | 13 | **5** or **32** |
| `Path.open` during a rename | `PermissionError` | 13 | **None** |

The reader half is the more expensive one. `cli/launch.py` treats an unreadable
config as "launch untraced" **by design**, so on Windows a config write racing a
launch silently cost tracing, with nothing raised anywhere to say so.

Both sides now go through one bounded retry helper (10 attempts, ~1.1s total,
then the original error re-raised unchanged).

> **The subtle bit, and the thing to check me on:** the two paths report
> contention differently. `Path.open` goes through the C runtime, which sets
> `errno` and leaves `winerror` as `None`. My first attempt matched on `winerror`
> only — it fixed the writer, left the reader broken, and the test still failed.
> A genuine "you may not read this" is indistinguishable from the second form, so
> it is retried too and then raised unchanged: ~1.1s on a path that was going to
> fail anyway.

### 3.2 The explainability workspace key was world-readable on Windows

**`src/aisquare/services/explainability.py`**

`store_api_key` used `chmod(0o600)`. This is the **third** secret file with this
bug, and the first to land *after* the branch fixed the other two — the
credentials file and the serve token. Now goes through
`paths.restrict_to_owner`, and warns when the restriction cannot be applied.

### 3.3 The spawn-seam registry was separator-dependent

**`src/aisquare/core/spawn.py`, `tests/test_spawn_seams.py`**

Two things, and only the first is mine:

1. `paths.restrict_to_owner` runs `icacls`, which makes it a process-spawn site.
   `main` added a guard requiring every such site to carry a written tracing
   ruling, and this branch added the site — so **all three Linux legs went red on
   the merge commit**. Registered as `EXCLUDED` ("`icacls`; no model"), in both
   the prose inventory and the machine-readable copy.
2. The guard built its keys with `str(Path)`. `SEAMS` is keyed with forward
   slashes, so on Windows *every* call site read as undecided and *every* ruling
   read as stale, against a registry that was entirely correct. Now `.as_posix()`.

## 4. Test ports — the recurring shape

Nearly every one of the 26 is the same mistake in a different costume:

> **A POSIX idiom used as a test PREMISE silently stops being one on Windows.**
> That does not fail the test. It makes the test pass for the wrong reason, or
> fail for a reason unrelated to the code under test.

`tests/fsperms.py` now owns the two that recur, and — importantly — **verifies
its own effect rather than trusting the syscall's return**:

- **`unwritable(dir)`** — `os.chmod(dir, 0o500)` is a no-op against a directory
  on Windows (Python maps the mode onto the read-only *file* attribute, which
  directories ignore). Measured: a write into a `0o500` directory succeeds.
  Applies a DENY ace there, mode bits on POSIX, then **probes with a real write**
  and raises if the denial did not take.
- **`can_symlink()`** — a *capability* probe, not a platform check. Creating a
  symlink on Windows needs `SeCreateSymbolicLinkPrivilege`, which the CI runner
  (`runneradmin`) holds and an ordinary developer account does not. The same code
  therefore passes on CI and fails on a developer box.
- **`can_deny_writes()`** — root bypasses the mode bits. Found by running the
  Ubuntu container as root, where five "this write must fail" tests passed for
  the wrong reason. CI runs as `runner`, so they still assert there.

The rest, grouped:

| Group | Cause | Fix |
|---|---|---|
| `test_config_symlink` (9), `test_config_write_failure_surface` (3) | `chmod` premise + symlink privilege | `fsperms` helpers |
| `test_gate_import_guard` (4) | `/repo/src` is absolute on POSIX, **drive-relative** on Windows — `.resolve()` attaches whatever drive the suite runs from | build fixtures absolute on the running platform |
| `test_home_filesystem_check` (4) | `/proc/self/mountinfo` parsing needs POSIX path semantics | skip the parser, and **pin the Windows answer separately** so the behaviour is asserted, not merely skipped |
| `test_explainability_env` (~11), `test_explainability` (1) | hardcoded `/bin/sh`; and `bash` on PATH | see below |
| `test_config_durable_replace` (1) | classified fds via `/proc/self/fd` | `os.fstat` + `S_ISDIR`, which is portable and strictly better |
| `test_install_provenance` (1) | asserted the POSIX spelling of a path | assert `str(Path(...))` — the property is *which directory*, not the separator |
| `test_import_cost_of_the_integration` (1) | `nturl2path` is a Windows-only stdlib import | recorded as a platform allowance, matching the file's existing `DRIFTS_BY_INTERPRETER` design |
| `test_insight_sweeper` (1) | `st_mode & 0o777 == 0o600` on NTFS | assert the ACL on Windows, mode bits on POSIX |

**The `bash` one is worth calling out.** On GitHub's `windows-latest`,
`shutil.which("bash")` finds `C:\Windows\System32\bash.exe` — the **WSL
launcher**, not a shell. With no distro installed it exits 1 having evaluated
nothing, so every parametrised "bash" case failed with an error that had nothing
to do with the quoting under test. Git Bash's real `bash.exe` is on the same
PATH. Only *running* one distinguishes them, so shell discovery is now a
functional probe.

## 5. Verification

Run before pushing, at your request. The Ubuntu legs mirror the CI `check` job
(`ruff check` → `ruff format --check` → `mypy` → `pytest -ra`) on `ubuntu:24.04`
via deadsnakes, **as a non-root user** — because root bypasses the mode bits and
would have hidden the `can_deny_writes` problem entirely.

| Lane | Result |
|---|---|
| Ubuntu 24.04, Python 3.11 | **1777 passed, 3 skipped** |
| Ubuntu 24.04, Python 3.12 | **1777 passed, 3 skipped** |
| Ubuntu 24.04, Python 3.13 | **1777 passed, 3 skipped** |
| Windows 11, Python 3.12 (local) | **1748 passed, 22 skipped** |

`ruff check`, `ruff format --check` and `mypy` pass on all four; the three
Ubuntu legs were re-run for those three steps alone to confirm it directly
rather than inferring it from the exit code.

Windows skips 22 against Ubuntu's 3. The difference is **not** deferred work:

- **12** are `can_symlink()` — this developer machine is not admin and not in
  Developer Mode. **These run on the CI runner**, which is why the Ubuntu legs
  show them passing and why that lane is the one that proves them.
- **3** are the `/proc` daemon probe, **1** the mount-table parser, **1** the
  POSIX mode-bit assertion in `test_paths` — all structural, all with a Windows
  counterpart asserted separately where one exists.
- the rest are pre-existing (`no SDK installed`).

### What is NOT proven locally

The combination of *symlink + denied directory on Windows* runs only on CI: this
machine cannot create symlinks, so those 12 skip here. Both halves are verified
independently — `tests/test_fsperms.py` exercises the Windows DENY ace and its
restoration, and the Ubuntu legs exercise the symlink logic — but the
intersection is first exercised on the runner. That is the residual risk in this
change, and it is the reason `unwritable` raises instead of yielding when its
denial does not take: if it is wrong on CI, it will say so rather than go green.
