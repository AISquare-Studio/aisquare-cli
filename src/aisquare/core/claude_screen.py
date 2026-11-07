"""Claude Code's screen reader under the name #295's writers import it by
(``services.delivery``, ``services.fleet``): :mod:`aisquare.core.agent_adapters.claude_screen`,
the one reader, so no second parser of the same pane can disagree with the captain's.

A reader of Claude Code's screen only: a writer into any agent asks the pane's own adapter
(``AgentAdapter.screen``), as ``services.captain.screen`` does.
"""

from aisquare.core.agent_adapters.claude_screen import BOX_BODY_LINES as BOX_BODY_LINES
from aisquare.core.agent_adapters.claude_screen import FOOTER_LINES as FOOTER_LINES
from aisquare.core.agent_adapters.claude_screen import INPUT_LINE as INPUT_LINE
from aisquare.core.agent_adapters.claude_screen import LIVE_SPINNER as LIVE_SPINNER
from aisquare.core.agent_adapters.claude_screen import MODAL_FOOTER as MODAL_FOOTER
from aisquare.core.agent_adapters.claude_screen import OPTION as OPTION
from aisquare.core.agent_adapters.claude_screen import PANE_ESCAPES as PANE_ESCAPES
from aisquare.core.agent_adapters.claude_screen import PROMPT_MARK as PROMPT_MARK
from aisquare.core.agent_adapters.claude_screen import RATING as RATING
from aisquare.core.agent_adapters.claude_screen import RULE as RULE
from aisquare.core.agent_adapters.claude_screen import SPINNER_LINES as SPINNER_LINES
from aisquare.core.agent_adapters.claude_screen import TAIL_LINES as TAIL_LINES
from aisquare.core.agent_adapters.claude_screen import TRUST as TRUST
from aisquare.core.agent_adapters.claude_screen import WORKING as WORKING
from aisquare.core.agent_adapters.claude_screen import YES_NO as YES_NO
from aisquare.core.agent_adapters.claude_screen import box_drawn as box_drawn
from aisquare.core.agent_adapters.claude_screen import box_idle as box_idle
from aisquare.core.agent_adapters.claude_screen import composer_text as composer_text
from aisquare.core.agent_adapters.claude_screen import dialog_region as dialog_region
from aisquare.core.agent_adapters.claude_screen import input_box_at as input_box_at
from aisquare.core.agent_adapters.claude_screen import modal_showing as modal_showing
from aisquare.core.agent_adapters.claude_screen import prompt_showing as prompt_showing
from aisquare.core.agent_adapters.claude_screen import strip_escapes as strip_escapes
from aisquare.core.agent_adapters.claude_screen import tail as tail
from aisquare.core.agent_adapters.types import ScreenPrompt as Prompt

__all__ = [
    "BOX_BODY_LINES",
    "FOOTER_LINES",
    "INPUT_LINE",
    "LIVE_SPINNER",
    "MODAL_FOOTER",
    "OPTION",
    "PANE_ESCAPES",
    "PROMPT_MARK",
    "RATING",
    "RULE",
    "SPINNER_LINES",
    "TAIL_LINES",
    "TRUST",
    "WORKING",
    "YES_NO",
    "Prompt",
    "box_drawn",
    "box_idle",
    "composer_text",
    "dialog_region",
    "input_box_at",
    "modal_showing",
    "prompt_showing",
    "strip_escapes",
    "tail",
]
