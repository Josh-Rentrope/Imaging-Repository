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
