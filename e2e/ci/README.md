# e2e/ci: the nightly's CI plumbing, and running it locally with act

`.github/workflows/e2e.yml` runs the real-user suite (`e2e/PLAN.md`) on a fresh Linux
machine: nightly, by hand, and on a pull request labelled `e2e`. This directory holds the
scripts its steps call, plus the script that runs the same workflow on your own machine with
[act](https://nektosact.com).

| File | What it does |
| --- | --- |
| `install-ttyd.sh` | Installs ttyd 1.7.7, the static release binary for x86_64 or aarch64, into `/usr/local/bin`. The sha256 is pinned in the script and cross-checked against the release's `SHA256SUMS`; a mismatch with either refuses the install. |
| `setup-user.sh` | Creates `e2e`, the person the journeys play: uid 2000, a fresh home from `/etc/skel`, bash, passwordless sudo. Works as root (act) or through sudo (GitHub's `runner`). It refuses to run outside GitHub Actions or act, because it replaces `/home/e2e` and edits sudoers. |
| `act-run.sh` | Runs the workflow's Linux leg locally under act, exactly as GitHub would dispatch it, then unzips the uploaded artifact. |

## Running the workflow locally

You need Docker running and act (`brew install act`). The first run pulls
`catthehacker/ubuntu:act-24.04` (about 2.3 GB) and clones the actions; later runs reuse both.
Run it from anywhere in the checkout:

```sh
sh e2e/ci/act-run.sh --smoke             # the plumbing only, with a fake run: about 40 s
sh e2e/ci/act-run.sh                     # the real suite, track main: about 2 minutes
sh e2e/ci/act-run.sh --journeys J01      # a pytest -k expression
sh e2e/ci/act-run.sh --track release     # the README one-liner, against PyPI
sh e2e/ci/act-run.sh --keep              # keep a failed job's container to poke at
```

This is the command it runs (it prints it first and saves it as `act-command.txt`):

```sh
act workflow_dispatch --workflows .github/workflows/e2e.yml --job linux \
  --platform ubuntu-24.04=catthehacker/ubuntu:act-24.04 \
  --container-architecture linux/arm64 \
  --container-options "--shm-size=2g --init" --container-daemon-socket - \
  --artifact-server-path "$PWD/out/act-artifacts/<UTC time>-main-smoke/server" \
  --input track=main --input smoke=true --pull=false --rm \
  --local-repository "actions/upload-artifact@v7=$PWD/out/act-cache/upload-artifact@v5.0.0"
```

Each run gets its own directory, `out/act-artifacts/<UTC time>-<track>[-smoke]/` (`out/` is
gitignored):

| Path | What it holds |
| --- | --- |
| `act-command.txt` | the exact command, one argument per line |
| `act.log` | act's whole output |
| `timings.txt` | one line per step: outcome, duration, step name, as act measured them |
| `server/` | what act's artifact server stored, still zipped |
| `e2e-linux-<track>/` | the artifact, unzipped: `run.json`, `junit.xml`, `shots/`, `text/`, `video/`, `logs/`, and `report/` (`gallery.html`, `summary.md`, `step-summary.md`, `verdict.json`) |

It ends by printing the verdict and the gallery's path. The exit code is act's, so a run is
red when any step fails, including the workflow's last one, which fails on any verdict but
PASSED. The artifact is unzipped even when the job failed, because a red run is when you need
the evidence.

## Where the time goes

Measured on an Apple-silicon Mac (Docker Desktop, 10 CPUs, 8 GB), with the image already
pulled and the actions already cloned, as act reported each step:

| Step | Smoke run | Real suite |
| --- | --- | --- |
| checkout (act copies the tree) | 0.5 s | 0.7 s |
| Set up Python 3.13 | 0.4 s (11.2 s on the very first run) | 0.5 s |
| apt: fonts, sudo, curl, git, ca-certificates | 6.2 s | 8.8 s |
| setup-user.sh | 0.2 s | 0.2 s |
| install-ttyd.sh | 0.5 s | 0.5 s |
| Build the wheel | 1.3 s | 1.6 s |
| Harness venv, Playwright's Chromium and its system libraries | 20.5 s | 21.1 s |
| The fake run / the journeys | 0.5 s | 78.0 s |
| Report, step summary | 0.4 s | 0.3 s |
| Upload | 0.8 s | 0.7 s |
| Gate on the verdict | skipped | 0.1 s |
| **Wall clock, start to unzipped artifact** | **35 s** | **116 s** |

The journeys' share grows with the suite. J01 can wait up to 900 s for the installer, which
fetches uv, aisquare, tmux and gh itself; on this machine it needed 25 s.

## How act differs from GitHub

Read this before trusting a green local run, or before chasing a red one.

- **The machine.** act runs the job in a container of catthehacker's image, as root. GitHub
  runs it on a VM as `runner`, with passwordless sudo. The scripts handle both (root runs
  commands directly; `runner` goes through sudo), and the journeys' user is the same `e2e`
  account, uid 2000, in both. What is preinstalled differs: GitHub's image ships Node and `gh`
  on the system PATH, while act's has no `gh` and keeps its only Node under
  `/opt/acttoolcache`. So under act the installer really installs gh (through `sudo apt-get`,
  as the non-root user), and on GitHub it will find gh already there. Node is "already
  current" on both, because the harness puts the directory of whatever `node` it finds on
  the user's PATH (`e2e/runner/machine.py`): measured under act, J01's installer screen reads
  "already current: git node claude-code".
- **The architecture.** act runs natively: arm64 on Apple silicon. GitHub's `ubuntu-24.04` is
  x86_64. Python, ttyd, Chromium and aisquare all have arm64 builds, but fonts, rendering and
  timing differ, so compare screenshots only within one fingerprint (PLAN §9).
  `--amd64` runs x86_64 under emulation, several times slower.
- **The checkout.** act skips `actions/checkout` and copies the working tree into the
  container instead: tracked and untracked files alike, minus whatever `.gitignore` names. A
  local run therefore tests your uncommitted changes (CI tests the commit), and edits you make
  while it runs do not reach it.
- **The upload.** act's artifact server stands in for GitHub's, and it refuses uploads from
  `actions/upload-artifact` v6 and later (v6 re-encodes the signed URL, so act answers
  "unauthorized"; v7 also sends a field act rejects). `act-run.sh` therefore swaps in v5 for
  that one action (`--local-repository`, pinned by commit); act logs the swap as
  "LocalRepositoryCache matched". GitHub runs v7. `retention-days` means nothing locally.
- **Timeouts.** act enforces a step's `timeout-minutes` (the suite's 30) but only logs the
  job's (40), so a local run can outlast GitHub's limit.
- **Isolation from this machine.** By default act mounts the host's Docker socket into the
  job container. `act-run.sh` turns that off (`--container-daemon-socket -`): the job needs no
  Docker, and the product under test runs as a user with sudo, so it must not be able to reach
  the other containers on your machine.
- **/dev/shm.** Docker gives a container 64 MB, where Chromium keeps its shared memory.
  `act-run.sh` asks for 2 GB, closer to the runner VM.
- **PID 1.** act's container runs `tail -f /dev/null` as PID 1, and it never reaps the
  orphans re-parented to it. In a real run every sudo the harness started, and `asq` itself,
  stayed behind as zombies, and a zombie still answers `kill -0`, so a liveness check would
  call a dead process alive. `act-run.sh` passes `--init`, so Docker's init is PID 1 and reaps
  them, as systemd does on the VM. A probe shows 1 zombie without it and 0 with it.
- **Persistent state.** The job container is fresh every run, but act keeps
  `/opt/hostedtoolcache` in the `act-toolcache` volume, so setup-python finds Python already
  there after the first run. GitHub's runner image comes with its own pre-filled cache.
- **The image.** `act-run.sh` does not pull again when the image is present (`--pull=false`),
  so two runs an hour apart test the same machine. Run
  `docker pull catthehacker/ubuntu:act-24.04` to move forward. GitHub's image moves every week.
- **Not emulated at all.** OIDC tokens, GitHub's cache service (this workflow uses none),
  service containers, `permissions:` (act ignores them, so a missing permission only shows up
  on GitHub), `concurrency:`, and the `schedule` and `pull_request` triggers: `act-run.sh`
  always dispatches. To try the label gate, run `act pull_request -e <event.json>` by hand.
  `github.token` is empty and no secret is passed. act writes `$GITHUB_STEP_SUMMARY` to a
  file inside the container; the same text is the artifact's `report/step-summary.md`.
- **One at a time.** act's artifact server listens on this Mac's LAN address, port 34567, and
  the job container's name is fixed, so two act runs at once collide.

## Debugging a failed run

- `act.log` holds everything the job printed, and `timings.txt` shows which step failed.
- `--keep` leaves the container behind: `docker ps -a | grep act-e2e`, then
  `docker exec -it <name> bash`. The checkout is at the same path inside as on your Mac, and
  the journeys' user is `sudo -u e2e -i`. Remove it with `docker rm -f <name>` when you are
  done.
