"""One registry for supported terminal agents; adding an adapter is registration."""

from aisquare.core.agent_adapters.claude_code import ClaudeCodeAdapter
from aisquare.core.agent_adapters.codex import CodexAdapter
from aisquare.core.agent_adapters.types import AgentAdapter, executable_name

_ADAPTERS: dict[str, AgentAdapter] = {}


def register(adapter: AgentAdapter) -> None:
    if adapter.id in _ADAPTERS:
        raise ValueError(f"agent adapter already registered: {adapter.id}")
    _ADAPTERS[adapter.id] = adapter


def adapters() -> tuple[AgentAdapter, ...]:
    return tuple(_ADAPTERS.values())


def get_adapter(name: str) -> AgentAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"unknown terminal agent {name!r}; choose {', '.join(_ADAPTERS)}"
        ) from None


def adapter_for_binary(binary: str) -> AgentAdapter | None:
    name = executable_name(binary)
    return next((adapter for adapter in adapters() if name == adapter.binary), None)


register(ClaudeCodeAdapter())
register(CodexAdapter())
