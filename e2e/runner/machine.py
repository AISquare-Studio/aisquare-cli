"""The user's machine: a home, a login shell and a git repo, as a fresh user has them.

Two modes, one interface:

* **local** (a developer's Mac or Linux box): a scratch HOME in a temp directory,
  never the developer's own. The suite installs aisquare INTO that home, so the
  real ``~/.aisquare``, ``~/.claude`` and ``~/.local/bin`` are never touched —
  :func:`_refuse_real_home` makes that a hard error, not a convention.
* **user** (CI, ``E2E_USER=e2e``): a dedicated non-root account with passwordless
  sudo, created by ``e2e/ci/setup-user.sh``. The shell, probes and files run as
  that user, so the installer meets a normal user's machine, sudo prompts and all.

The shell starts from ``env -i`` plus an allowlist, so nothing from the runner —
CI tokens, ``GITHUB_*``, a developer's ``CLAUDE_*`` — can reach the product or a
screenshot. What a developer's profile would add (``~/.local/bin`` on PATH, a
prompt) is written to ``~/.bashrc`` like any dotfile, plus the OSC 133 prompt
marks the terminal client counts to know a command finished and how.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: Fixed so the seeded repo has the same commit — and the project the same codename — every run.
SEED_DATE = "2026-01-01T09:00:00+00:00"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "QA Bot",
    "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
    "GIT_COMMITTER_NAME": "QA Bot",
    "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
    "GIT_AUTHOR_DATE": SEED_DATE,
    "GIT_COMMITTER_DATE": SEED_DATE,
}

#: The demo's own small repo (docs/demo/seed.sh), so the demo GIF and the E2E agree.
SEED_FILES = {
    "README.md": "# acme-api\n\nThe small HTTP service the aisquare demo onboards.\n",
    "src/acme/app.py": 'def health() -> dict[str, str]:\n    return {"status": "ok"}\n',
    "tests/test_app.py": (
        "from acme.app import health\n\n\ndef test_health() -> None:\n"
        '    assert health() == {"status": "ok"}\n'
    ),
}


_UTF8_LOCALE = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"


class RealHomeError(RuntimeError):
    """The machine would have been the developer's own home: refuse, never proceed."""


def _real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _refuse_real_home(home: Path) -> None:
    real = _real_home()
    resolved = home.resolve()
    if resolved == real or resolved in real.parents:
        raise RealHomeError(f"refusing to use {resolved} as the test machine's HOME (it is real)")


def _system_path(*, developer_node: bool) -> list[str]:
    """What the user's PATH has besides ~/.local/bin: the system dirs (and Homebrew's).

    ``developer_node`` adds the directory of the ``node`` the HARNESS sees (nvm, fnm),
    which is what a developer's own shell has. Only for local runs: in CI the user is a
    fresh account, and handing it the runner's Node (act's toolcache, the VM's) would
    let the installer report Node "already current" instead of installing it — the
    very step J01 exists to watch.
    """
    entries = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    node = shutil.which("node") if developer_node else None
    if node:  # repomix needs Node for codebase snapshots
        entries.insert(0, str(Path(node).parent))
    return [e for e in dict.fromkeys(entries) if Path(e).is_dir()]


@dataclass
class Machine:
    home: Path
    user: str | None
    tmux_tmpdir: Path
    scratch: Path | None
    """The temp dir this machine owns (local mode), removed on :meth:`close`."""

    # --- construction ----------------------------------------------------------------

    @classmethod
    def create(cls, *, agent_src: Path) -> Machine:
        user = os.environ.get("E2E_USER") or None
        if user:
            home = Path(pwd.getpwnam(user).pw_dir)
            scratch = None
            # Inside the user's home: writable by them, and short enough for a socket path.
            tmux_tmpdir = home / ".t"
        else:
            base = os.environ.get("E2E_MACHINE_BASE")
            scratch = Path(tempfile.mkdtemp(prefix="aisquare-e2e-", dir=base or None))
            home = scratch / "home"
            home.mkdir()
            # AF_UNIX paths are capped near 104 bytes on macOS, so never under a long temp dir.
            tmux_tmpdir = Path(tempfile.mkdtemp(prefix="aqe-", dir="/tmp"))
        _refuse_real_home(home)
        machine = cls(home=home, user=user, tmux_tmpdir=tmux_tmpdir, scratch=scratch)
        machine._install_agent(agent_src)
        machine._write_profile()
        machine.sh(
            f"mkdir -p {shlex.quote(str(tmux_tmpdir))} && chmod 700 {shlex.quote(str(tmux_tmpdir))}"
        )
        return machine

    # --- running things as the user ----------------------------------------------------

    @property
    def agent_dir(self) -> Path:
        return self.home / ".local" / "e2e-agent"

    def path(self) -> str:
        system = _system_path(developer_node=self.user is None)
        return ":".join([str(self.agent_dir), str(self.home / ".local" / "bin"), *system])

    def base_env(self) -> dict[str, str]:
        """The allowlisted environment of a fresh login on this machine."""
        name = self.user or pwd.getpwuid(os.getuid()).pw_name
        return {
            "HOME": str(self.home),
            "USER": name,
            "LOGNAME": name,
            "SHELL": "/bin/bash",
            "PATH": self.path(),
            "TERM": "xterm-256color",
            # macOS ships no C.UTF-8; minimal Linux images ship nothing else.
            "LANG": _UTF8_LOCALE,
            "LC_ALL": _UTF8_LOCALE,
        }

    def _as_user(self, argv: Sequence[str]) -> list[str]:
        env = [f"{k}={v}" for k, v in self.base_env().items()]
        inner = ["env", "-i", *env, *argv]
        if self.user and pwd.getpwnam(self.user).pw_uid != os.getuid():
            return ["sudo", "-n", "-u", self.user, "-H", "--", *inner]
        return inner

    def shell_argv(self) -> list[str]:
        """The command ttyd runs: this user's login shell, from a clean environment."""
        return self._as_user(["/bin/bash", "--login"])

    def sh(self, script: str, *, cwd: Path | None = None, timeout: float = 120) -> str:
        """Run ``script`` in this user's login shell (as a second terminal would) and return stdout.

        For setup and read-only probes only; journeys drive the product through the terminal.
        """
        result = subprocess.run(
            self._as_user(["/bin/bash", "-lc", script]),
            cwd=str(cwd or self.home),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, script, output=result.stdout, stderr=result.stderr
            )
        return result.stdout

    def probe(
        self, script: str, *, cwd: Path | None = None, timeout: float = 120
    ) -> tuple[int, str]:
        """Like :meth:`sh` but never raises: (exit code, stdout+stderr)."""
        result = subprocess.run(
            self._as_user(["/bin/bash", "-lc", script]),
            cwd=str(cwd or self.home),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    # --- the files a user has -------------------------------------------------------------

    def _write_as_user(self, path: Path, text: str, mode: int = 0o644) -> None:
        payload = shlex.quote(text)
        target = shlex.quote(str(path))
        self.sh(
            f"mkdir -p $(dirname {target}) && printf '%s' {payload} > {target}"
            f" && chmod {mode:o} {target}"
        )

    def _install_agent(self, agent_src: Path) -> None:
        """Put the stand-in ``claude`` where this user's PATH finds it first."""
        self._write_as_user(self.agent_dir / "claude", agent_src.read_text(encoding="utf-8"), 0o755)

    def _write_profile(self) -> None:
        tmux = shlex.quote(str(self.tmux_tmpdir))
        bashrc = "\n".join(
            [
                "# The e2e machine's dotfiles: what a developer's shell has, plus prompt marks.",
                f"export PATH={shlex.quote(self.path())}",
                "export TERM=xterm-256color COLORTERM=truecolor TZ=UTC",
                f"export TMUX_TMPDIR={tmux}",
                # Textual's animations make two shots of the same screen differ; a
                # rendering preference, not a behaviour change.
                "export TEXTUAL_ANIMATIONS=none",
                # The stand-in agent has no model behind it to answer the availability probe.
                "export AISQUARE_HARNESS_PROBE=0",
                "export HISTFILE=/dev/null",
                "shopt -s checkwinsize",
                "PS1='\\[\\e[38;5;75m\\]qa\\[\\e[0m\\]@\\[\\e[38;5;114m\\]e2e-machine\\[\\e[0m\\]"
                " \\[\\e[1;38;5;222m\\]\\w\\[\\e[0m\\] \\$ '",
                # OSC 133: D;<exit> when a command ends, A when the prompt is drawn,
                # B where the typed command starts. The client counts them.
                "PROMPT_COMMAND='__e2e_rc=$?; "
                'printf "\\033]133;D;%s\\007\\033]133;A\\007" "$__e2e_rc"\'',
                "PS1=\"$PS1\"'\\[\\033]133;B\\007\\]'",
                "",
            ]
        )
        self._write_as_user(self.home / ".bashrc", bashrc)
        self._write_as_user(self.home / ".bash_profile", "[ -f ~/.bashrc ] && . ~/.bashrc\n")
        self._write_as_user(
            self.home / ".gitconfig",
            "[user]\n\tname = QA Bot\n\temail = e2e@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n",
        )

    def seed_repo(self, name: str = "acme-api") -> Path:
        """The small git repository the user is standing in when they install aisquare."""
        root = self.home / name
        for rel, text in SEED_FILES.items():
            self._write_as_user(root / rel, text)
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in GIT_IDENTITY.items())
        self.sh(f"git init -q && git add -A && {env} git commit -qm 'Initial commit'", cwd=root)
        return root

    # --- teardown -------------------------------------------------------------------------

    def close(self) -> None:
        """Stop every tmux server this machine started, then drop its scratch dirs."""
        self.probe(
            'for s in "$TMUX_TMPDIR"/tmux-*/*; do'
            ' [ -S "$s" ] && tmux -S "$s" kill-server; done; true',
            timeout=30,
        )
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)
            shutil.rmtree(self.tmux_tmpdir, ignore_errors=True)
