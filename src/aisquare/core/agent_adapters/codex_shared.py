"""What Codex's accounts share (#113's #22), as its ``AgentAccounts.share_parts`` say it.

Measured on Codex 0.164.0 and 0.162.1 in scratch homes with a loopback provider, each part
once as the account's own and once as a link to another folder:

- ``AGENTS.md`` in ``$CODEX_HOME`` is read through a link, and the request holds its text;
- ``skills`` lists the same skills linked as its own. Codex writes its bundled ``.system``
  skills into the folder it finds, so a linked one gets them in the plain home's, once, for
  every account; its links stay links;
- ``prompts``: neither version reads ``$CODEX_HOME/prompts`` at all (``/prompts:name`` is an
  "Unrecognized command" with the file in the folder itself, not linked), so there is no part
  to declare for it. A release that reads it again is one line here.

Memory has no Codex setting, so Codex has none to merge (``AgentAccounts.memory`` is ``None``).
"""

from __future__ import annotations

from aisquare.core.agent_adapters.types import SharePart

SHARE_PARTS: tuple[SharePart, ...] = (
    SharePart("agents_md", "AGENTS.md", is_dir=False),
    SharePart("skills", "skills"),
)
