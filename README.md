# pcb-mill

Gerber + Excellon → GRBL G-code for milling PCBs on a small CNC (isolation routing, drilling,
boring, board cut-out). Works on macOS, Windows and Linux. KiCad is optional.

Built on [gerber2gcode](https://github.com/gcappaert/gerber2gcode), fetched at a pinned commit and
patched (see [Patches](#patches)).

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [git](https://git-scm.com).
2. ```
   git clone https://github.com/isaacphi/pcb-mill
   cd pcb-mill
   uv run fetch_gerber2gcode.py
   ```
   uv installs Python and the dependencies on first run.

## Use

**From Gerbers** (any EDA tool):

```
# single-sided, copper designed on the top layer (default)
uv run mill.py --copper board-F_Cu.gbr --edge board-Edge_Cuts.gbr --drill board.drl -o out

# single-sided, copper designed on the bottom layer (output is mirrored)
uv run mill.py --copper board-B_Cu.gbr --side bottom --edge ... --drill ... -o out

# double-sided
uv run mill.py --top board-F_Cu.gbr --bottom board-B_Cu.gbr --edge ... --drill ... -o out
```

The drill file must contain **all** holes (plated and non-plated) in one Excellon file.

**From KiCad** (8+; exports the Gerbers for you):

```
uv run kicad_export.py board.kicad_pcb -o out [--side top|bottom | --double]
```

`kicad-cli` is found via `$KICAD_CLI`, `PATH`, or the default macOS/Windows install location.

**Check before cutting:**

```
uv run plot_toolpaths.py out <mirror_x>   # mirror_x is printed by mill.py
```

Writes `out/toolpaths.png`. Bore arcs are not drawn.

## Output

| Mode | Files | Notes |
|---|---|---|
| single-sided | `onesided.nc` (+ `isolation.nc`, `drill.nc`, `edge.nc`) | Board milled copper-side up. `--side bottom` pre-mirrors everything. One tool, no pauses. |
| double-sided | `top.nc`, `bottom.nc` | Two sheets. `top.nc` drills 3 corner alignment holes (0,0), (W,0), (W,H): pin them, flip the board left-right onto the pins, run `bottom.nc` (pre-mirrored). No plated vias: wire through and solder both sides. |

Each file runs isolation → drill → edge cut.

## Settings: `mill_config.yaml`

Set up for a single 0.8 mm end mill. Copy it, edit bit size, feeds, depths and spindle speed for
your machine, and pass it with `--config`. Design the board with track width and clearance ≥ the
bit diameter.

Holes: within `bore_tolerance` (0.2 mm) of the bit → plunged at bit size. Larger → helically bored
to size (needs an end mill, not a twist drill).

## Patches

Applied in order by `fetch_gerber2gcode.py`:

- `01-toolradius.patch` — offsets isolation and edge cuts by the bit radius. Upstream puts the
  bit centre on the copper/outline edge, so every feature shrinks by half a bit width.
- `02-bore.patch` — bores holes larger than bit + `bore_tolerance`.

gerber2gcode has no open-source licence ("provided as-is for educational and personal use"),
so it is fetched from its repository, not included here.

## Status

Isolation, drilling and edge cuts have been cut on real boards. Helical boring and the
tool-radius isolation offset are verified in generated G-code only, not yet on a physical cut.
Developed on macOS; Windows support is untested.
