"""Reading TotalSegmentator's progress.

The lines below are copied verbatim from a real run on this workstation, tqdm
layout and all. Recorded rather than written by hand: the point is that the
patterns match what the tool actually prints, and a line invented here would only
prove the parser agrees with itself.
"""

from __future__ import annotations

import io

import pytest

from app.pipeline import totalseg

#: The weights download, which the tool labels.
DOWNLOAD = "Downloading:  71%|#######1  | 166M/234M [00:02<00:01, 53.8MB/s]"
#: The prediction, which it does not -- so the label has to be supplied.
PREDICTING = "  3%|2         | 1/36 [00:04<02:46,  4.75s/it]"


def test_the_labelled_bar_is_named_for_what_it_is():
    found = totalseg.read_progress(DOWNLOAD)

    assert found is not None
    label, fraction, done, total = found
    assert label == "Fetching model weights"
    assert fraction == pytest.approx(0.71)
    # Bytes with SI suffixes, so there is nothing here to count against.
    assert (done, total) == (None, None)


def test_the_bare_bar_gets_the_step_it_belongs_to():
    """TotalSegmentator names the download and leaves the segmentation bare, so
    an unnamed bar is the segmentation rather than an unknown."""
    found = totalseg.read_progress(PREDICTING)

    assert found is not None
    label, fraction, done, total = found
    assert label == "Segmenting"
    assert fraction == pytest.approx(0.03)
    assert (done, total) == (1, 36)


def test_a_line_that_is_not_a_bar_yields_nothing():
    assert totalseg.read_progress("Usage: TotalSegmentator [-h]") is None
    assert totalseg.read_progress("") is None


def test_the_stream_is_split_on_carriage_returns_too():
    """tqdm redraws with `\\r`. Read as lines this arrives as one enormous line
    at the very end, which is the same as having no progress at all."""
    stream = io.StringIO("one\rtwo\r\nthree\nfour")

    assert list(totalseg._segments(stream)) == ["one", "two", "three", "four"]


def test_progress_is_only_reported_when_someone_asked(monkeypatch):
    """The callback is optional; the run must not depend on anyone listening."""
    stream = io.StringIO(f"{PREDICTING}\r{DOWNLOAD}\r")
    monkeypatch.setattr(totalseg.subprocess, "Popen", lambda *a, **k: _FakeProcess(stream))

    seen: list[tuple] = []

    def collect(label: str, fraction: float, done: int | None, total: int | None) -> None:
        seen.append((label, fraction, done, total))

    returncode, _ = totalseg._run_streaming(["anything"], collect)

    assert returncode == 0
    assert [entry[0] for entry in seen][:2] == ["Segmenting", "Fetching model weights"]
    # The end is announced, so the last phase does not stop short of it.
    assert seen[-1][1] == 1.0


# ── two bars, one progress ──────────────────────────────────────────────────

#: What a real fast run actually prints: inference over four patches, then a
#: pass over the 117 classes. Copied from a run on this workstation.
REAL_FAST_RUN = ["  0%|          | 0/4 [00:00<?, ?it/s]",
                 "100%|##########| 4/4 [00:15<00:00, 3.91s/it]",
                 "  0%|          | 0/117 [00:00<?, ?it/s]",
                 "100%|##########| 117/117 [00:01<00:00, 74.82it/s]"]


def test_a_second_bar_takes_the_next_share_rather_than_starting_over():
    """The tool's bars are unlabelled and sequential, so read on their own the
    bar fills, resets, and fills again — two completions of one segmentation."""
    phases = totalseg._Phases()
    walked = [phases.place(total, percent / 100) for total, percent in _BY_TOTAL]

    assert walked == sorted(walked)
    # Half the bar for inference, then half of what is left.
    assert [round(value, 3) for value in walked] == [0.0, 0.5, 0.5, 0.75]


def test_a_bar_that_keeps_its_length_is_the_same_phase():
    phases = totalseg._Phases()

    assert phases.place(4, 0.25) == pytest.approx(0.125)
    assert phases.place(4, 0.5) == pytest.approx(0.25)
    assert phases.place(4, 1.0) == pytest.approx(0.5)


def test_each_new_phase_takes_half_of_what_is_left():
    """So it never has to know how many phases there will be."""
    phases = totalseg._Phases()

    assert phases.place(1, 0.0) == pytest.approx(0.0)
    assert phases.place(2, 0.0) == pytest.approx(0.5)
    assert phases.place(3, 0.0) == pytest.approx(0.75)


#: `(bar length, percent)` in the order a real run prints them.
_BY_TOTAL = [(4, 0), (4, 100), (117, 0), (117, 100)]


def test_a_run_with_nobody_listening_still_returns_its_tail():
    stream = io.StringIO("the reason it failed\n")

    class Failed(_FakeProcess):
        returncode = 3

    import app.pipeline.totalseg as module

    original = module.subprocess.Popen
    module.subprocess.Popen = lambda *a, **k: Failed(stream)
    try:
        returncode, tail = module._run_streaming(["anything"], None)
    finally:
        module.subprocess.Popen = original

    assert returncode == 3
    assert "the reason it failed" in tail


class _FakeProcess:
    """Enough of a Popen for the reader: a stream, a wait, and a code."""

    returncode = 0

    def __init__(self, stream: io.StringIO) -> None:
        self.stdout = stream
        self.killed = False

    def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
