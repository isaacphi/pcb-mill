"""Excellon drill file parsing, hole reporting, and PTH/NPTH merging.

KiCad-style decimal metric files only (METRIC + decimal coords), which is what
mill.py/gerber2gcode consume. A "tool" T<n>C<diam> block plus modal X/Y coords.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TOOL_DEF = re.compile(r"^T(\d+)C([\d.]+)")
TOOL_SEL = re.compile(r"^T(\d+)\s*$")
X_RE = re.compile(r"X(-?[\d.]+)")
Y_RE = re.compile(r"Y(-?[\d.]+)")


@dataclass
class DrillData:
    tools: dict[int, float] = field(default_factory=dict)  # tool nr -> diameter mm
    holes: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    metric: bool = True

    def add(self, other: "DrillData"):
        """Merge another file's tools/holes with fresh tool numbers."""
        n = max(self.tools, default=0)
        for t, dia in other.tools.items():
            n += 1
            self.tools[n] = dia
            if other.holes.get(t):
                self.holes[n] = list(other.holes[t])


def parse(text: str) -> DrillData:
    d = DrillData()
    d.metric = "INCH" not in text.split("%")[0]
    cur_tool = None
    x = y = None
    in_header = True
    for raw in text.splitlines():
        line = raw.split(";")[0].strip()
        if not line:
            continue
        if line.startswith("%"):
            in_header = False
            continue
        m = TOOL_DEF.match(line)
        if m and in_header:
            dia = float(m.group(2)) * (1 if d.metric else 25.4)
            d.tools[int(m.group(1))] = dia
            continue
        m = TOOL_SEL.match(line)
        if m and not in_header:
            cur_tool = int(m.group(1))
            continue
        if line.startswith(("X", "Y")) and cur_tool is not None:
            xm = X_RE.search(line)
            ym = Y_RE.search(line)
            if xm:
                x = float(xm.group(1))
            if ym:
                y = float(ym.group(1))
            if x is not None and y is not None:
                scale = 1 if d.metric else 25.4
                d.holes.setdefault(cur_tool, []).append((x * scale, y * scale))
                x = y = None
    return d


def render(d: DrillData) -> str:
    """Minimal KiCad-style Excellon (what gerber2gcode reads)."""
    out = ["M48", "; merged by pcb-mill", "FMAT,2", "METRIC"]
    for t in sorted(d.tools):
        out.append(f"T{t}C{d.tools[t]:.3f}")
    out += ["%", "G90", "G05"]
    for t in sorted(d.tools):
        pts = d.holes.get(t)
        if not pts:
            continue
        out.append(f"T{t}")
        out += [f"X{px:g}Y{py:g}" for px, py in pts]
    out.append("M30")
    return "\n".join(out) + "\n"


def merge(*texts: str) -> str:
    """Combine separate PTH/NPTH Excellon files into the single file mill.py wants."""
    merged = DrillData()
    for text in texts:
        if text and text.strip():
            merged.add(parse(text))
    return render(merged)


def hole_report(d: DrillData, bit_diameter: float, bore_tolerance: float) -> list[dict]:
    """Per hole-size: count + how mill.py will machine it with `bit_diameter`."""
    rows = []
    for t in sorted(d.tools, key=lambda t: d.tools[t]):
        dia = d.tools[t]
        n = len(d.holes.get(t, []))
        if n == 0:
            continue
        if dia < bit_diameter - 1e-9:
            method = f"undersized; plunged at {bit_diameter:g} mm bit (hole comes out oversized)"
        elif dia <= bit_diameter + bore_tolerance:
            method = "plunged"
        else:
            method = "helically bored"
        rows.append({"diameter": dia, "count": n, "method": method})
    return rows
