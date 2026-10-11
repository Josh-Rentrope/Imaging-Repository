"""The progress registry, and the socket that carries it."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.progress import Progress, Update, job_topic


def _update(job_id: str, **fields) -> Update:
    return Update(topic=job_topic(job_id, None), task_id=job_id, **fields)


@pytest.fixture()
def loop():
    """A loop actually spinning on its own thread.

    Not `asyncio.run`. `call_soon_threadsafe` only delivers while the loop is
    running, which is exactly the property under test — a loop that never runs
    would make every one of these pass by not exercising anything.
    """
    spinning = asyncio.new_event_loop()
    thread = threading.Thread(target=spinning.run_forever, daemon=True)
    thread.start()
    try:
        yield spinning
    finally:
        spinning.call_soon_threadsafe(spinning.stop)
        thread.join(timeout=5)
        spinning.close()


def drain(loop: asyncio.AbstractEventLoop, subscriber, timeout: float = 1.0):
    """Drain on the loop's own thread, since that is where it has to run."""
    return asyncio.run_coroutine_threadsafe(subscriber.drain(timeout), loop).result(5)


# ── the registry ────────────────────────────────────────────────────────────


def test_a_subscriber_that_arrives_mid_run_is_told_where_things_are(loop):
    """The reason a late client is not left staring at nothing: every topic it
    asks for is seeded with its current value."""
    progress = Progress()
    progress.publish(_update("j1", label="Extracting", done=3, total=8))

    subscriber = progress.subscribe([job_topic("j1", None)], loop)

    assert [update.label for update in drain(loop, subscriber)] == ["Extracting"]


def test_only_the_newest_value_per_topic_is_kept(loop):
    """A bar has no history worth replaying, and latest-wins is also what stops
    a slow client making the server accumulate."""
    progress = Progress()
    subscriber = progress.subscribe([job_topic("j1", None)], loop)

    for done in range(1, 6):
        progress.publish(_update("j1", label="Extracting", done=done, total=8))

    updates = drain(loop, subscriber)
    assert len(updates) == 1
    assert updates[0].done == 5


def test_a_finished_job_keeps_its_last_value_until_it_is_deleted():
    """A client subscribing a moment after the end is still told how it ended;
    deleting the job is what bounds the map."""
    progress = Progress()
    topic = job_topic("j1", None)

    progress.finish(topic, "j1", "succeeded", "done")

    assert progress.snapshot(topic) is not None
    progress.forget(topic)
    assert progress.snapshot(topic) is None


def test_a_reporter_cannot_reach_a_different_viewer(loop):
    """Every topic is namespaced by the folder that owns it, so a job id is not
    a capability: the same id in another namespace is a different topic."""
    progress = Progress()
    progress.publish(Update(topic=job_topic("j1", "alice"), task_id="j1", label="theirs"))

    ours = progress.subscribe([job_topic("j1", "bob")], loop)

    assert drain(loop, ours) == []


def test_publishing_from_another_thread_reaches_the_loop(loop):
    """The whole reason for the registry: jobs run on threads and clients are on
    the event loop, and the loop is where the socket can be written to."""
    progress = Progress()
    subscriber = progress.subscribe([job_topic("j1", None)], loop)

    threading.Thread(
        target=lambda: progress.publish(_update("j1", label="Extracting", done=1, total=4))
    ).start()

    assert drain(loop, subscriber)[0].done == 1


# ── the socket ──────────────────────────────────────────────────────────────


def test_the_socket_says_ready_before_anything_is_published(client):
    """So a client can tell a live subscription from one that silently did
    nothing, which is otherwise indistinguishable."""
    with client.websocket_connect("/ws?topics=job:nothing-yet") as socket:
        assert socket.receive_json() == {"type": "ready", "topics": ["job:nothing-yet"]}


def test_a_published_update_arrives_on_the_socket(client):
    """Threads publish, sockets send: the two halves meeting."""
    with client.websocket_connect("/ws?topics=job:j1") as socket:
        assert socket.receive_json()["type"] == "ready"

        # The route namespaces the topic by the ambient folder, which with
        # tenancy off is no folder at all.
        client.app.state.progress.publish(
            Update(topic=job_topic("j1", None), task_id="j1", label="Extracting", done=2, total=8)
        )

        message = socket.receive_json()
        assert message["task_id"] == "j1"
        assert message["label"] == "Extracting"
        assert message["fraction"] == 0.25


def test_an_unknown_topic_is_ignored_rather_than_refused(client):
    """A name we do not serve is answered with `ready` and then nothing, so a
    client that guesses a topic shape gets silence instead of an error it has no
    way to act on."""
    with client.websocket_connect("/ws?topics=workspace:whatever") as socket:
        assert socket.receive_json() == {"type": "ready", "topics": ["workspace:whatever"]}


def test_a_job_reports_that_it_finished(client):
    """End to end through the store: a running job publishes a terminal status,
    which is what tells the page to go and read the record."""
    from app.models import JobCreate

    job = client.app.state.job_store.submit(
        JobCreate(stages=[{"op": "rectify"}], capture={"capture_id": "c-1"}),
        allow_diagnostic_server=False,
    )

    topic = job_topic(job.job_id, None)
    deadline = time.monotonic() + 30
    latest = None
    while time.monotonic() < deadline:
        latest = client.app.state.progress.snapshot(topic)
        if latest is not None and latest.status not in ("queued", "running"):
            break
        time.sleep(0.05)

    assert latest is not None, "the job never reported anything"
    assert latest.status == "succeeded"


def test_the_route_registers():
    """A guard against a module-level failure staying invisible until a browser
    opens a socket."""
    from app.routers import events

    assert [route.path for route in events.router.routes] == ["/ws"]
