"""One module reads the sign-in credential keys, and one module speaks HTTP to the provider.

The explainability integration learned this the hard way (see
``tests/test_one_key_resolver.py``): two readers of one key drift, and the
surface that only SPEAKS about a credential ends up disagreeing with the one
that USES it. So every ``iam_*`` string literal in the package must live in
``services/iam.py``, and every caller asks that module for a ``Session``.

The second half keeps the import-cost ratchet honest by construction: the
provider client is the only new module allowed to import ``urllib.request``,
and it may do so only inside a function body.
"""

from __future__ import annotations

import ast
from pathlib import Path

from aisquare.services import iam

PACKAGE = Path(iam.__file__).resolve().parents[1]
READER = Path(iam.__file__).resolve()

#: Modules that spoke HTTP before the identity provider existed.
PRE_EXISTING_HTTP = {
    "services/explainability.py",
    "services/explainability_ops.py",
    "services/mcp_server.py",
}


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _string_constants(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_only_the_provider_client_names_the_credential_keys() -> None:
    offenders: dict[str, list[str]] = {}
    for module in _modules():
        if module == READER:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        hits = sorted({s for s in _string_constants(tree) if s.startswith("iam_")})
        if hits:
            offenders[str(module.relative_to(PACKAGE))] = hits
    assert not offenders, f"iam_* credential keys are read outside services/iam.py: {offenders}"


def test_the_reader_actually_names_them() -> None:
    """The guard above is only meaningful if the keys are string literals somewhere."""
    tree = ast.parse(READER.read_text(encoding="utf-8"))
    literals = set(_string_constants(tree))
    assert set(iam.CREDENTIAL_KEYS) <= literals
    assert len(iam.CREDENTIAL_KEYS) >= 8


def test_network_imports_stay_inside_functions() -> None:
    """``urllib.request`` and ``webbrowser`` pull in ssl/http/shlex, which the ratchet pins out."""
    heavy = {"urllib.request", "urllib.error", "http.client", "ssl", "webbrowser", "secrets"}
    offenders: list[str] = []
    for module in _modules():
        rel = str(module.relative_to(PACKAGE))
        if rel in PRE_EXISTING_HTTP:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(
                name in heavy or name.split(".")[0] in {"ssl", "webbrowser", "secrets"}
                for name in names
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"heavy imports at module scope: {offenders}"
