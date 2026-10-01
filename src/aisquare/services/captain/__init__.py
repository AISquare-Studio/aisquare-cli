"""The captain: the home-level agent that runs every project's fleet for the owner.

- :mod:`.actions` — the Actions MCP server the captain mounts
  (``aisquare captain serve --stdio``): the fixed tool vocabulary, one
  ``captain_action`` board event per call.
- :mod:`.state` — the runtime state the server, the CLI verbs, the voice page
  and the TUI share across processes.
- :mod:`.queue` — the attention queue: what needs the owner across every
  project, deduplicated, ranked and resolved one by one (T7).
"""
