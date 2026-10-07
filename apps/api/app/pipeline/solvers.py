"""Camera-pose solvers: what they are, and which ones this deployment can run.

`estimate_poses` needs a camera trajectory for a set of photographs. There is
more than one way to get one and they are not interchangeable, so the choice is
named, recorded per run, and reported as available or not rather than assumed.

**They take different input shapes, and that is the part worth knowing.**
Structure-from-motion (COLMAP, openMVG) is built for an *unordered set* of
photographs: it matches features between every plausible pair, so the order the
files arrived in does not matter and a set of stills is exactly its use case.
Visual SLAM (ORB-SLAM2, ORB-SLAM3) is built for a *sequence*: it tracks features
from one frame to the next and leans on the assumption that consecutive frames
overlap, so it is fast and drift-prone and it degrades badly when handed a
shuffled folder. A phone recording a slow arc around a mouth is a sequence; a
folder of stills is not. Expressing this as `accepts` is what keeps "toggle
between them" from meaning "silently get a wrong answer from one of them".

**Licences are carried because one of them is not permissive.** COLMAP is
BSD-3-Clause and openMVG is MPL-2.0, both fine here. ORB-SLAM2 is GPLv3. Running
GPLv3 code as a network service is not distribution, so a hosted deployment is
unaffected; shipping a binary that links it — an on-premise appliance, a desktop
build — triggers copyleft on the whole linked work. That is a decision for
whoever chooses to deploy it, so the licence travels with the entry rather than
living in someone's memory.

Nothing here runs a solver. The implementations live in the private core, which
publishes them as entry points::

    [project.entry-points."bone_viewer.solvers"]
    colmap = "bone_viewer_core.solvers:ColmapSolver"

This module is the register of what exists, what is installed, and what ran.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

ENTRY_POINT_GROUP = "bone_viewer.solvers"


class InputShape(StrEnum):
    """What a solver will accept. A sequence is not an unordered set."""

    UNORDERED = "unordered_images"
    SEQUENCE = "image_sequence"
    DEVICE = "device_poses"


class SolverUnavailable(Exception):
    """A solver was named that this deployment cannot run. User-facing."""


@dataclass(frozen=True)
class SolverInfo:
    """A solver's identity. Describes the tool, never the private pipeline."""

    name: str
    tool: str
    description: str
    algorithm: str
    accepts: frozenset[str]
    licence: str
    reference: str | None = None
    notes: str | None = None


@dataclass
class PoseOutcome:
    """What a solver returns: poses in capture order, plus how much to trust them."""

    poses: list[dict[str, Any]]
    #: Mean reprojection error in pixels, when the solver reports one.
    residual_px: float | None = None
    #: Whether the trajectory is metric. A solved one never is.
    scale_known: bool = False
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class PoseSolver(Protocol):
    info: SolverInfo

    def available(self) -> tuple[bool, str | None]:
        """(can it run, why not when it cannot). Checked before it is asked to."""
        ...

    def solve(self, frames: list[dict[str, Any]], params: dict[str, Any]) -> PoseOutcome: ...


#: Every framework this pipeline knows how to talk to, whether or not it is
#: installed. An entry is a statement about the tool, never about this machine —
#: `available()` answers that, per deployment.
CATALOGUE: dict[str, SolverInfo] = {
    "device": SolverInfo(
        name="device",
        tool="device tracker",
        description="Takes the camera position the capture device recorded alongside each frame.",
        algorithm="visual-inertial odometry, on the device",
        accepts=frozenset({InputShape.DEVICE, InputShape.SEQUENCE}),
        licence="—",
        notes="Not solved here: the poses arrived with the photographs.",
    ),
    "colmap": SolverInfo(
        name="colmap",
        tool="COLMAP",
        description=(
            "Solves camera positions from an unordered set of photographs by "
            "matching features between them."
        ),
        algorithm="incremental structure-from-motion, then multi-view stereo",
        accepts=frozenset({InputShape.UNORDERED, InputShape.SEQUENCE}),
        licence="BSD-3-Clause",
        reference="Schönberger & Frahm, CVPR 2016",
        notes="Order does not matter, which is what makes it the fit for a folder of stills.",
    ),
    "orbslam2": SolverInfo(
        name="orbslam2",
        tool="ORB-SLAM2",
        description="Tracks camera position through a sequence of frames, keyframe by keyframe.",
        algorithm="feature-based visual SLAM, ORB descriptors",
        accepts=frozenset({InputShape.SEQUENCE}),
        licence="GPLv3",
        reference="Mur-Artal & Tardós, IEEE TRO 2017",
        notes=(
            "A sequence solver: consecutive frames must overlap, so a shuffled set of "
            "stills is not a valid input for it. GPLv3 — running it as a service is not "
            "distribution, but shipping a binary that links it is."
        ),
    ),
    "openmvg": SolverInfo(
        name="openmvg",
        tool="openMVG",
        description=(
            "Solves camera positions from an unordered set of photographs, as a "
            "pipeline of separate tools."
        ),
        algorithm="global and incremental structure-from-motion",
        accepts=frozenset({InputShape.UNORDERED, InputShape.SEQUENCE}),
        licence="MPL-2.0",
        reference="Moulon et al., CVPR 2016",
        notes="Same input shape as COLMAP; useful as a second opinion on the same set.",
    ),
    "hloc": SolverInfo(
        name="hloc",
        tool="hloc",
        description=(
            "Matches photographs by learned features before solving, for sets "
            "with little texture or a large viewpoint change."
        ),
        algorithm="hierarchical localisation with learned features, then structure-from-motion",
        accepts=frozenset({InputShape.UNORDERED, InputShape.SEQUENCE}),
        licence="Apache-2.0",
        reference="Sarlin et al., CVPR 2019",
        notes=(
            "Needs a GPU and downloaded weights; the slowest of these and the "
            "most tolerant of hard images."
        ),
    ),
}


_REGISTERED: dict[str, PoseSolver] = {}


def register(solver: PoseSolver) -> None:
    """Add an implementation. Called by entry-point discovery, or by a test.

    The implementation's own `info` is what lands in the catalogue, even when
    the name is already known. The catalogue entry for an *unimplemented*
    framework is this repo's guess at it; the implementation is the thing that
    actually knows which shapes it takes and what it is licensed under.
    """
    CATALOGUE[solver.info.name] = solver.info
    _REGISTERED[solver.info.name] = solver


def load_registered() -> list[str]:
    """Discover solvers published by other distributions. Absent is normal."""
    loaded: list[str] = []
    try:
        discovered = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        return loaded

    for entry_point in discovered:
        try:
            solver = entry_point.load()()
        except Exception:
            # A solver that will not import is not a reason to fail the app; it
            # simply does not appear as available, which is the honest answer.
            continue
        register(solver)
        loaded.append(entry_point.name)
    return loaded


def get(name: str) -> PoseSolver | None:
    return _REGISTERED.get(name)


def accepts(name: str, shape: str) -> bool:
    info = CATALOGUE.get(name)
    return info is not None and shape in info.accepts


def survey() -> list[dict[str, Any]]:
    """Every solver, whether it is installed here, and why not when it is not.

    Reported rather than inferred so a client can show which of these a given
    deployment will actually use, instead of offering a choice that fails.
    """
    rows: list[dict[str, Any]] = []
    for name, info in CATALOGUE.items():
        solver = _REGISTERED.get(name)
        if solver is None:
            available, detail = False, (
                "not installed in this deployment" if name != "device" else "not applicable"
            )
        else:
            try:
                available, detail = solver.available()
            except Exception as exc:  # a broken probe must not take the survey down
                available, detail = False, f"availability check failed: {exc}"

        rows.append(
            {
                "name": name,
                "tool": info.tool,
                "description": info.description,
                "algorithm": info.algorithm,
                "accepts": sorted(info.accepts),
                "licence": info.licence,
                "reference": info.reference,
                "notes": info.notes,
                "available": available,
                "detail": detail,
            }
        )
    return rows


#: How to say each shape in a sentence. The enum values are wire names.
SHAPE_WORDS: dict[str, str] = {
    InputShape.UNORDERED: "an unordered set of photographs",
    InputShape.SEQUENCE: "a sequence of overlapping frames",
    InputShape.DEVICE: "camera positions supplied with the capture",
}


def described(shape: str) -> str:
    return SHAPE_WORDS.get(shape, str(shape))


def shape_of(frames: Iterable[dict[str, Any]]) -> str:
    """The input shape a capture presents. Decided by the capture, not chosen."""
    return (
        InputShape.DEVICE
        if any(frame.get("pose") for frame in frames)
        else InputShape.UNORDERED
    )


def resolve(name: str | None, frames: Iterable[dict[str, Any]]) -> str | None:
    """Check a requested solver against what is installed and what it accepts.

    Returns the solver to use, or None when nothing is named and nothing is
    installed — a deployment that cannot solve poses should be able to say so
    rather than refuse to load.

    A *named* solver that cannot run because it is missing or because it does not
    take this input raises, with something the operator can act on. Falling back
    to a different solver would be worse than failing: a pose trajectory from
    somewhere other than where it was asked to come from is a wrong answer that
    looks right, and every downstream measurement inherits it.
    """
    frames = list(frames)
    shape = shape_of(frames)

    if name is None:
        # Default: whatever is installed and takes this input. `device` is never
        # registered as an implementation — a supplied pose is not solved for.
        installed = [
            candidate
            for candidate, info in CATALOGUE.items()
            if shape in info.accepts and candidate in _REGISTERED
        ]
        return installed[0] if installed else None

    if name not in CATALOGUE:
        known = ", ".join(sorted(CATALOGUE))
        raise SolverUnavailable(f"{name!r} is not a known pose solver; known: {known}")

    info = CATALOGUE[name]
    if shape not in info.accepts:
        # Spelled out rather than formatted from the enums: these strings reach
        # the operator, and `[<InputShape.SEQUENCE: 'image_sequence'>]` is a
        # repr leaking into a sentence.
        wanted = " or ".join(described(item) for item in sorted(info.accepts))
        raise SolverUnavailable(
            f"{info.tool} does not accept this input ({described(shape)}); "
            f"it takes {wanted}. Pick a solver that matches how the capture was made."
        )

    solver = _REGISTERED.get(name)
    if solver is None:
        raise SolverUnavailable(
            f"{info.tool} is not installed in this deployment. It is a known solver; "
            "no implementation is registered for it here."
        )

    available, detail = solver.available()
    if not available:
        raise SolverUnavailable(f"{info.tool} cannot run here: {detail or 'unavailable'}")
    return name
