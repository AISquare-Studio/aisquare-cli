"""A tmux server the fleet starts holds no agent's process identity.

SCOPE #113 0.8.2, A12. A tmux server copies the environment of the client that starts it
into its global one, and every window opened on it inherits that. Main keeps the team
variables out (``core.spawn.TEAM_ENV_VARS``). A fleet server started from inside an
agent's tool shell also pinned that agent's process identity: Codex's ``CODEX_THREAD_ID``
(set in its tools' shells alone, measured on 0.162.1) and Claude Code's ``CLAUDE_PID``. A
``codex`` started by hand in such a window read the inherited thread and took itself for
its nested child (``core.agent_sessions.own_process``). Every tmux client runs with each
registered agent's ``tool_shell_env`` and ``pid_env`` stripped, a third agent's too.
"""

from __future__ import annotations

from dataclasses import replace

from aisquare.core.agent_adapters import register, unregister
from aisquare.core.spawn import TEAM_ENV_VARS, agent_identity_env_vars, tmux_client_env
from tests.fake_agent import FakeAgentAdapter

SHELL = {
    "AISQUARE_HOME": "/srv/aisquare",
    "PATH": "/usr/bin",
    "CODEX_THREAD_ID": "019a2b3c-4d5e-7f60-8a1b-000000000001",
    "CLAUDE_PID": "31337",
    "AISQUARE_ROLE": "coder",
}


def test_a_tmux_client_runs_with_no_agents_process_identity() -> None:
    assert {"CODEX_THREAD_ID", "CLAUDE_PID"} <= set(agent_identity_env_vars())
    env = tmux_client_env(SHELL)
    assert env == {"AISQUARE_HOME": "/srv/aisquare", "PATH": "/usr/bin"}
    assert not set(TEAM_ENV_VARS) & set(env)


def test_a_third_agents_process_identity_is_stripped_with_no_edit_here() -> None:
    fake = FakeAgentAdapter()
    fake.capabilities = replace(
        fake.capabilities, tool_shell_env="FAKE_AGENT_THREAD", pid_env="FAKE_AGENT_PID"
    )
    register(fake)
    try:
        env = tmux_client_env({**SHELL, "FAKE_AGENT_THREAD": "t-1", "FAKE_AGENT_PID": "9"})
    finally:
        unregister(fake.id)
    assert "FAKE_AGENT_THREAD" not in env and "FAKE_AGENT_PID" not in env
    assert "FAKE_AGENT_THREAD" not in agent_identity_env_vars(), "gone with the adapter"
