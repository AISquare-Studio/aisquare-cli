"""Token usage, as every agent's transcript reader counts it: :class:`Usage` (a turn's
worth, or a session's when summed) and :class:`ModelUsage` (one model's requests, priced).

Each agent reads its own transcript into these (``AgentTranscript.usage``: Claude Code's
session JSONL in ``agent_adapters.claude_usage``, Codex's rollout in ``codex_usage``), priced
off its own list (``AgentAdapter.price_table``); ``services.metrics`` records and sums them
(``aisquare metrics tokens``). Main's Claude Code scanner, here before #113's lane 274-L2,
is ``claude_usage``'s now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The tool that starts a subagent: ``Task`` in Claude Code's tool list, ``Agent``
#: in the calls it writes to the transcript (2.1.309, measured).
SUBAGENT_TOOLS = frozenset({"Task", "Agent"})

#: The markers aisquare's own context is framed in. Seen anywhere but a tool's
#: OUTPUT, they mean aisquare reached the model (a tool result may be a file of
#: this repository that merely names them).
AISQUARE_MARKERS = ("<aisquare-context>", "<aisquare-team", "<aisquare-team-delta>")


@dataclass
class ModelUsage:
    """What one model was billed for across a stretch of transcript."""

    requests: int = 0
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    cost_usd: float = 0.0
    """List price of these requests; 0 for an unpriced model (see ``priced``)."""
    weighted: float = 0.0
    """Cost-weighted tokens (``core.pricing``); 0 for an unpriced model."""
    priced: bool = True

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    def add(self, other: ModelUsage) -> None:
        self.requests += other.requests
        self.input += other.input
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.cache_read += other.cache_read
        self.output += other.output
        self.cost_usd += other.cost_usd
        self.weighted += other.weighted
        self.priced = self.priced and other.priced

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "input": self.input,
            "cache_write_5m": self.cache_write_5m,
            "cache_write_1h": self.cache_write_1h,
            "cache_read": self.cache_read,
            "output": self.output,
            "cost_usd": self.cost_usd,
            "weighted": self.weighted,
            "priced": self.priced,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelUsage:
        return cls(
            requests=int(data.get("requests", 0)),
            input=int(data.get("input", 0)),
            cache_write_5m=int(data.get("cache_write_5m", 0)),
            cache_write_1h=int(data.get("cache_write_1h", 0)),
            cache_read=int(data.get("cache_read", 0)),
            output=int(data.get("output", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            weighted=float(data.get("weighted", 0.0)),
            priced=bool(data.get("priced", True)),
        )


@dataclass
class Usage:
    """What a stretch of transcript adds: a turn's worth, or a session's when summed."""

    models: dict[str, ModelUsage] = field(default_factory=dict)
    tools: dict[str, int] = field(default_factory=dict)
    """Tool calls by tool name."""
    tool_result_chars: int = 0
    lookup_calls: int = 0
    injected_chars: int = 0
    """Characters aisquare's hooks put in front of the model."""
    aisquare_traces: list[str] = field(default_factory=list)
    """Each sign that aisquare reached this session — empty for a native one."""

    @property
    def requests(self) -> int:
        return sum(m.requests for m in self.models.values())

    @property
    def tool_calls(self) -> int:
        return sum(self.tools.values())

    @property
    def subagent_calls(self) -> int:
        return sum(count for name, count in self.tools.items() if name in SUBAGENT_TOOLS)

    def totals(self) -> ModelUsage:
        """Every model summed into one; ``priced`` only when all of them are."""
        out = ModelUsage()
        for usage in self.models.values():
            out.add(usage)
        return out

    def is_empty(self) -> bool:
        return not (
            self.models
            or self.tools
            or self.tool_result_chars
            or self.injected_chars
            or self.aisquare_traces
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": {name: usage.as_dict() for name, usage in sorted(self.models.items())},
            "tools": dict(sorted(self.tools.items())),
            "tool_result_chars": self.tool_result_chars,
            "lookup_calls": self.lookup_calls,
            "injected_chars": self.injected_chars,
            "aisquare_traces": list(self.aisquare_traces),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Usage:
        models = data.get("models")
        tools = data.get("tools")
        traces = data.get("aisquare_traces")
        return cls(
            models={
                str(name): ModelUsage.from_dict(value)
                for name, value in models.items()
                if isinstance(value, dict)
            }
            if isinstance(models, dict)
            else {},
            tools={str(k): int(v) for k, v in tools.items()} if isinstance(tools, dict) else {},
            tool_result_chars=int(data.get("tool_result_chars", 0)),
            lookup_calls=int(data.get("lookup_calls", 0)),
            injected_chars=int(data.get("injected_chars", 0)),
            aisquare_traces=[str(t) for t in traces] if isinstance(traces, list) else [],
        )

    def add(self, other: Usage) -> None:
        for model, usage in other.models.items():
            self.models.setdefault(model, ModelUsage()).add(usage)
        for name, count in other.tools.items():
            self.tools[name] = self.tools.get(name, 0) + count
        self.tool_result_chars += other.tool_result_chars
        self.lookup_calls += other.lookup_calls
        self.injected_chars += other.injected_chars
        for trace in other.aisquare_traces:
            if trace not in self.aisquare_traces:
                self.aisquare_traces.append(trace)
