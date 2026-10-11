"""Live progress, published from worker threads to WebSocket subscribers.

A job runs on a plain `threading.Thread`; the client watching it is on the event
loop. Starlette's WebSocket has no cross-thread API, so every publish crosses
with `loop.call_soon_threadsafe` onto a per-subscriber buffer.

**In memory only, and deliberately.** A progress value describes a run in
flight; a restart ends that run and the job record already says so. Persisting
it would be a second source of truth for something that is stale the moment it
is written — and the first thing anyone would trust over the record.

**Latest wins per topic.** A bar has no history worth replaying, so a subscriber
that fell behind sees where things are now rather than a queue of where they
were. That also means a slow client cannot make the server accumulate: the
buffer holds one value per topic however long the client takes to read.
"""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

#: How long a subscriber waits before the handler sends a keepalive. Also how a
#: dropped connection is noticed at all: with no traffic there is nothing to
#: fail, and the socket would sit open for as long as the browser kept it.
IDLE_SECONDS = 15.0


class Reporter(Protocol):
    """How a step says where it has got to.

    `total` is `None` when the step cannot know how many there are, which is a
    different thing from zero. A bar drawn against an unknown total is a lie, so
    the client draws a spinner instead — see `Update.determinate`.
    """

    def __call__(
        self,
        label: str,
        *,
        done: int | None = None,
        total: int | None = None,
        stage: str | None = None,
        overall: float | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class Update:
    """One progress value, routed internally and addressed externally.

    `topic` is the internal, viewer-namespaced key the registry routes on;
    `task_id` is what the client asked for and what it matches its rows against.
    They are separate so the stream cannot be used to watch another viewer's
    job: the route rewrites every requested topic into the caller's namespace,
    and the client never has to know the namespace exists.
    """

    topic: str
    task_id: str
    label: str = ""
    stage: str | None = None
    done: int | None = None
    total: int | None = None
    status: str = "running"
    #: Where the whole run is, when the producer can place this step within it.
    #:
    #: Set when a run knows its own phases and their relative cost, so the bar
    #: spans the job rather than resetting at every phase boundary. Absent means
    #: "the bar is this step's own progress", which is what a single-phase run
    #: wants anyway.
    overall: float | None = None

    @property
    def determinate(self) -> bool:
        return self.done is not None and bool(self.total)

    def fraction(self) -> float | None:
        if self.overall is not None:
            return min(1.0, max(0.0, self.overall))
        return min(1.0, self.done / self.total) if self.determinate else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "progress",
            "task_id": self.task_id,
            "label": self.label,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "fraction": self.fraction(),
            "status": self.status,
            "at": datetime.now(UTC).isoformat(),
        }


def job_topic(job_id: str, viewer: str | None) -> str:
    """A job's internal topic, namespaced by the folder that owns it.

    Pure, so both the worker that publishes and the route that subscribes build
    the same string from the same rule and cannot drift apart.
    """
    return f"job:{viewer or ''}/{job_id}"


def job_name(job_id: str) -> str:
    """What a client calls a job's topic. Bare, so the namespace stays internal."""
    return job_id


#: What a client asks for to reach its own viewer topic.
#:
#: A client cannot name its own folder — that is a hash of a cookie it does not
#: read — so it asks for `me` and the route resolves it. Which is also what stops
#: it naming anyone else's.
VIEWER_ALIAS = "me"

#: What a viewer-scoped update is addressed as. A download is not a job and has
#: no job id to be watched by, so it needs a name both sides can agree on
#: without one.
VIEWER_TASK = "download"


def viewer_topic(folder: str | None) -> str:
    """The topic for work that is not a job: a download, an import.

    Scoped by folder on exactly the same rule as a job topic, so one viewer's
    transfers cannot appear in another's stream.
    """
    return f"viewer:{folder or ''}"


class _Subscriber:
    """One WebSocket's view of the topics it asked for."""

    def __init__(self, loop: asyncio.AbstractEventLoop, seed: dict[str, Update]) -> None:
        self._loop = loop
        #: Internal topic -> newest update, never a history.
        self._pending: dict[str, Update] = dict(seed)
        self._wake = asyncio.Event()
        self._closed = False

    def offer(self, update: Update) -> None:
        """Called from whichever thread published. Never blocks that thread."""
        if self._closed:
            return
        self._loop.call_soon_threadsafe(self._apply, update)

    def _apply(self, update: Update) -> None:
        if self._closed:
            return
        self._pending[update.topic] = update
        self._wake.set()

    async def drain(self, timeout: float = IDLE_SECONDS) -> list[Update]:
        """The updates worth sending, or nothing once `timeout` passes.

        An empty list is the caller's cue to send a keepalive, which is also how
        a connection that has gone away is discovered — there is nothing else to
        fail on an idle socket.
        """
        if not self._pending:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                return []
        self._wake.clear()
        batch = list(self._pending.values())
        self._pending.clear()
        return batch

    def close(self) -> None:
        self._closed = True
        self._pending.clear()


class Progress:
    """The process-wide registry of who is publishing and who is watching."""

    def __init__(self) -> None:
        # Held briefly: publishing happens on the subprocess-line rate, and every
        # subscriber's buffer is touched under the same lock.
        self._lock = threading.Lock()
        self._latest: dict[str, Update] = {}
        self._subscribers: dict[str, set[_Subscriber]] = {}

    def publish(self, update: Update) -> None:
        with self._lock:
            self._latest[update.topic] = update
            targets = list(self._subscribers.get(update.topic, ()))
        for subscriber in targets:
            subscriber.offer(update)

    def forget(self, topic: str) -> None:
        """Drop a topic's last value, called when the job it describes is gone.

        Not called when a job merely ends: a client that subscribes just after
        the end should still be told how it ended, and the live jobs are what
        bound this anyway.
        """
        with self._lock:
            self._latest.pop(topic, None)

    def snapshot(self, topic: str) -> Update | None:
        """Where a topic currently is, or None if nothing has reported on it."""
        with self._lock:
            return self._latest.get(topic)

    def subscribe(
        self, topics: list[str], loop: asyncio.AbstractEventLoop
    ) -> _Subscriber:
        """Watch `topics`, starting from wherever each one currently is.

        Seeded under the lock so an update published between subscribing and the
        first send is either in the seed or in the buffer, never in neither.
        """
        with self._lock:
            seed = {topic: self._latest[topic] for topic in topics if topic in self._latest}
            subscriber = _Subscriber(loop, seed)
            for topic in topics:
                self._subscribers.setdefault(topic, set()).add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            for topic in list(self._subscribers):
                watchers = self._subscribers[topic]
                watchers.discard(subscriber)
                if not watchers:
                    del self._subscribers[topic]
        subscriber.close()

    def reporter(self, topic: str, task_id: str) -> Reporter:
        """A reporter bound to one topic, for one run."""

        def report(
            label: str,
            *,
            done: int | None = None,
            total: int | None = None,
            stage: str | None = None,
            overall: float | None = None,
        ) -> None:
            self.publish(
                Update(
                    topic=topic,
                    task_id=task_id,
                    label=label,
                    stage=stage,
                    done=done,
                    total=total,
                    overall=overall,
                )
            )

        return report

    def finish(self, topic: str, task_id: str, status: str, label: str = "") -> None:
        """Send the terminal update.

        The value stays in `_latest` rather than being dropped, so a client that
        subscribes a moment after the job ended still learns how it ended instead
        of watching a topic that has no history and never will. What actually
        bounds it is the job being deleted, which is where it is removed.
        """
        self.publish(Update(topic=topic, task_id=task_id, label=label, status=status))


#: The reporter in scope for the work running on this thread.
#:
#: A context variable rather than an argument threaded through every call,
#: because a bare `threading.Thread` starts with a fresh context and the job
#: worker is exactly that — the same reason `current_folder` is set the same
#: way, one module over. It is read once, in `ServerBackend.run`, and travels
#: into the core as an ordinary entry in the context dict it already builds.
current_reporter: ContextVar[Reporter | None] = ContextVar(
    "bone_viewer_progress", default=None
)
