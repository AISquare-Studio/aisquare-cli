"""The cliXR transport: board state projected onto a websocket for a spatial client.

Four modules, one direction of dependency:

``protocol``
    Pydantic models for every message on the wire, and the JSON schema the
    browser client is written against. Nothing here imports the rest.
``projector``
    ``ContextStore`` rows -> ``protocol`` models, plus the diff between two
    snapshots. Pure: it takes a store and returns models, and opens nothing.
``speech``
    Speech-to-text. A ``Transcriber`` fed 16kHz mono PCM16 and asked for
    interim text, plus the ``FakeTranscriber`` the tests inject. Imports
    faster-whisper lazily, inside the factory, so a base install never pays
    for it. Nothing here imports the rest either.
``server``
    The Starlette app. Static files, one websocket, the poll loop, the
    fan-out to each connection, and the voice path that hands binary frames
    to ``speech`` and routes the final transcript as a prompt.

The board is read, never written, with one exception that is a board write by
construction: a ``prompt`` message hands its text to ``services.fleet.tell`` or
``services.team.add_note``, which are the same entry points the CLI uses. No
hook, no task-lifecycle call, no settings merge happens anywhere below this
package.
"""

from __future__ import annotations
