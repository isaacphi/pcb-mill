#!/usr/bin/env python
"""Plot mill.py output for visual verification before cutting.

Overlays top isolation (blue), bottom isolation mirrored back about the
alignment axis (red — must register concentrically with the blue pad rings),
drill hits (black) and the edge cut (green) into toolpaths.png.

Run:  uv run plot_toolpaths.py <mill_out_dir> <mirror_x_mm>
      (mirror_x is printed by mill.py: "mirror axis X = ... mm")
"""
import re, sys, os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def parse(path):
    """Cutting polylines: XY moves while Z < 0."""
    segs, cur, z, pen = [], None, 5.0, []
    for line in open(path):
        m = dict(re.findall(r'([XYZ])(-?\d+\.?\d*)', line.split(';')[0]))
        if 'Z' in m:
            z = float(m['Z'])
            if z >= 0 and pen:
                segs.append(pen); pen = []
            elif z < 0 and not pen and cur:
                pen = [cur]          # plunge point starts the polyline
        if 'X' in m or 'Y' in m:
            cur = (float(m.get('X', cur[0] if cur else 0)),
                   float(m.get('Y', cur[1] if cur else 0)))
            if z < 0:
                pen.append(cur)
    if pen:
        segs.append(pen)
    return segs


def drills(path):
    pts, cur = [], (0, 0)
    for line in open(path):
        m = dict(re.findall(r'([XYZ])(-?\d+\.?\d*)', line.split(';')[0]))
        if 'X' in m or 'Y' in m:
            cur = (float(m.get('X', cur[0])), float(m.get('Y', cur[1])))
        if 'Z' in m and float(m['Z']) < 0:
            pts.append(cur)
    return pts


out = sys.argv[1] if len(sys.argv) > 1 else '.'
os.chdir(out)
fig, ax = plt.subplots(figsize=(16, 10))
if os.path.exists('onesided.nc'):
    # single-sided output: everything already in one machine frame, no mirroring
    for seg in parse('isolation.nc'):
        if len(seg) > 1:
            ax.plot([p[0] for p in seg], [p[1] for p in seg], 'b-', lw=0.4)
    dr = drills('drill.nc')
    ax.plot([p[0] for p in dr], [p[1] for p in dr], 'k.', ms=3)
    for seg in parse('edge.nc'):
        if len(seg) > 1:
            ax.plot([p[0] for p in seg], [p[1] for p in seg], 'g-', lw=1)
    ax.set_title('single-sided: blue=isolation, black=drill pecks, green=edge (machine frame)')
elif os.path.exists('top.nc') and os.path.exists('bottom.nc'):
    mirror_x = float(sys.argv[2])
    for seg in parse('top.nc'):
        if len(seg) > 1:
            ax.plot([p[0] for p in seg], [p[1] for p in seg], 'b-', lw=0.4)
    for seg in parse('bottom.nc'):
        if len(seg) > 1:
            ax.plot([mirror_x - p[0] for p in seg], [p[1] for p in seg], 'r-', lw=0.3, alpha=0.6)
    dr = drills('top.nc')
    ax.plot([p[0] for p in dr], [p[1] for p in dr], 'k.', ms=3)
    ax.set_title('2-file double-sided: blue=top sheet, red=bottom sheet (mirrored back), black=drill')
else:
    mirror_x = float(sys.argv[2])
    for seg in parse('top_copper_isolation.nc'):
        if len(seg) > 1:
            ax.plot([p[0] for p in seg], [p[1] for p in seg], 'b-', lw=0.4)
    for seg in parse('bottom_copper_isolation.nc'):
        if len(seg) > 1:
            ax.plot([mirror_x - p[0] for p in seg], [p[1] for p in seg], 'r-', lw=0.3, alpha=0.6)
    dr = drills('drill_top.nc')
    ax.plot([p[0] for p in dr], [p[1] for p in dr], 'k.', ms=3)
    for seg in parse('edge_top.nc'):
        if len(seg) > 1:
            ax.plot([p[0] for p in seg], [p[1] for p in seg], 'g-', lw=1)
    ax.set_title('blue=top isolation, red=bottom (mirrored back), black=drill pecks, green=edge')
ax.set_aspect('equal')
plt.savefig('toolpaths.png', dpi=110, bbox_inches='tight')
print(f'toolpaths.png written ({len(dr)} drill pecks)')
