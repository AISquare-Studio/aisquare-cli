"""Adapters: the implementations of :mod:`aisquare.office.ports`.

Everything under here is allowed to import the ``office`` extra — Starlette,
uvicorn, websockets, an HTTP client — and nothing outside here is. That is the
line ``tests/office/test_foundation_imports.py`` pins: the typed foundation
(``models``, ``config``, ``ports``) and every ordinary CLI command must keep
working in a plain ``pip install aisquare-cli``.

This file therefore exports nothing and imports nothing. Re-exporting an
adapter from the package would make ``import aisquare.office.adapters`` pull an
HTTP client into a hook process on the first day someone adds a sibling module.
Import the module you need.
"""

from __future__ import annotations
