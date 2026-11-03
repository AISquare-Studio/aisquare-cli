"""Whether this process runs in a macOS login session reached over the network (#20).

macOS gives every login a security session, and every process started in it inherits it: a
tmux server started from an SSH login is that login's, and so is every pane it opens, however
the client that later attaches reached the machine. Agents started in such a pane read their
Claude Code login from the Keychain, which the SSH login's session cannot read ("login
keychain is locked"), so the fleet's tmux server must not be started from one
(``services.fleet._before_a_server_starts``).

The environment cannot say it. ``SSH_CONNECTION`` is what tmux copies into a pane when a
client attaches over SSH, to a server the Mac started, whose panes read the Keychain; and a
``sudo`` strips it from an SSH login whose panes cannot. The session can:
``SessionGetInfo`` (``Security/AuthSession.h``) answers for the calling process.
"""

from __future__ import annotations

import sys

SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"

CALLER_SECURITY_SESSION = 0xFFFFFFFF
"""``callerSecuritySession``: the session of the process that asks."""

SESSION_IS_REMOTE = 0x1000
"""``sessionIsRemote``: the session was established over the network (an SSH login)."""


def remote_session() -> bool | None:
    """Whether this process's macOS security session was established over the network: an
    SSH login and everything started from it. ``None`` off macOS, or when Security.framework
    cannot be asked (nothing is refused on a guess)."""
    if sys.platform != "darwin":
        return None
    import ctypes  # macOS only, off every other platform's import

    try:
        security = ctypes.CDLL(SECURITY_FRAMEWORK)
        session = ctypes.c_uint32()
        attributes = ctypes.c_uint32()
        status = security.SessionGetInfo(
            ctypes.c_uint32(CALLER_SECURITY_SESSION),
            ctypes.byref(session),
            ctypes.byref(attributes),
        )
    except (OSError, AttributeError):
        return None
    if status != 0:
        return None
    return bool(attributes.value & SESSION_IS_REMOTE)
