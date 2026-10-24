# Cutting a release: what is automatic

**What this is.** What happens when a GitHub Release is published, so the
person cutting it knows what not to do by hand. The steps that are still
manual follow it, in order.

## Publishing the release does all of this

`.github/workflows/publish.yml` runs on `release: published`:

1. **build**: the sdist and the wheel, at the version in `pyproject.toml`.
2. **precheck** (from 0.9.0 on): `.github/workflows/installers-check.yml`,
   which stamps `install.sh` and `install.ps1` to the release with
   `scripts/stamp_installers.py` and checks the stamped copies, all before
   anything reaches PyPI:
   - the tag must be `v` plus `pyproject.toml`'s version;
   - shellcheck, shfmt, `dash -n` and BusyBox `ash -n` on `install.sh`, and the
     install.sh test suites run on the stamped copy;
   - PowerShell's parser and PSScriptAnalyzer on `install.ps1`.
3. **publish**: uploads the build to PyPI by Trusted Publishing, only once
   **build** and **precheck** have both passed.
4. **installers**: `.github/workflows/installers.yml` stamps the same files
   again, waits until PyPI serves the version (`pip download`, up to ten
   minutes) so no installer is attached before the version it pins can be
   installed, and attaches both with `gh release upload --clobber`.

**Do not attach `install.sh` or `install.ps1` by hand.** The job does it, and
`--clobber` means a rerun replaces whatever is there.

After it, these URLs work:

| | install.sh | install.ps1 |
| --- | --- | --- |
| this version | `https://github.com/AISquare-Studio/aisquare-cli/releases/download/vX.Y.Z/install.sh` | `…/releases/download/vX.Y.Z/install.ps1` |
| the latest release | `https://github.com/AISquare-Studio/aisquare-cli/releases/latest/download/install.sh` | `…/releases/latest/download/install.ps1` |

The versioned copy installs exactly X.Y.Z unless the user passes `--version`.
The latest URL follows GitHub's "Latest" release, so a **pre-release** or a
release published with *Set as the latest release* unticked does not move it.
From the moment the release is published until **installers** finishes, the
latest URLs answer 404: GitHub already calls the new release the latest, and it
has no assets yet. The copies on `main` are unstamped. They hand off to the
latest release's own installer once it is attached: main's `install.sh`
downloads it, checks its stamp against PyPI's latest version, and runs it with
the user's arguments, and main's `install.ps1` does the same inside WSL. Until
then (the 404 window above, or for good if **installers** fails), they carry on
as main's own script and install the latest release from PyPI. So the main
one-liner keeps working throughout, and says in one line which installer ran.

## Still by hand, in order

1. Bump `version` in `pyproject.toml` (and the other pins the release commit
   moves), and merge the release PR.
2. **Dry-run the checks on `main`, before publishing anything.** Actions →
   *Release installers (check)* → *Run workflow*, on `main`, with the new tag
   (for example `v0.9.0`). It stamps and runs every check **precheck** will run,
   and attaches nothing; the tag does not have to exist yet. Green here means the
   release's **precheck** will be green too. For the first release that carries
   the workflow, this is also its first live run.
3. Tag `v` plus that version and publish the release, with release notes.
4. If **precheck** fails, nothing has reached PyPI: fix the cause, delete the
   release and its tag, and publish again.
5. If **installers** fails, do not delete and recreate the release: PyPI will
   not take the same version twice. Users are not blocked meanwhile: the main
   one-liner falls back to main's own script. If PyPI was only slow, **re-run
   the failed job** from the Actions page. A re-run checks out the tag again, so if
   something else broke, fix it on a branch cut from the tag and run *Release
   installers* from that branch with the release's tag and **upload** ticked.

## Running the workflows by hand

GitHub offers *Run workflow* only for a workflow the default branch has, so
both work this way once they are on `main`:

- *Release installers (check)*: every check, on any branch whose
  `pyproject.toml` version is the tag's. Attaches nothing.
- *Release installers*: stamps, waits for that version on PyPI, and attaches
  only with **upload** ticked. Without it, it is a dry run, and its PyPI wait
  fails for a version not published yet, as it should.

Locally, the stamp alone is:

```text
python scripts/stamp_installers.py --tag vX.Y.Z --out /tmp/installers
```
