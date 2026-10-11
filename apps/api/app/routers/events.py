"""The live event stream.

One socket, subscribed to named topics. Progress is the only thing that travels
on it: a result already has a durable home in the job record, and sending whole
jobs down a stream as well would give the same fact two representations free to
disagree. A client learns here that a job ended, then reads the record once.

**The viewer is resolved here, not by middleware.** `ViewerMiddleware` is
`BaseHTTPMiddleware`, which sees `http` scope only — a WebSocket upgrade never
passes through it. Left as-is, every subscription would resolve in whatever
folder happened to be ambient, which is how one viewer ends up watching
another's job. `resolve()` takes an `HTTPConnection`, so this is the same
implementation the HTTP side uses rather than a second one that can drift.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_settings
from ..progress import VIEWER_ALIAS, job_topic, viewer_topic
from ..tenancy import resolve

router = APIRouter(tags=["events"])

#: The kinds of thing a client may subscribe to. A workspace or working-set
#: topic slots in here without touching the socket or the message shape.
_KINDS = frozenset({"job", "viewer"})


def _internal(requested: str, viewer: str | None) -> str | None:
    """The internal topic a requested name maps to, or None if it is not one.

    Every topic is rewritten into the caller's namespace, so a job id belonging
    to another viewer addresses nothing. The client asks with a bare id and is
    answered with a bare id; the namespace is the server's business.
    """
    kind, _, ident = requested.partition(":")
    if not ident:
        return None
    if kind == "job":
        return job_topic(ident, viewer)
    if kind == "viewer" and ident == VIEWER_ALIAS:
        return viewer_topic(viewer)
    return None


@router.websocket("/ws")
async def stream(websocket: WebSocket) -> None:
    await websocket.accept()

    resolution = resolve(websocket, get_settings())
    if resolution.issue_cookie:
        # A viewer is minted on the first HTTP request, and there is no reliable
        # way to set that cookie during an upgrade. Refused rather than served
        # from a freshly minted folder that no other request will ever read --
        # a stream that shows nothing, for reasons invisible from the client.
        await websocket.close(code=1008, reason="no viewer yet; load the page first")
        return

    progress = websocket.app.state.progress
    requested = [
        name for name in (websocket.query_params.get("topics") or "").split(",") if name
    ]
    named = [(name, _internal(name, resolution.folder)) for name in requested]
    topics = [topic for _, topic in named if topic is not None]

    subscriber = progress.subscribe(topics, asyncio.get_running_loop())
    try:
        # The names that were *accepted*, not the ones asked for. A client whose
        # topic spelling is wrong otherwise gets `ready` and then nothing, which
        # is indistinguishable from a job that has not started -- so the
        # mismatch is reported in the one message the client is already reading.
        await websocket.send_json(
            {"type": "ready", "topics": [name for name, topic in named if topic is not None]}
        )
        while True:
            updates = await subscriber.drain()
            if not updates:
                # Nothing to say. The keepalive is also the only way a socket the
                # client has abandoned is ever noticed: an idle connection has
                # nothing else to fail on, and would sit open indefinitely.
                await websocket.send_json({"type": "ping"})
                continue
            for update in updates:
                await websocket.send_json(update.as_dict())
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # Uvicorn raises rather than the Starlette disconnect type once the
        # socket has gone; either way the subscription is all that is left to
        # clean up, and it is cleaned up below.
        pass
    finally:
        progress.unsubscribe(subscriber)
