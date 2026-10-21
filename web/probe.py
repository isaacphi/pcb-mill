"""Probe-region geometry: board extents from generated G-code + mesh sizing.

The generated .nc files are zero-based at the board's bottom-left corner
(mill.py re-origins during conversion), so once the user G92s the bit at the
physical board corner, job coordinates == machine coordinates and the mesh
region is just the extents shrunk by a safety margin.
"""

from __future__ import annotations

import re
from pathlib import Path

MOVE_RE = re.compile(r"^\s*G0?[01](\s|$)")
WORD_RE = re.compile(r"([XY])(-?\d+(?:\.\d+)?)")

MARGIN_MM = 1.5      # keep probe points this far inside the milling envelope
SPACING_MM = 10.0    # target distance between probe points
MAX_POINTS = 15      # per axis


def extents_from_gcode(paths: list[Path]) -> tuple[float, float, float, float] | None:
    """(min_x, min_y, max_x, max_y) over all G0/G1 moves in the given files."""
    xs: list[float] = []
    ys: list[float] = []
    for p in paths:
        for line in Path(p).read_text(errors="replace").splitlines():
            if not MOVE_RE.match(line):
                continue
            for axis, val in WORD_RE.findall(line):
                (xs if axis == "X" else ys).append(float(val))
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def mesh_params(
    extents: tuple[float, float, float, float],
    margin: float = MARGIN_MM,
    spacing: float = SPACING_MM,
    max_points: int = MAX_POINTS,
) -> tuple[float, float, float, float, int, int]:
    """(x0, y0, x1, y1, pts_x, pts_y) for PCB_MESH, shrunk by `margin`.

    Point counts target ~`spacing` mm apart, clamped to [2, max_points].
    """
    x0, y0, x1, y1 = extents
    rx0, ry0, rx1, ry1 = x0 + margin, y0 + margin, x1 - margin, y1 - margin
    if rx1 <= rx0 or ry1 <= ry0:
        raise ValueError(f"board too small for margin {margin}mm: {extents}")

    def count(a: float, b: float) -> int:
        return max(2, min(max_points, round((b - a) / spacing) + 1))

    return rx0, ry0, rx1, ry1, count(rx0, rx1), count(ry0, ry1)
