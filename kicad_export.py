"""KiCad board -> Gerbers + drill -> G-code (wraps mill.py; needs KiCad 8+ installed).

Exports F.Cu, B.Cu, Edge.Cuts and one combined PTH+NPTH Excellon file into <out>/gerbers,
then runs mill.py. Single-sided: --side bottom (default) mills B.Cu, --side top mills F.Cu.
kicad-cli is found via $KICAD_CLI, then PATH, then the default Mac/Windows install path.

Run:  uv run kicad_export.py board.kicad_pcb [-o out] [--side top|bottom | --double] [--config FILE]
"""

import argparse
import glob
import os
import shutil
import sys
from pathlib import Path

from mill import ROOT, mill, sh

DEFAULT_CLI = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    *sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"), reverse=True),
]


def find_kicad_cli():
    for c in [os.environ.get("KICAD_CLI"), shutil.which("kicad-cli"), *DEFAULT_CLI]:
        if c and Path(c).exists():
            return c
    sys.exit("kicad-cli not found — install KiCad or set KICAD_CLI to its path")


def export(board, gdir):
    """-> (F.Cu, B.Cu, Edge.Cuts, drill) paths."""
    cli = find_kicad_cli()
    gdir.mkdir(parents=True, exist_ok=True)
    sh([cli, "pcb", "export", "gerbers", "--layers", "F.Cu,B.Cu,Edge.Cuts",
        "--no-protel-ext", "--output", str(gdir), str(board)])
    # no --excellon-separate-th: plated + non-plated holes in ONE file
    sh([cli, "pcb", "export", "drill", "--format", "excellon",
        "--output", str(gdir) + os.sep, str(board)])
    s = board.stem
    return (gdir / f"{s}-F_Cu.gbr", gdir / f"{s}-B_Cu.gbr",
            gdir / f"{s}-Edge_Cuts.gbr", gdir / f"{s}.drl")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("out"))
    ap.add_argument("--double", action="store_true", help="two-sided: mill F.Cu and B.Cu sheets")
    ap.add_argument("--side", choices=("top", "bottom"), default="bottom",
                    help="single-sided: copper layer, F.Cu or B.Cu (default: bottom)")
    ap.add_argument("--config", default=ROOT / "mill_config.yaml")
    a = ap.parse_args()
    out = a.out.resolve()
    fcu, bcu, edge, drl = export(a.board.resolve(), out / "gerbers")
    if a.double:
        mill(edge, drl, out, top=fcu, bottom=bcu, config=a.config)
    else:
        mill(edge, drl, out, copper=fcu if a.side == "top" else bcu, side=a.side, config=a.config)


if __name__ == "__main__":
    main()
