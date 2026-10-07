"""Wall-clock budgets in tests, scaled for the platform the suite runs on.

Not a test module. A budget in a test is an order of magnitude that catches the
catastrophic (an insert that waits on a lock, a quit that ignores its deadline, a
storm that convoys), not a stopwatch. A number that fits the Linux legs fails on
the Windows leg for reasons that are the runner's, not the code's: the same work
is slower there, process start-up and fsync most of all, and a loaded
windows-latest runner is slower again. Measured on the Windows leg:

- the bulk storm (``test_delivery_bulk``) took 86-143 s on eight runs and 191.7 s
  on a ninth, against its 180 s box (job 108259151826);
- twenty turn rows (``test_metrics``) took 1.12 s against their 1.0 s budget in
  job 108259170919, where that module took 135.5 s; it took 28-50 s on twelve of
  fifteen runs.

So a budget is written once, as what the Linux legs need, and ``budget`` scales it
by ``WINDOWS_FACTOR`` on Windows. The factor widens the margin for the runner; it
does not remove the bound, and each scaled budget still fails the regression it
was written for.
"""

from __future__ import annotations

import sys

WINDOWS_FACTOR = 3.0
"""How much longer the same work may take on Windows: the slowest Windows runs above
took about three times their module's usual time, and every budget scaled by this
already has room for the usual."""


def budget(seconds: float) -> float:
    """``seconds`` everywhere but Windows, and ``WINDOWS_FACTOR`` times as long there."""
    return seconds * WINDOWS_FACTOR if sys.platform == "win32" else seconds
