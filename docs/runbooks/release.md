# Cutting a release: what is automatic

**What this is.** What happens when a GitHub Release is published, so the
person cutting it knows what not to do by hand. The steps that are still
manual are at the bottom.

## Publishing the release does all of this

`.github/workflows/publish.yml` runs on `release: published`:

1. **build**: the sdist and the wheel, at the version in `pyproject.toml`.
2. **publish**: uploads them to PyPI by Trusted Publishing.
3. **installers** (from 0.9.0 on): `.github/workflows/installers.yml`, which
   - stamps `install.sh` and `install.ps1` to the release with
     `scripts/stamp_installers.py`. It refuses a tag that is not
     `pyproject.toml`'s version;
   - checks the stamped copies: shellcheck, shfmt, `dash -n` and BusyBox
     `ash -n`, the install.sh test suites run on the stamped copy, and
     PowerShell's parser and PSScriptAnalyzer for `install.ps1`;
   - waits until PyPI serves the version (`pip download`, up to ten minutes),
     so no installer is attached before the version it pins can be installed;
   - attaches both files to the release with `gh release upload --clobber`.

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
The copies on `main` are unstamped and install the latest release from PyPI.

## Still by hand

- Bump `version` in `pyproject.toml`, and tag `v` plus that version. The
  installers job fails on any other tag, after PyPI already has the version, so
  check this before publishing.
- Write the release notes.
- If the **installers** job fails, do not delete and recreate the release:
  PyPI will not take the same version twice. If PyPI was only slow, **re-run the
  failed job** from the Actions page. A re-run checks out the tag again, so if a
  check found a real fault, fix it on a branch cut from the tag and run
  *Release installers* from that branch with the release's tag and **upload**
  ticked.

## A dry run

Once `installers.yml` is on `main` (GitHub only offers *Run workflow* for a
workflow the default branch has): Actions → *Release installers* → *Run
workflow*, on the branch to test, with a tag equal to that branch's
`pyproject.toml` version and **upload** left off. It stamps and checks everything, waits for that version on PyPI (so use one that
is already published), and attaches nothing. Locally, the stamp alone is:

```text
python scripts/stamp_installers.py --tag vX.Y.Z --out /tmp/installers
```
