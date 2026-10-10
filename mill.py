"""Gerber + Excellon -> GRBL G-code for PCB isolation milling (no KiCad needed).

Single-sided (--copper): milled copper-side up. Writes onesided.nc (isolation ->
drill -> edge, one tool, no pauses) plus isolation.nc, drill.nc, edge.nc.
  --side bottom (default): copper designed on the bottom layer; all output pre-mirrored.
  --side top: copper designed on the top layer; output as designed.

--top + --bottom: double-sided (two single-sided sheets glued together). Writes top.nc and
bottom.nc (pre-mirrored); both drill 3 corner alignment holes for flip registration.

The drill file must hold ALL holes (plated + non-plated) in one Excellon file.

Run:  uv run mill.py --copper B_Cu.gbr --edge Edge_Cuts.gbr --drill board.drl [-o out] [--side top]
      uv run mill.py --top F_Cu.gbr --bottom B_Cu.gbr --edge ... --drill ... -o out
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "vendor" / "gerber2gcode" / "gerber_to_gcode.py"
INTERMEDIATE = ("pcb_isolation.nc", "pcb_back_isolation.nc", "pcb_edge_cuts.nc", "pcb_drill.nc")


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit("FAILED: %s\n%s\n%s" % (cmd[:3], r.stdout[-1500:], r.stderr[-1500:]))
    return r


def _is_motion(line):
    s = line.lstrip()
    return s.startswith("G0") or s.startswith("G1")


def mirror_x(text, axis):
    """Mirror every motion X about `axis` (X' = axis - X); arcs also flip G2<->G3 and negate I."""

    def repl(m):
        return "X%.4f" % (axis - float(m.group(1)))

    out = []
    for line in text.splitlines():
        arc = re.match(r"\s*G0?([23])\b", line)
        if _is_motion(line) or arc:
            line = re.sub(r"X(-?\d+\.?\d*)", repl, line)
        if arc:  # mirroring reverses arc direction and the X centre offset
            line = re.sub(r"G0?[23]\b", "G3" if arc.group(1) == "2" else "G2", line, count=1)
            line = re.sub(r"I(-?\d+\.?\d*)", lambda m: "I%.4f" % -float(m.group(1)), line)
        out.append(line)
    return "\n".join(out) + "\n"


def board_span_x(edge_gcode):
    xs = [
        float(x)
        for l in edge_gcode.splitlines()
        if _is_motion(l)
        for x in re.findall(r"X(-?\d+\.?\d*)", l)
    ]
    return min(xs), max(xs)


# ---------- G-code surgery for the single-sided combined program ----------


def strip_alignment_holes(text):
    """Remove the corner alignment-hole block from a drill program."""
    lines = text.splitlines()
    try:
        a = next(i for i, l in enumerate(lines) if "=== ALIGNMENT HOLES" in l)
        b = next(i for i, l in enumerate(lines) if "=== END ALIGNMENT HOLES" in l)
    except StopIteration:
        return text
    return "\n".join(lines[:a] + lines[b + 1 :]) + "\n"


def strip_alignment_marks(text):
    """Remove alignment marks + the M0 inspect pause from an isolation program."""
    lines = text.splitlines()
    try:
        a = next(i for i, l in enumerate(lines) if "=== ALIGNMENT MARKS" in l)
    except StopIteration:
        return text
    m0 = next(i for i in range(a, len(lines)) if lines[i].lstrip().startswith("M0"))
    # resume block after M0: M3 S.. then G4 dwell
    end = next(
        i
        for i in range(m0, min(m0 + 5, len(lines)))
        if lines[i].lstrip().startswith("G4")
    )
    return "\n".join(lines[:a] + lines[end + 1 :]) + "\n"


def split_program(text):
    """-> (header, body, footer). Header ends at the first 'G0 Z' safe move after
    spindle start; footer is the trailing safe-up/M5/origin/M2 block."""
    lines = text.splitlines()
    h = next(i for i, l in enumerate(lines) if l.startswith("G0 Z"))
    f = max(i for i, l in enumerate(lines) if l.lstrip().startswith("M5"))
    fs = f - 1 if f and lines[f - 1].startswith("G0 Z") else f
    return lines[: h + 1], lines[h + 1 : fs], lines[fs:]


def spindle_speed(header_lines):
    for l in header_lines:
        m = re.match(r"M3 S(\d+)", l.strip())
        if m:
            return int(m.group(1))
    return None


def combine(sections):
    """sections: [(title, program_text)]. One program: first header, all bodies
    (re-issuing M3 when the spindle speed changes), one footer."""
    hdr, _, ftr = split_program(sections[0][1])
    out = list(hdr)
    speed = spindle_speed(hdr)
    for title, text in sections:
        h, body, _ = split_program(text)
        out.append("")
        out.append(f"; ================= {title} =================")
        s = spindle_speed(h)
        if s and s != speed:
            out.append(f"M3 S{s}  ; spindle speed change")
            out.append("G4 P2")
            speed = s
        out.extend(body)
    out.extend(ftr)
    return "\n".join(out) + "\n"


# ---------- flows ----------


def run_gerber2gcode(top, bottom, edge, drill, config, out):
    """Run gerber2gcode --separate in `out` and return (mirror axis X, read(name))."""
    if not TOOL.exists():
        sys.exit("gerber2gcode missing — run:  uv run fetch_gerber2gcode.py")
    if "D01" not in Path(edge).read_text():
        sys.exit("Edge cuts Gerber has no outline — draw a board outline first.")
    # --separate writes to the CWD (ignores -o), so run in `out` with absolute inputs
    sh(
        [sys.executable, str(TOOL), "-t", str(top), "-b", str(bottom), "-e", str(edge),
         "-d", str(drill), "-o", "pcb.nc", "--separate", "--config", str(config)],
        cwd=str(out),
    )
    texts = {n: (out / n).read_text() for n in INTERMEDIATE}
    for n in INTERMEDIATE:
        (out / n).unlink()
    lo, hi = board_span_x(texts["pcb_edge_cuts.nc"])
    return lo + hi, texts


def single_sided(copper, side, edge, drill, config, out):
    # gerber2gcode needs both layers; feed it the copper twice and keep the side we want
    axis, t = run_gerber2gcode(copper, copper, edge, drill, config, out)
    if side == "top":  # copper faces up as designed: machine frame, no mirroring
        iso = strip_alignment_marks(t["pcb_isolation.nc"])
        drl = strip_alignment_holes(t["pcb_drill.nc"])
        edg = t["pcb_edge_cuts.nc"]
    else:  # bottom copper milled facing up: everything mirrored
        iso = strip_alignment_marks(t["pcb_back_isolation.nc"])
        drl = strip_alignment_holes(mirror_x(t["pcb_drill.nc"], axis))
        edg = mirror_x(t["pcb_edge_cuts.nc"], axis)
    files = {"isolation.nc": iso, "drill.nc": drl, "edge.nc": edg,
             "onesided.nc": combine([("COPPER ISOLATION", iso), ("DRILL", drl), ("EDGE CUT", edg)])}
    print(f"single-sided, {side} copper (milled copper-side up"
          + ("; all output pre-mirrored)" if side == "bottom" else ")"))
    return axis, files


def double_sided(top, bottom, edge, drill, config, out):
    axis, t = run_gerber2gcode(top, bottom, edge, drill, config, out)
    # bottom isolation is already mirrored by gerber2gcode; mirror its drill + edge here
    files = {
        "top.nc": combine([
            ("COPPER ISOLATION", strip_alignment_marks(t["pcb_isolation.nc"])),
            ("DRILL", t["pcb_drill.nc"]),
            ("EDGE CUT", t["pcb_edge_cuts.nc"]),
        ]),
        "bottom.nc": combine([
            ("COPPER ISOLATION", strip_alignment_marks(t["pcb_back_isolation.nc"])),
            ("DRILL", mirror_x(t["pcb_drill.nc"], axis)),
            ("EDGE CUT", mirror_x(t["pcb_edge_cuts.nc"], axis)),
        ]),
    }
    print("double-sided (two single-sided sheets; mill top, flip onto pins, mill bottom)")
    return axis, files


def mill(edge, drill, out, copper=None, side="bottom", top=None, bottom=None,
         config=ROOT / "mill_config.yaml"):
    """Single-sided: copper + side. Double-sided: top + bottom."""
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    paths = [Path(p).resolve() if p else None for p in (edge, drill, config, copper, top, bottom)]
    for p in paths:
        if p and not p.exists():
            sys.exit(f"not found: {p}")
    edge, drill, config, copper, top, bottom = paths
    if top and bottom:
        axis, files = double_sided(top, bottom, edge, drill, config, out)
    else:
        axis, files = single_sided(copper, side, edge, drill, config, out)
    for name, text in files.items():
        (out / name).write_text(text)
        print("  %-16s %8d bytes" % (name, len(text)))
    print("mirror axis X = %.3f mm" % axis)
    print(f"check before cutting:  uv run plot_toolpaths.py {out} {axis:.3f}")
    return axis


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    layers = ap.add_mutually_exclusive_group(required=True)
    layers.add_argument("--copper", help="single-sided: the copper Gerber")
    layers.add_argument("--top", help="double-sided: top copper Gerber (needs --bottom)")
    ap.add_argument("--bottom", help="double-sided: bottom copper Gerber")
    ap.add_argument("--side", choices=("top", "bottom"), default="bottom",
                    help="single-sided: layer the copper was designed on (default: bottom)")
    ap.add_argument("--edge", required=True, help="board outline Gerber")
    ap.add_argument("--drill", required=True, help="Excellon drill file with ALL holes")
    ap.add_argument("-o", "--out", default="out", help="output directory (default: out)")
    ap.add_argument("--config", default=ROOT / "mill_config.yaml", help="tool/feed settings YAML")
    a = ap.parse_args()
    if bool(a.top) != bool(a.bottom):
        ap.error("double-sided needs both --top and --bottom")
    mill(a.edge, a.drill, a.out, copper=a.copper, side=a.side, top=a.top, bottom=a.bottom,
         config=a.config)


if __name__ == "__main__":
    main()
