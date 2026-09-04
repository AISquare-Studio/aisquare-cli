"""The fleet UI (``asq`` with no arguments): a Textual app over the fleet service.

Imported LAZILY — only by ``aisquare.cli.fleet.ui`` — because textual is the one
heavy import in this package and ``aisquare hook …`` runs on every prompt of
every session. Nothing under ``aisquare.cli.ui`` may be imported at
``aisquare.cli.app`` import time. Layout: ``app`` (the two-pane shell),
``sidebar`` (Fleet ▸ projects ▸ agents ▸ Doctor), ``terminal`` (the embedded
tmux pane), ``board`` (the widgets lifted from ``cli.watch``) and ``views/``
(what the right pane shows). See docs/plans/fleet-tui.md §4-§6.
"""
