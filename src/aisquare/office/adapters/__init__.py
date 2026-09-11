"""Adapters: the outward-facing edges of Office.

One module per peer or platform service. Each owns its own transport, its own
decoding and its own failure mapping, and each returns the shared
:class:`~aisquare.office.models.ServiceResult` envelope so a route never has to
know which of them answered.

Nothing is re-exported here. These modules import the Office serving extra, and
a package ``__init__`` that pulled them in would drag that extra into every
``aisquare.office`` import — which is exactly what
``tests/office/test_foundation_imports.py`` pins must never happen.
"""

from __future__ import annotations
