"""One registry for supported terminal agents; adding an adapter is registration."""

from aisquare.core.agent_adapters.claude_code import ClaudeCodeAdapter
from aisquare.core.agent_adapters.codex import CodexAdapter
from aisquare.core.agent_adapters.types import AgentAdapter, executable_name

_ADAPTERS: dict[str, AgentAdapter] = {}

#: The agent a launch runs when nothing chooses one, and the one a row or a board
#: session from before adapters (no ``agent`` recorded) ran.
DEFAULT_AGENT = "claude-code"


def register(adapter: AgentAdapter) -> None:
    if adapter.id in _ADAPTERS:
        raise ValueError(f"agent adapter already registered: {adapter.id}")
    _ADAPTERS[adapter.id] = adapter


def unregister(name: str) -> None:
    """Take an adapter out again (a test's synthetic one)."""
    _ADAPTERS.pop(name, None)


def adapters() -> tuple[AgentAdapter, ...]:
    return tuple(_ADAPTERS.values())


def get_adapter(name: str) -> AgentAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"unknown terminal agent {name!r}; choose {', '.join(_ADAPTERS)}"
        ) from None


def default_adapter() -> AgentAdapter:
    """:data:`DEFAULT_AGENT`'s adapter: what a launch runs when nothing chooses one, and what
    an unnamed executable (a ``claude2`` binding) and a record from before adapters ran."""
    return get_adapter(DEFAULT_AGENT)


def default_binary() -> str:
    """The executable a launch runs when nothing names one: :data:`DEFAULT_AGENT`'s own."""
    return default_adapter().binary


def recorded_adapter(name: str | None) -> AgentAdapter | None:
    """The adapter a recorded ``agent`` names: :data:`DEFAULT_AGENT`'s for ``None`` (a
    record from before adapters), ``None`` for one this install does not know."""
    return _ADAPTERS.get(name or DEFAULT_AGENT)


def adapter_for_binary(binary: str) -> AgentAdapter | None:
    """The adapter whose ``binary`` is ``binary``'s name (``types.executable_name``: the
    basename, Windows shims included and a trailing separator tolerated), or ``None``: what
    a launch's own flags for an agent go by (a role's ``default_args``, ``--session-id``).

    **A wrapper script literally named ``claude`` is Claude Code**, deliberately:
    ``~/bin/claude`` and ``/opt/wrap/claude`` are the shape operators actually use to add an
    account or a flag to it, a wrapper is indistinguishable from the real thing from here,
    and the cost of being wrong is one unknown flag on a program the operator named after
    it. Anything else (``claude2``, ``claude-next``, ``aider``) is no agent's by name."""
    name = executable_name(binary)
    return next((adapter for adapter in adapters() if name == adapter.binary), None)


register(ClaudeCodeAdapter())
register(CodexAdapter())
