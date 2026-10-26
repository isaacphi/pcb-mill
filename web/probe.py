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

POCKET_MM = 100.0      # the bed pocket (work area): job coords 0..100 on both axes
CUT_SLACK_MM = 1.0     # isolation paths may run ~half a bit past the board edge (cutting air)
BOARD_SLACK_MM = 0.5   # gerber outlines are rarely exactly 100.0
EDGE_TOOL_MM = 0.8     # edge.nc is the tool-centre path, one bit wider than the board
PROBE_WINDOW = (5.0, 95.0)   # only probe inside this job-coordinate box (clear of the pocket walls)
MARGIN_MM = 1.5        # probe points this far inside the cut extents
SPACING_MM = 10.0      # max distance between probe points
MIN_POINTS = 4         # per axis (bicubic mesh needs >= 4)
MAX_POINTS = 12        # per axis
SAMPLES_PER_POINT = 2     # probe touches per mesh point (median; retried if they disagree)
SECONDS_PER_POINT = 2.8   # measured ~1.45 s/point at 1 sample; ~2.6 s at 2

# Machine travel (stepper_x/y position_max in klipper/printer.cfg) minus a
# 0.5 mm guard. The origin is the pocket corner, ~(25.4, 27.5).
MACHINE_MAX = (126.0, 128.0)
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
    if w > POCKET_MM + CUT_SLACK_MM or h > POCKET_MM + CUT_SLACK_MM:
        return (f"cut area is {w:.1f} × {h:.1f} mm; the limit is {POCKET_MM:.0f} × "
                f"{POCKET_MM:.0f} mm (the bed pocket) — shrink the board")
    if edge is not None:
        bw, bh = size_of(edge)
        lim = POCKET_MM + EDGE_TOOL_MM + BOARD_SLACK_MM
        if bw > lim or bh > lim:
            return (f"board outline is {bw - EDGE_TOOL_MM:.1f} × {bh - EDGE_TOOL_MM:.1f} mm; "
                    f"the limit is {POCKET_MM:.0f} × {POCKET_MM:.0f} mm (the bed pocket)")
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
    job: Extents = (0.0, 0.0, 0.0, 0.0)   # the same region in JOB coordinates

    def as_dict(self) -> dict:
        return asdict(self)

    def gcode(self) -> str:
        return (f"PCB_MESH X0={self.x0:.3f} Y0={self.y0:.3f} X1={self.x1:.3f} "
                f"Y1={self.y1:.3f} PTSX={self.nx} PTSY={self.ny} SAMPLES={SAMPLES_PER_POINT}")


def _count(span: float, spacing: float) -> int:
    return max(MIN_POINTS, min(MAX_POINTS, math.ceil(span / spacing - 1e-9) + 1))


def mesh_plan(
    cut: Extents,
    origin: tuple[float, float],
    margin: float = MARGIN_MM,
    spacing: float = SPACING_MM,
    window: tuple[float, float] = PROBE_WINDOW,
    toolpaths: Extents | None = None,
) -> MeshPlan:
    """Mesh region over the cut extents: shrunk by `margin`, clipped to the
    probe `window`, shifted by the board `origin` into machine coordinates.

    `fits` is False when the mesh (or, if given, the whole `toolpaths` box)
    would leave machine travel; `problem` says by how much.
    """
    ox, oy = origin
    lo, hi = window
    x0, y0 = max(cut[0] + margin, lo), max(cut[1] + margin, lo)
    x1, y1 = min(cut[2] - margin, hi), min(cut[3] - margin, hi)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"cut area too small for the probe window/margin: {cut}")
    nx, ny = _count(x1 - x0, spacing), _count(y1 - y0, spacing)

    problem = None
    boxes = [(x0, y0, x1, y1)] + ([toolpaths] if toolpaths else [])
    over = []
    for bx0, by0, bx1, by1 in boxes:
        if bx1 + ox > MACHINE_MAX[0]:
            over.append(f"{bx1 + ox - MACHINE_MAX[0]:.1f} mm past X travel")
        if by1 + oy > MACHINE_MAX[1]:
            over.append(f"{by1 + oy - MACHINE_MAX[1]:.1f} mm past Y travel")
        if bx0 + ox < MACHINE_MIN[0] or by0 + oy < MACHINE_MIN[1]:
            over.append("below X/Y minimum")
    if over:
        problem = (f"with the board origin at machine {ox:.1f}, {oy:.1f} the job is "
                   + ", ".join(dict.fromkeys(over)) +
                   " — move the board origin or raise position_max")
    return MeshPlan(round(x0 + ox, 3), round(y0 + oy, 3), round(x1 + ox, 3), round(y1 + oy, 3),
                    nx, ny, int(nx * ny * SECONDS_PER_POINT), problem is None, problem,
                    (round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)))


# ---------------------------------------------------------------------------
# G-code header: refuse to cut without a datum + mesh that cover this job
# ---------------------------------------------------------------------------
def ready_header(region: Extents, deep: bool = False) -> str:
    """Lines prepended to a job file. `region` is the mesh region in job
    coordinates. PCB_READY aborts the print unless the live Klipper session has
    a datum and a mesh covering it at the current board origin. Drill/edge files
    also get PCB_DEEP (room below the copper; no motion)."""
    head = (f"PCB_READY X0={region[0]:.3f} Y0={region[1]:.3f} "
            f"X1={region[2]:.3f} Y1={region[3]:.3f}\n")
    return head + ("PCB_DEEP\n" if deep else "")


def with_header(gcode: str, region: Extents, deep: bool = False) -> str:
    return ready_header(region, deep) + gcode
