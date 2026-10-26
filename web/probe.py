"""Probe-region geometry: cut extents from generated G-code, size validation,
and the mesh plan handed to Klipper's PCB_MESH.

The generated .nc files are zero-based at the board's bottom-left corner
(mill.py re-origins during conversion). Once the board corner is saved
(SET_BOARD_ORIGIN) job coordinates + origin = machine coordinates, so the
mesh region is the *cut* extents shrunk by a margin, shifted by the origin.

All probing itself runs inside Klipper (PCB_DATUM / PCB_MESH macros); see
klipper/printer.cfg for the safety design.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from pathlib import Path

MOVE_RE = re.compile(r"^\s*G0?[01](\s|$)")
WORD_RE = re.compile(r"([XY])(-?\d+(?:\.\d+)?)")

BOARD_MAX_MM = 100.0   # the bed pocket: board outline must fit
CUT_MAX_MM = 97.0      # area that gets milled/probed: margin from the board edge
EDGE_TOOL_MM = 0.8     # edge.nc is the tool-centre path, one bit wider than the board
MARGIN_MM = 1.5        # probe points this far inside the cut extents
SPACING_MM = 10.0      # max distance between probe points
MIN_POINTS = 4         # per axis (bicubic mesh needs >= 4)
MAX_POINTS = 12        # per axis
SECONDS_PER_POINT = 3.5

# Machine travel (stepper_x/y position_max in klipper/printer.cfg) minus a
# 1 mm guard. The bed is bigger than the pocket, but Klipper refuses moves
# beyond position_max.
MACHINE_MAX = (119.0, 119.0)
MACHINE_MIN = (-3.0, -3.0)

Extents = tuple[float, float, float, float]


def extents_from_gcode(paths: list[Path]) -> Extents | None:
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


def size_of(ext: Extents) -> tuple[float, float]:
    return ext[2] - ext[0], ext[3] - ext[1]


def validate_job(cut: Extents | None, edge: Extents | None = None) -> str | None:
    """Error text if the job can't be milled in the bed pocket, else None."""
    if cut is None:
        return "no cut paths found in the generated G-code"
    w, h = size_of(cut)
    if w > CUT_MAX_MM or h > CUT_MAX_MM:
        return (f"cut area is {w:.1f} × {h:.1f} mm; the limit is {CUT_MAX_MM:.0f} × "
                f"{CUT_MAX_MM:.0f} mm (board must fit the {BOARD_MAX_MM:.0f} mm pocket "
                "with margin) — shrink the board")
    if edge is not None:
        bw, bh = size_of(edge)
        lim = BOARD_MAX_MM + EDGE_TOOL_MM
        if bw > lim or bh > lim:
            return (f"board outline is {bw - EDGE_TOOL_MM:.1f} × {bh - EDGE_TOOL_MM:.1f} mm; "
                    f"max is {BOARD_MAX_MM:.0f} × {BOARD_MAX_MM:.0f} mm")
    return None


@dataclass
class MeshPlan:
    x0: float          # MACHINE coordinates
    y0: float
    x1: float
    y1: float
    nx: int
    ny: int
    seconds: int
    fits: bool
    problem: str | None

    def as_dict(self) -> dict:
        return asdict(self)

    def gcode(self) -> str:
        return (f"PCB_MESH X0={self.x0:.3f} Y0={self.y0:.3f} X1={self.x1:.3f} "
                f"Y1={self.y1:.3f} PTSX={self.nx} PTSY={self.ny} SAMPLES=1")


def _count(span: float, spacing: float) -> int:
    return max(MIN_POINTS, min(MAX_POINTS, math.ceil(span / spacing - 1e-9) + 1))


def mesh_plan(
    cut: Extents,
    origin: tuple[float, float],
    margin: float = MARGIN_MM,
    spacing: float = SPACING_MM,
) -> MeshPlan:
    """Mesh region (machine coords) over the cut extents, shifted by `origin`.

    `fits` is False when any point would be outside machine travel; `problem`
    says by how much, so the UI can tell the user to move the board origin.
    """
    ox, oy = origin
    x0, y0, x1, y1 = cut[0] + margin, cut[1] + margin, cut[2] - margin, cut[3] - margin
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"cut area too small for margin {margin} mm: {cut}")
    mx0, my0, mx1, my1 = x0 + ox, y0 + oy, x1 + ox, y1 + oy
    nx, ny = _count(x1 - x0, spacing), _count(y1 - y0, spacing)

    problem = None
    over_x, over_y = mx1 - MACHINE_MAX[0], my1 - MACHINE_MAX[1]
    if over_x > 0 or over_y > 0 or mx0 < MACHINE_MIN[0] or my0 < MACHINE_MIN[1]:
        bits = []
        if over_x > 0:
            bits.append(f"{over_x:.1f} mm past X travel")
        if over_y > 0:
            bits.append(f"{over_y:.1f} mm past Y travel")
        if mx0 < MACHINE_MIN[0] or my0 < MACHINE_MIN[1]:
            bits.append("below X/Y minimum")
        problem = ("with the board origin at machine "
                   f"{ox:.1f}, {oy:.1f} the mesh is " + ", ".join(bits) +
                   " — move the board origin toward the bed's X/Y zero")
    return MeshPlan(round(mx0, 3), round(my0, 3), round(mx1, 3), round(my1, 3), nx, ny,
                    int(nx * ny * SECONDS_PER_POINT), problem is None, problem)


# ---------------------------------------------------------------------------
# G-code header: refuse to cut without a datum + mesh that cover this job
# ---------------------------------------------------------------------------
def ready_header(cut: Extents) -> str:
    """Lines prepended to the job file; PCB_READY aborts the print if the live
    Klipper session has no datum/mesh covering these job-coordinate extents."""
    return (f"PCB_READY X0={cut[0]:.3f} Y0={cut[1]:.3f} X1={cut[2]:.3f} Y1={cut[3]:.3f}\n")


def with_header(gcode: str, cut: Extents) -> str:
    return ready_header(cut) + gcode
