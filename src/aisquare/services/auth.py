"""Signing in and out: the steps around the device flow that are not UI.

The interactive part (printing the code, opening the browser, the live
countdown, the poll loop) lives in ``cli/auth.py``. This module completes a
sign-in once the token endpoint has answered, signs out, and answers "who am I"
questions from the stored session. It talks to the identity provider only
through ``services/iam.py``.
"""

from __future__ import annotations

from typing import Any

from aisquare.services import iam


def complete_sign_in(
    api_url: str, endpoints: iam.Endpoints, token_response: dict[str, Any]
) -> iam.Session:
    """Identify the user, store the session, and retire the previous token for this host."""
    access_token = str(token_response["access_token"])
    expires_in = token_response.get("expires_in")
    scope = str(token_response.get("scope") or iam.SCOPE)
    claims = iam.fetch_userinfo(endpoints, access_token)
    previous = iam.stored_session()
    session = iam.store_session(
        api_url=api_url,
        token=access_token,
        expires_in=int(expires_in) if expires_in else None,
        scope=scope,
        claims=claims,
    )
    if previous is not None and previous.token != access_token:
        # Best effort: a failure here leaves a token that expires on its own.
        iam.revoke(endpoints, previous.token)
    return session


def sign_in_with_token(api_url: str, token: str) -> iam.Session:
    """``login --with-token``: a token obtained elsewhere, checked against userinfo, then stored."""
    if not token:
        raise iam.IamError("invalid_token", "No token was read from stdin.")
    endpoints = iam.discover(api_url)
    try:
        claims = iam.fetch_userinfo(endpoints, token)
    except iam.IamError as exc:
        if exc.code == "session_expired":
            raise iam.IamError(
                "invalid_token", "That token was not accepted. It may be expired or revoked."
            ) from exc
        raise
    return iam.store_session(api_url=api_url, token=token, expires_in=None, scope="", claims=claims)


def sign_out(session: iam.Session) -> bool:
    """Revoke on the server when it can be reached, then forget locally. Returns ``revoked``."""
    revoked = False
    try:
        endpoints = iam.discover(session.api_url)
        revoked = iam.revoke(endpoints, session.token)
    except iam.IamError:
        revoked = False
    iam.clear_session()
    return revoked


def live_check(session: iam.Session) -> dict[str, Any]:
    """Ask the server whether the session still works. Never raises."""
    try:
        endpoints = iam.discover(session.api_url)
        claims = iam.fetch_userinfo(endpoints, session.token)
    except iam.IamError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message}
    return {"ok": True, "email": str(claims.get("email", "")), "sub": str(claims.get("sub", ""))}
