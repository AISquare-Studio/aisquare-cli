"""Agent providers: what this back-end can actually observe, one row per provider.

The matrix here is **descriptive**. It reports what the current CLI has evidence
for, never what a provider is imagined to support, because a capability document
that overstates is worse than one that says "pane only": the GUI renders answer
controls from it, and a control that cannot deliver is a button that lies.

Two facts shape every row, both read from ``core.agents`` rather than assumed.

**Only ``claude-code`` has a hook-installation path.** ``AgentSpec.settings_path``
is ``None`` for Cursor and Codex, and ``install_hooks`` returns ``False`` for
them on exactly that test. A provider without hooks writes no ``TeamSession``
row, so the board's ``attention`` state is unreachable for it and its state comes
from pane output timing alone.

**Hooks do not carry a dialog.** Even where they are installed, the five
installed hooks read nine payload keys between them, and none of those keys is
an option label, an option key, the on-screen order, the current selection, a
dialog kind, a tool name or plan text. So no provider is reported as supporting
structured input *from hooks*, and every option list this back-end produces is
``detected_by`` ``frame``.

``structured_input`` is therefore ``False`` for every provider at this revision.
P09 owns answering a prompt; raising that flag is its call once it can
demonstrate a delivered answer against a captured dialog, and asserting it here
would be this packet claiming another packet's result. :data:`CAPABILITY_REVISION`
exists so that change is a visible revision rather than a silent edit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from aisquare.office.models import Provider, ProviderObservation

CAPABILITY_REVISION: Final = 1
"""Bumped whenever a row below changes meaning, so consumers can pin behaviour."""

KNOWN_PROVIDERS: Final = ("claude-code", "codex", "cursor")
"""Every provider ``core.agents._specs`` registers, in a stable order."""

HOOK_CAPABLE_PROVIDERS: Final = ("claude-code",)
"""Providers whose :class:`~aisquare.core.agents.AgentSpec` has a settings path.

Not a claim that hooks *are* installed on this machine — that is a separate
question :func:`provider_capabilities` asks per run.
"""

NO_HOOK_PATH_REASON: Final = (
    "no hook install path, so no board row: state comes from pane output timing "
    "and attention is unreachable"
)

HOOKS_NOT_INSTALLED_REASON: Final = (
    "hooks are not installed for this provider here; run `aisquare agents connect` "
    "to restore lifecycle and attention evidence"
)

PANE_ONLY_OPTIONS_REASON: Final = (
    "hooks carry lifecycle and attention only; option labels, keys, order and "
    "selection exist solely in the pane"
)


def _hooks_installed(name: str) -> bool:
    """Whether aisquare's hooks are fully installed for ``name`` in the ambient home.

    Imported inside the call so that reading the matrix does not drag agent
    detection into a process that only wanted the vocabulary above, and so a
    test can pass its own probe instead of arranging a config directory.
    """
    from aisquare.core.agents import hooks_installed

    return hooks_installed(name)


def observation_for(provider: str, *, hooks_installed: bool) -> ProviderObservation:
    """How much of ``provider`` this back-end can see, given its hook state.

    ``hooks_and_pane`` requires both halves to be real: a provider with a hook
    path whose hooks are not installed is ``pane_only``, because an uninstalled
    hook produces exactly as much evidence as an absent one.
    """
    if provider not in KNOWN_PROVIDERS:
        return "unsupported"
    if provider in HOOK_CAPABLE_PROVIDERS and hooks_installed:
        return "hooks_and_pane"
    return "pane_only"


def provider_capabilities(
    probe: Callable[[str], bool] | None = None,
) -> tuple[Provider, ...]:
    """The capability matrix, derived from this machine rather than from a table.

    ``probe`` answers "are aisquare's hooks installed for this provider?"; it is
    injected so a test states the machine it means. It is the only I/O this
    function performs, and a provider with no hook path is never probed at all.
    """
    check = probe if probe is not None else _hooks_installed
    rows: list[Provider] = []
    for provider in KNOWN_PROVIDERS:
        has_path = provider in HOOK_CAPABLE_PROVIDERS
        installed = check(provider) if has_path else False
        observation = observation_for(provider, hooks_installed=installed)
        if not has_path:
            reason = NO_HOOK_PATH_REASON
        elif not installed:
            reason = HOOKS_NOT_INSTALLED_REASON
        else:
            reason = PANE_ONLY_OPTIONS_REASON
        rows.append(
            Provider(
                id=provider,
                observation=observation,
                structured_input=False,
                reason=reason,
            )
        )
    return tuple(rows)


__all__ = [
    "CAPABILITY_REVISION",
    "HOOKS_NOT_INSTALLED_REASON",
    "HOOK_CAPABLE_PROVIDERS",
    "KNOWN_PROVIDERS",
    "NO_HOOK_PATH_REASON",
    "PANE_ONLY_OPTIONS_REASON",
    "observation_for",
    "provider_capabilities",
]
