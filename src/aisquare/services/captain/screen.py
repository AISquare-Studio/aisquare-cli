"""The captain's reader of an agent's pane: :mod:`aisquare.core.claude_screen`, under the name
``brain``, ``actions`` and their tests have always imported it by.

The reader moved to ``core`` (P0-W2) because it reads Claude Code's screen for every writer
into an agent, not for the captain alone: the one delivery door (``services.delivery``)
reads the input box with it before it types, and the fleet's listings read it for an idle
agent. It imports nothing of aisquare, so ``core`` is where it belongs.
"""

from __future__ import annotations

from aisquare.core.claude_screen import BOX_BODY_LINES as BOX_BODY_LINES
from aisquare.core.claude_screen import FOOTER_LINES as FOOTER_LINES
from aisquare.core.claude_screen import INPUT_LINE as INPUT_LINE
from aisquare.core.claude_screen import LIVE_SPINNER as LIVE_SPINNER
from aisquare.core.claude_screen import MODAL_FOOTER as MODAL_FOOTER
from aisquare.core.claude_screen import OPTION as OPTION
from aisquare.core.claude_screen import PANE_ESCAPES as PANE_ESCAPES
from aisquare.core.claude_screen import PROMPT_MARK as PROMPT_MARK
from aisquare.core.claude_screen import RATING as RATING
from aisquare.core.claude_screen import RULE as RULE
from aisquare.core.claude_screen import SPINNER_LINES as SPINNER_LINES
from aisquare.core.claude_screen import TAIL_LINES as TAIL_LINES
from aisquare.core.claude_screen import TRUST as TRUST
from aisquare.core.claude_screen import WORKING as WORKING
from aisquare.core.claude_screen import YES_NO as YES_NO
from aisquare.core.claude_screen import Prompt as Prompt
from aisquare.core.claude_screen import box_idle as box_idle
from aisquare.core.claude_screen import composer_text as composer_text
from aisquare.core.claude_screen import dialog_region as dialog_region
from aisquare.core.claude_screen import input_box_at as input_box_at
from aisquare.core.claude_screen import modal_showing as modal_showing
from aisquare.core.claude_screen import prompt_showing as prompt_showing
from aisquare.core.claude_screen import strip_escapes as strip_escapes
from aisquare.core.claude_screen import tail as tail
