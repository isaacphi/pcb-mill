# Gerber to G-code Converter for PCB Milling

A command-line tool for converting Gerber PCB layout files to GRBL-compatible G-code for CNC milling and laser etching.

## Features

- Converts standard Gerber files to G-code for CNC milling
- **Laser etching** support for solder mask removal (diode laser, GRBL `$32=1`)
- **Double-sided PCB** support via back copper isolation with automatic horizontal mirror
- **Solder mask overlay PNG** generation for photoemulsion stencil printing
- YAML-based configuration for tool presets
- Isolation routing, edge cuts, and drilling operations
- Isolation border passes for extra trace clearance
- Pad detection and raster fill for laser operations
- Alignment mark generation (X-in-circle, 4mm diameter) for multi-step workflows
- Automatic board outline generation from trace bounds
- Separate or combined G-code output files
- Tool change commands for multi-tool workflows
- GRBL-compatible output
- Excellon drill file support

## Installation

### Prerequisites

- Python 3.7 or higher
- pip package manager

### Install Dependencies

```bash
pip install -r requirements.txt
```

See `INSTALL_WINDOWS.md` for detailed Windows setup instructions.

## Usage

### Windows Batch Script

On Windows, use the included batch script (automatically uses `config.yaml` from the tool directory):

```batch
gerber2gcode.bat -t traces.gtl -o output.nc
gerber2gcode.bat -t board.gtl -e edges.gm1 -d holes.drl --separate
gerber2gcode.bat --help
```

**Running from other directories:** Add the tool directory to your PATH, then run from anywhere.

```batch
cd C:\my\pcb\project
gerber2gcode -t board.gtl -o output.nc
```

To add to PATH:
1. Press `Win + R`, type `sysdm.cpl`, press Enter
2. Click **Advanced** tab → **Environment Variables**
3. Under "User variables", select **Path** → **Edit** → **New**
4. Add: `C:\Users\gcapp\Documents\Making\PCB Milling\Gerber2Gcode`
5. Click OK and open a new command prompt

### Basic Usage

```bash
# Isolation routing from traces file
python gerber_to_gcode.py -t traces.gtl -o output.nc

# Full PCB with traces, edge cuts, and drill holes
python gerber_to_gcode.py -t traces.gtl -e edges.gbr -d holes.drl -o output.nc
```

### Generating Separate Files

```bash
# Generate separate G-code files for each operation
python gerber_to_gcode.py -t traces.gtl -e edges.gbr -d holes.drl --separate
# Outputs: pcb_isolation.nc, pcb_edge_cuts.nc, pcb_drill.nc
```

### Laser Etching (Solder Mask Removal)

```bash
# Laser etch using copper layer for pad detection
python gerber_to_gcode.py -l top_copper.gtl --separate

# Laser etch with accurate pad fills from solder mask layer
python gerber_to_gcode.py -l top_copper.gtl -m top_mask.gts --separate
# Outputs: pcb_laser_traces.nc, pcb_laser_pads.nc
```

Requires GRBL laser mode enabled on the machine: `$32=1`

### Solder Mask Overlay PNG

```bash
# Generate a printable PNG for photoemulsion stenciling
python gerber_to_gcode.py -m board.gts --soldermask-png soldermask_top.png

# Override print DPI (default 600)
python gerber_to_gcode.py -m board.gts --soldermask-png mask.png --print-dpi 1200
```

### Double-Sided PCB (Back Copper / Ground Plane)

```bash
# Front isolation + back copper isolation (board flip compensated)
python gerber_to_gcode.py -t front.gtl -b back.gbl -e edges.gm1 --separate
# Outputs: pcb_isolation.nc, pcb_back_isolation.nc, pcb_edge_cuts.nc
```

The back copper bitmap is horizontally mirrored to compensate for physically flipping the board.

### Using a Configuration File

```bash
# Use default config.yaml
python gerber_to_gcode.py -t traces.gtl

# Use custom configuration
python gerber_to_gcode.py -t traces.gtl --config my_settings.yaml
```

## Command Line Options

### Input Files

| Option | Description |
|--------|-------------|
| `-t, --traces` | Copper traces Gerber file (.gbr, .gtl, .gbl) |
| `-e, --edge-cuts` | Edge cuts Gerber file for board outline |
| `-d, --drill` | Excellon drill file (.drl, .xln) |
| `-l, --laser-layer` | Copper layer Gerber for laser solder mask removal (.gtl, .gbr) |
| `-m, --mask-layer` | Solder mask Gerber for accurate pad fill (.gts top, .gbs bottom) |
| `-b, --back-traces` | Back copper Gerber for double-sided / ground plane isolation (.gbl, .gbr) |
| `input` | Legacy: Input Gerber file (use `-t` instead) |

### Output Options

| Option | Description |
|--------|-------------|
| `-o, --output` | Output G-code file (default: output.nc) |
| `--separate` | Generate separate files for each operation |
| `--generate-edge-cuts` | Output file for generated edge cuts Gerber |
| `--soldermask-png` | Output PNG for soldermask overlay (requires `-m`) |
| `--print-dpi` | Override print DPI for soldermask PNG (default from config, typically 600) |

### Configuration

| Option | Description |
|--------|-------------|
| `--config` | YAML configuration file (default: config.yaml) |
| `--edge-margin` | Override edge margin for imputed board outline (mm) |
| `--dpi` | Override DPI for Gerber rendering |

## Configuration File (config.yaml)

All tool settings are defined in the YAML configuration file:

```yaml
# General settings
general:
  safe_height: 2.0        # mm - Z height for rapid moves
  dpi: 1000               # Resolution for Gerber rendering

# Tool presets for each operation type
tools:
  # Isolation routing for copper traces
  isolation:
    tool_diameter: 0.1    # mm - V-bit tip or engraving bit
    spindle_speed: 10000  # RPM
    feed_rate: 200        # mm/min - cutting feed
    plunge_rate: 50       # mm/min - plunge feed
    cut_depth: 0.06       # mm - depth per pass
    passes: 1             # number of isolation passes
    step_over: 0.1        # mm - step between passes
    isolation_border: 0.5 # mm extra clearance around traces (0 = off)

  # Edge cuts / board outline
  edge_cuts:
    tool_diameter: 3.175  # mm - end mill for cutting through
    spindle_speed: 8000   # RPM
    feed_rate: 150        # mm/min - cutting feed
    plunge_rate: 30       # mm/min - plunge feed
    cut_depth: 0.3        # mm - depth per pass
    total_depth: 1.5      # mm - total board thickness
    tabs: false           # whether to leave holding tabs
    tab_width: 2.0        # mm - width of tabs if enabled
    tab_height: 0.3       # mm - height of tabs

  # Drilling holes
  drill:
    tool_diameter: 1.2    # mm - drill bit diameter
    spindle_speed: 5000   # RPM
    feed_rate: 100        # mm/min
    plunge_rate: 60       # mm/min - drilling feed
    cut_depth: 0.3        # mm - peck depth (0 for single plunge)
    total_depth: 1.54     # mm - drill through depth
    retract_height: 1.0   # mm - retract between pecks

  # Back copper isolation (double-sided PCBs / ground plane)
  # Omit to inherit all settings from isolation above
  back_isolation:
    tool_diameter: 0.1
    spindle_speed: 10000
    feed_rate: 200
    plunge_rate: 50
    cut_depth: 0.06
    passes: 1
    step_over: 0.1
    isolation_border: 0.5

  # Laser etching (diode laser for solder mask removal)
  laser:
    power: 500              # S value 0-1000 (GRBL laser power) for trace isolation
    feed_rate: 350          # mm/min for trace isolation
    plunge_rate: 1000       # mm/min (unused for laser, kept for compatibility)
    focus_height: 50.0      # mm Z height for laser focus
    dynamic_mode: true      # M4 dynamic mode (recommended) vs M3 constant
    fill_line_spacing: 0.1  # mm between raster lines for pad fill and border passes
    trace_border_passes: 3  # extra lines drawn outside each trace (0 = single edge)
    pad_min_area: 1.0       # mm² minimum area to classify as pad
    pad_max_eccentricity: 0.8  # 0=circle 1=line; below this = pad
    pad_power: 400          # S value for pad ablation (omit to use main power)
    pad_feed_rate: 500      # mm/min for pad ablation (omit to use main feed_rate)

# Soldermask overlay settings (for photoemulsion printing)
soldermask_overlay:
  print_dpi: 600          # DPI for printing on transparent film
  invert: true            # true = positive photoemulsion (pads dark, mask clear)
                          # false = negative photoemulsion

# Edge cuts imputation (when no edge cuts file provided)
edge_margin: 3.0          # mm - margin around traces for imputed board outline

# Output options
output:
  separate_files: false   # true = one file per operation, false = combined
  file_prefix: "pcb"      # prefix for output files when separate_files is true
```

## Workflows

### Standard Single-Sided PCB

1. Mill isolation routes with a V-bit
2. Drill holes
3. Cut board outline with an end mill

```bash
python gerber_to_gcode.py -t board.gtl -e board.gm1 -d board.drl --separate
```

### PCB with Laser Solder Mask

1. Mill isolation routes and drill holes (CNC)
2. Apply solder mask (spray/dip)
3. Laser etch pads clear of solder mask (laser)

```bash
# Step 1: CNC operations
python gerber_to_gcode.py -t board.gtl -e board.gm1 -d board.drl --separate

# Step 2: Laser operations (uses alignment mark for registration)
python gerber_to_gcode.py -l board.gtl -m board.gts --separate
```

### Double-Sided PCB

1. Mill front copper isolation
2. Flip board on alignment pins
3. Mill back copper isolation (mirrored automatically)
4. Drill and cut outline

```bash
python gerber_to_gcode.py -t front.gtl -b back.gbl -e board.gm1 -d board.drl --separate
```

### Photoemulsion Solder Mask

Generate a printable transparency for exposing a photoemulsion solder mask stencil:

```bash
python gerber_to_gcode.py -m board.gts --soldermask-png soldermask_top.png
```

## Examples

### Example 1: Basic Isolation Routing

```bash
python gerber_to_gcode.py -t top_copper.gtl -o isolation.nc
```

### Example 2: Full PCB Processing

```bash
python gerber_to_gcode.py \
    -t board.gtl \
    -e board_outline.gm1 \
    -d board.drl \
    -o board_complete.nc
```

### Example 3: Separate Output Files

```bash
python gerber_to_gcode.py \
    -t board.gtl \
    -e board_outline.gm1 \
    -d board.drl \
    --separate
# Outputs: pcb_isolation.nc, pcb_edge_cuts.nc, pcb_drill.nc
```

### Example 4: Laser Etching with Mask Layer

```bash
python gerber_to_gcode.py \
    -l board.gtl \
    -m board.gts \
    --separate
# Outputs: pcb_laser_traces.nc, pcb_laser_pads.nc
```

### Example 5: Full Double-Sided PCB

```bash
python gerber_to_gcode.py \
    -t front.gtl \
    -b back.gbl \
    -e board.gm1 \
    -d board.drl \
    --separate
# Outputs: pcb_isolation.nc, pcb_back_isolation.nc, pcb_edge_cuts.nc, pcb_drill.nc
```

### Example 6: Auto-Generate Board Outline

```bash
python gerber_to_gcode.py \
    -t board.gtl \
    --edge-margin 2.0 \
    --generate-edge-cuts board_outline.gbr \
    -o board.nc
```

### Example 7: High Resolution Rendering

```bash
python gerber_to_gcode.py \
    -t fine_traces.gtl \
    --dpi 2000 \
    -o fine_traces.nc
```

### Example 8: Custom Configuration

```bash
python gerber_to_gcode.py \
    -t board.gtl \
    -e edges.gbr \
    --config custom_tools.yaml \
    -o output.nc
```

## Typical Tool Settings

### V-Bit Engraving (0.1mm tip)
- Tool diameter: 0.1 mm
- Feed rate: 200-300 mm/min
- Plunge rate: 50-80 mm/min
- Cut depth: 0.03-0.1 mm
- Spindle speed: 10000-20000 RPM

### End Mill (1/8" / 3.175mm)
- Tool diameter: 3.175 mm
- Feed rate: 150-250 mm/min
- Plunge rate: 30-60 mm/min
- Cut depth: 0.3-0.5 mm per pass
- Spindle speed: 8000-12000 RPM

### PCB Drilling
- Tool diameter: 0.8-1.2 mm
- Plunge rate: 60-100 mm/min
- Peck depth: 0.3-0.5 mm
- Spindle speed: 5000-15000 RPM

### Diode Laser (Solder Mask Removal)
- Power: 400-600 S value (out of 1000)
- Feed rate: 300-500 mm/min for traces
- Focus height: set to laser's focal distance
- Dynamic mode (M4): recommended
- Fill line spacing: 0.08-0.12 mm

## Workflow Tips

1. **Test First**: Always run a test cut on scrap material
2. **Depth Calibration**: Measure actual copper thickness and adjust cut depth
3. **Isolation Border**: Use `isolation_border` in config for extra clearance without multiple passes
4. **Separate Files**: Use `--separate` for manual tool changes between operations
5. **Laser Registration**: Alignment marks are automatically added to laser files — use them with a registration pin system
6. **Laser Mode**: Enable GRBL laser mode before running laser files: `$32=1`; disable after: `$32=0`
7. **Back Copper**: For double-sided boards, keep the same X/Y reference when flipping the board
8. **Pad Detection**: Provide `-m` (solder mask layer) for more accurate laser pad fills vs heuristic detection

## Output Files (--separate mode)

| File suffix | Operation |
|------------|-----------|
| `_isolation.nc` | Front copper isolation routing |
| `_back_isolation.nc` | Back copper isolation routing (mirrored) |
| `_edge_cuts.nc` | Board outline cutout |
| `_drill.nc` | Drilling operations |
| `_laser_traces.nc` | Laser trace border contours |
| `_laser_pads.nc` | Laser pad raster fill |

## File Types

### Gerber Files
- `.gtl` - Top copper layer
- `.gbl` - Bottom copper layer
- `.gto` - Top silkscreen
- `.gbo` - Bottom silkscreen
- `.gts` - Top soldermask
- `.gbs` - Bottom soldermask
- `.gm1` / `.gko` - Board outline/edge cuts
- `.gbr` - Generic Gerber

### Drill Files
- `.drl` - Excellon drill file
- `.xln` - Excellon drill file (alternate extension)

## Limitations

- Focused on isolation routing, edge cuts, drilling, and laser etching
- Tab support is defined in config but not yet fully implemented
- For high-volume production, consider dedicated PCB CAM software
- Laser pad detection without a solder mask layer uses heuristics and may misclassify some features

## Safety

- Always wear safety glasses when operating CNC equipment
- Secure workpiece properly before milling
- Start with conservative speeds and depths
- Keep hands clear of cutting area
- Use dust extraction when milling FR4
- For laser operations: use appropriate laser safety eyewear and enclosure

## Troubleshooting

**Issue**: "pygerber library not found"
- Run: `pip install pygerber`

**Issue**: "PyYAML not found"
- Run: `pip install pyyaml`

**Issue**: G-code paths don't match design
- Increase `--dpi` for finer resolution
- Check that input file is valid Gerber format

**Issue**: Edge cuts in wrong location
- Provide explicit edge cuts file with `-e`
- Adjust `--edge-margin` for auto-generated outlines

**Issue**: Drill holes not recognized
- Verify Excellon format (check INCH vs METRIC header)
- Some drill files use different coordinate formats

**Issue**: Tool breaking
- Reduce feed rate in config.yaml
- Reduce depth of cut per pass
- Use multiple lighter passes

**Issue**: Laser not firing or wrong power
- Confirm GRBL laser mode is enabled: `$32=1`
- Check `power` value in config (0–1000 for GRBL)
- Use `dynamic_mode: true` (M4) for better power modulation

**Issue**: Laser pad fills missing or wrong areas
- Provide the solder mask layer with `-m board.gts` for accurate fills
- Adjust `pad_min_area` and `pad_max_eccentricity` in config

**Issue**: Back copper alignment is off
- Ensure board is flipped on the same axis (typically the X axis)
- Use alignment marks from the isolation file as registration references

## License

This tool is provided as-is for educational and personal use.

## Contributing

Feel free to modify and improve this tool for your specific needs.
