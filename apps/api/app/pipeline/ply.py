"""Binary PLY writer.

Binary little-endian rather than ASCII: an iso-surface off a CBCT runs to
hundreds of thousands of triangles, and ASCII would be roughly four times the
size and slower to parse in the browser.
"""

from __future__ import annotations

import struct

import numpy as np


def write_ply(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    """Serialise (N, 3) float vertices and (M, 3) int faces as binary PLY."""
    vertices = np.ascontiguousarray(vertices, dtype="<f4")
    faces = np.ascontiguousarray(faces, dtype="<i4")

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {len(vertices)}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        f"element face {len(faces)}\n"
        "property list uchar int vertex_indices\n"
        "end_header\n"
    ).encode("ascii")

    # Each face is a count byte followed by three 32-bit indices, so the face
    # block is written row by row rather than as one structured array.
    face_block = bytearray()
    for face in faces:
        face_block += struct.pack("<B", 3)
        face_block += face.tobytes()

    return header + vertices.tobytes() + bytes(face_block)


def read_ply(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Vertices and faces back out of a PLY this codebase wrote.

    Two dialects, because two writers emit them: `write_ply` above produces
    binary, and the fixture's procedural arch is written as ASCII so its header
    comments stay readable in a text editor. Both spell their triangles
    `property list uchar int vertex_indices`, which is what makes one reader
    worth having.

    Narrow on purpose. A general PLY parser is a lot of untested surface for a
    format with a dozen dialects, and everything this is ever handed came from
    the two functions named above.
    """
    if not data.startswith(b"ply"):
        raise ValueError("not a PLY file")

    marker = data.find(b"end_header")
    if marker < 0:
        raise ValueError("PLY has no end_header")

    header = data[:marker].decode("ascii", errors="replace")
    body = data[marker + 11 :]

    counts: dict[str, int] = {}
    ascii_format = False
    for line in header.splitlines():
        if line.startswith("format "):
            ascii_format = line.split()[1] == "ascii"
        elif line.startswith("element "):
            _element, name, count = line.split()
            counts[name] = int(count)

    vertex_count = counts.get("vertex", 0)
    face_count = counts.get("face", 0)

    if ascii_format:
        fields = body.split()
        if len(fields) < vertex_count * 3 + face_count * 4:
            raise ValueError("ASCII PLY body is shorter than its header claims")
        values = np.array(fields[: vertex_count * 3 + face_count * 4], dtype=np.float64)
        vertices = values[: vertex_count * 3].reshape(vertex_count, 3)
        rest = values[vertex_count * 3 :].reshape(face_count, 4)
        if face_count and not np.all(rest[:, 0] == 3):
            raise ValueError("PLY contains a face that is not a triangle")
        return vertices.astype(np.float32), rest[:, 1:].astype(np.int32)

    vertices = np.frombuffer(body, dtype="<f4", count=vertex_count * 3).reshape(vertex_count, 3)

    # Each face is a count byte followed by three int32 indices.
    stride = 1 + 3 * 4
    raw = np.frombuffer(
        body, dtype=np.uint8, count=face_count * stride, offset=vertex_count * 3 * 4
    ).reshape(face_count, stride)
    if face_count and not np.all(raw[:, 0] == 3):
        raise ValueError("PLY contains a face that is not a triangle")
    faces = raw[:, 1:].copy().view("<i4").reshape(face_count, 3)

    return vertices.astype(np.float32), faces.astype(np.int32)
