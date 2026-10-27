#!/usr/bin/env python3
"""
Gerber to G-code converter for PCB milling
Converts Gerber files to GRBL-compatible G-code for CNC milling
Supports separate tools for isolation routing, edge cuts, and drilling
"""

import argparse
import sys
import os
from pathlib import Path
from typing import List, Tuple, Dict, Optional
import re

try:
    import yaml
except ImportError:
    print("Error: PyYAML not found. Install with: pip install pyyaml")
    sys.exit(1)

try:
    from pygerber.gerberx3.api.v2 import (
        GerberFile,
        FileTypeEnum,
        ImageFormatEnum,
        PixelFormatEnum,
    )
except ImportError:
    print("Error: pygerber library not found. Install with: pip install pygerber")
    sys.exit(1)

try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("Error: Required libraries not found. Install with: pip install numpy pillow")
    sys.exit(1)


class ToolPreset:
    """Tool settings for a specific operation"""
    def __init__(self, config: Dict):
        self.tool_diameter = config.get('tool_diameter', 0.1)
        self.spindle_speed = config.get('spindle_speed', 10000)
        self.feed_rate = config.get('feed_rate', 200)
        self.plunge_rate = config.get('plunge_rate', 50)
        self.cut_depth = config.get('cut_depth', 0.1)
        self.total_depth = config.get('total_depth', self.cut_depth)
        self.passes = config.get('passes', 1)
        self.step_over = config.get('step_over', 0.1)
        self.retract_height = config.get('retract_height', 1.0)
        self.tabs = config.get('tabs', False)
        self.tab_width = config.get('tab_width', 2.0)
        self.tab_height = config.get('tab_height', 0.3)
        self.isolation_border = config.get('isolation_border', 0.0)  # mm
        # Drill: holes more than bore_tolerance larger than the bit are helically bored
        self.bore_tolerance = config.get('bore_tolerance', 0.2)  # mm
        self.bore_step_down = config.get('bore_step_down', 0.4)  # mm of Z per helix turn
        # Laser-specific attributes
        self.power = config.get('power', 800)
        self.focus_height = config.get('focus_height', 50.0)
        self.dynamic_mode = config.get('dynamic_mode', True)
        self.fill_line_spacing = config.get('fill_line_spacing', 0.1)
        self.trace_border_passes = config.get('trace_border_passes', 0)
        self.pad_min_area = config.get('pad_min_area', 1.0)
        self.pad_max_eccentricity = config.get('pad_max_eccentricity', 0.8)
        # Pad ablation can use lower power / higher speed than trace isolation.
        # Falls back to the main laser power/feed_rate if not set.
        self.pad_power = config.get('pad_power', self.power)
        self.pad_feed_rate = config.get('pad_feed_rate', self.feed_rate)


class GerberToGcode:
    def __init__(self, config_file: Optional[str] = None):
        self.config = self.load_config(config_file)
        self.safe_height = self.config.get('general', {}).get('safe_height', 2.0)
        self.dpi = self.config.get('general', {}).get('dpi', 1000)
        self.edge_margin = self.config.get('edge_margin', 1.0)
        self.separate_files = self.config.get('output', {}).get('separate_files', False)
        self.file_prefix = self.config.get('output', {}).get('file_prefix', 'pcb')

        # Tool presets
        tools_config = self.config.get('tools', {})
        self.isolation_tool = ToolPreset(tools_config.get('isolation', {}))
        self.edge_cuts_tool = ToolPreset(tools_config.get('edge_cuts', {}))
        self.drill_tool = ToolPreset(tools_config.get('drill', {}))
        self.laser_tool = ToolPreset(tools_config.get('laser', {}))
        # Back isolation defaults to the same settings as isolation if not separately configured
        back_iso_config = tools_config.get('back_isolation', tools_config.get('isolation', {}))
        self.back_isolation_tool = ToolPreset(back_iso_config)

        # Soldermask overlay settings
        sm_config = self.config.get('soldermask_overlay', {})
        self.soldermask_print_dpi = sm_config.get('print_dpi', 600)
        self.soldermask_invert = sm_config.get('invert', True)

        # File paths
        self.traces_file = None
        self.edge_cuts_file = None
        self.drill_file = None
        self.output_file = None

        # Processed data
        self.trace_bounds = None
        self.board_outline = None
        # Coordinate offset applied to all G-code output so the board's lower-left
        # corner (= front alignment mark) maps to machine (0, 0).
        self._coord_offset = (0.0, 0.0)

    def load_config(self, config_file: Optional[str]) -> Dict:
        """Load configuration from YAML file or return defaults"""
        default_config = {
            'general': {'safe_height': 2.0, 'dpi': 1000},
            'tools': {
                'isolation': {
                    'tool_diameter': 0.1, 'spindle_speed': 10000,
                    'feed_rate': 200, 'plunge_rate': 50, 'cut_depth': 0.1,
                    'passes': 1, 'step_over': 0.1
                },
                'edge_cuts': {
                    'tool_diameter': 2.0, 'spindle_speed': 8000,
                    'feed_rate': 150, 'plunge_rate': 30, 'cut_depth': 0.5,
                    'total_depth': 1.6
                },
                'drill': {
                    'tool_diameter': 0.8, 'spindle_speed': 12000,
                    'feed_rate': 100, 'plunge_rate': 60, 'cut_depth': 0.5,
                    'total_depth': 1.8, 'retract_height': 1.0
                }
            },
            'edge_margin': 1.0,
            'output': {'separate_files': False, 'file_prefix': 'pcb'}
        }

        if config_file and os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    loaded = yaml.safe_load(f)
                    # Merge with defaults
                    self._deep_merge(default_config, loaded)
                    print(f"Loaded configuration from {config_file}")
                    return default_config
            except Exception as e:
                print(f"Warning: Could not load config file: {e}")
                print("Using default configuration")

        return default_config

    def _deep_merge(self, base: Dict, override: Dict):
        """Deep merge override into base dict"""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def generate_gcode_header(self, operation: str, tool: ToolPreset) -> str:
        """Generate G-code header with initialization commands"""
        header = [
            f"; G-code generated by Gerber to G-code converter",
            f"; Operation: {operation}",
            f"; Tool diameter: {tool.tool_diameter} mm",
            f"; Feed rate: {tool.feed_rate} mm/min",
            f"; Cut depth: {tool.cut_depth} mm",
            "",
            "G21         ; Set units to millimeters",
            "G90         ; Absolute positioning",
            "G94         ; Feed rate per minute",
            f"M3 S{tool.spindle_speed}  ; Start spindle",
            "G4 P2       ; Dwell 2 seconds for spindle to reach speed",
            f"G0 Z{self.safe_height}    ; Move to safe height",
            ""
        ]
        return "\n".join(header)

    def generate_gcode_footer(self) -> str:
        """Generate G-code footer with shutdown commands"""
        footer = [
            "",
            f"G0 Z{self.safe_height}    ; Move to safe height",
            "M5          ; Stop spindle",
            "G0 X0 Y0    ; Return to origin",
            "M2          ; Program end",
            ""
        ]
        return "\n".join(footer)

    def generate_tool_change(self, operation: str, tool: ToolPreset) -> List[str]:
        """Generate G-code for tool change"""
        return [
            "",
            f"; ========== TOOL CHANGE: {operation} ==========",
            f"; Tool: {tool.tool_diameter} mm",
            f"G0 Z{self.safe_height}    ; Move to safe height",
            "M5          ; Stop spindle",
            "G0 X0 Y0    ; Move to tool change position",
            f"M0          ; Pause for tool change - {operation}",
            f"M3 S{tool.spindle_speed}  ; Start spindle",
            "G4 P2       ; Dwell for spindle",
            ""
        ]

    def render_gerber_to_bitmap(self, gerber_file: str) -> Tuple[np.ndarray, Tuple[float, float, float, float]]:
        """Render Gerber file to a bitmap image using pygerber.
        Returns (bitmap, gerber_bounds) where gerber_bounds is (min_x, min_y, max_x, max_y) in mm."""
        from io import BytesIO

        try:
            gf = GerberFile.from_file(
                gerber_file,
                file_type=FileTypeEnum.INFER_FROM_EXTENSION
            )
            parsed_file = gf.parse()

            # Extract the actual Gerber coordinate bounds
            info = parsed_file.get_info()
            gerber_bounds = (
                float(info.min_x_mm),
                float(info.min_y_mm),
                float(info.max_x_mm),
                float(info.max_y_mm),
            )
            print(f"Gerber bounds: X={gerber_bounds[0]:.4f}-{gerber_bounds[2]:.4f} mm, "
                  f"Y={gerber_bounds[1]:.4f}-{gerber_bounds[3]:.4f} mm")

            dpmm = int(self.dpi / 25.4)
            buffer = BytesIO()
            parsed_file.render_raster(
                destination=buffer,
                dpmm=dpmm,
                pixel_format=PixelFormatEnum.RGBA,
                image_format=ImageFormatEnum.PNG,
            )

            buffer.seek(0)
            pil_image = Image.open(buffer)
            pil_image = pil_image.convert('L')
            img_array = np.array(pil_image)

            return img_array, gerber_bounds

        except Exception as e:
            print(f"Error rendering Gerber file {gerber_file}: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    def _read_edge_line_width(self, path: str) -> float:
        """Width of the Edge.Cuts stroke from its Gerber aperture, in mm."""
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            return 0.0
        widths = [float(m) for m in re.findall(r"%ADD\d+C,([\d.]+)\*%", text)]
        return max(widths) if widths else 0.0

    def smooth_path(self, path: np.ndarray, tolerance: float = 0.5) -> np.ndarray:
        """Smooth a path using Douglas-Peucker algorithm"""
        from skimage.measure import approximate_polygon
        if len(path) < 3:
            return path
        return approximate_polygon(path, tolerance=tolerance)

    def get_trace_bounds_from_bitmap(self, bitmap: np.ndarray,
                                     gerber_bounds: Tuple[float, float, float, float]
                                     ) -> Tuple[float, float, float, float]:
        """Get bounding box of traces from bitmap (min_x, min_y, max_x, max_y) in mm,
        mapped back to absolute Gerber coordinates."""
        val_min, val_max = bitmap.min(), bitmap.max()
        threshold = val_min + (val_max - val_min) * 0.4
        binary = bitmap > threshold

        rows, cols = np.where(binary)
        if len(rows) == 0:
            return gerber_bounds

        gb_min_x, gb_min_y, gb_max_x, gb_max_y = gerber_bounds
        # Pygerber uses int(dpi / 25.4) pixels/mm, so 25.4/dpi is not exact.
        # Derive the actual pixel pitch from the rendered bitmap dimensions.
        W, H = bitmap.shape[1], bitmap.shape[0]
        scale_x = (gb_max_x - gb_min_x) / (W - 1) if W > 1 else (25.4 / self.dpi)
        scale_y = (gb_max_y - gb_min_y) / (H - 1) if H > 1 else (25.4 / self.dpi)

        # Bitmap pixel coords to mm, then offset by Gerber origin
        min_x = cols.min() * scale_x + gb_min_x
        max_x = cols.max() * scale_x + gb_min_x
        # Y-axis is flipped: bitmap row 0 = top = max Y in Gerber
        min_y = gb_max_y - rows.max() * scale_y
        max_y = gb_max_y - rows.min() * scale_y

        return (min_x, min_y, max_x, max_y)

    def bitmap_to_toolpaths(self, bitmap: np.ndarray,
                            gerber_bounds: Tuple[float, float, float, float],
                            erosion_px: float = 0
                            ) -> List[List[Tuple[float, float]]]:
        """Convert bitmap to toolpaths using contour detection.
        Coordinates are mapped back to absolute Gerber coordinates using gerber_bounds.
        erosion_px: shrink copper region by this many pixels before contouring."""
        from skimage import measure, morphology

        val_min, val_max = bitmap.min(), bitmap.max()
        threshold = val_min + (val_max - val_min) * 0.4
        binary = bitmap > threshold

        if erosion_px > 0:
            # Back off 2px from the exact offset: at minimum clearance (= bit diameter)
            # a full tool-radius dilation makes opposing features touch and merge, and
            # the contour then skips the seam entirely (-> short). 2px (~34um at 1500
            # dpi) keeps such seams as thin open channels that still get cut, at the
            # cost of the bit biting ~35um into trace edges (render thresholding fattens
            # copper ~1px, partially compensating).
            disk_r = max(1, round(erosion_px) - 3)
            # Dilation by a euclidean disk == thresholding the distance transform;
            # the EDT is computed once per bitmap and reused, so many border passes
            # (isolation_border rubout) stay fast where repeated disk dilations
            # would be O(passes * r^2).
            cache = getattr(self, '_edt_cache', None)
            if cache is None or cache[0] is not bitmap:
                from scipy import ndimage
                cache = (bitmap, ndimage.distance_transform_edt(~binary))
                self._edt_cache = cache
            binary = cache[1] <= disk_r

        pad_size = 2
        binary = np.pad(binary, pad_size, mode='constant', constant_values=False)

        binary = morphology.closing(binary, morphology.disk(1))
        # skimage >=0.25 renamed the size args (min_size->max_size,
        # area_threshold->max_size). Fall back for older skimage, e.g.
        # Debian bookworm's 0.19.3 (what the Pi 3B can install).
        try:
            binary = morphology.remove_small_objects(binary, max_size=10)
            binary = morphology.remove_small_holes(binary, max_size=10)
        except TypeError:
            binary = morphology.remove_small_objects(binary, min_size=10)
            binary = morphology.remove_small_holes(binary, area_threshold=10)

        contours = measure.find_contours(binary.astype(float), 0.5)
        contours = [contour - pad_size for contour in contours]

        smooth_tolerance = max(0.5, self.dpi / 2000)
        gb_min_x, gb_min_y, gb_max_x, gb_max_y = gerber_bounds

        # Derive pixel size from actual bitmap dimensions (node-centered: pixel 0 is at
        # gb_min, pixel W-1 is at gb_max).  This avoids the ~0.5 mm systematic error that
        # arises from using 25.4/dpi when the renderer uses int(dpi/25.4) pixels/mm.
        W, H = bitmap.shape[1], bitmap.shape[0]
        scale_x = (gb_max_x - gb_min_x) / (W - 1) if W > 1 else (25.4 / self.dpi)
        scale_y = (gb_max_y - gb_min_y) / (H - 1) if H > 1 else (25.4 / self.dpi)

        x_off, y_off = self._coord_offset
        paths = []
        for contour in contours:
            smoothed = self.smooth_path(contour, tolerance=smooth_tolerance)
            # point[1] = column = X, point[0] = row = Y
            # Y-axis flipped: bitmap row 0 = top = max Y in Gerber
            path = [(point[1] * scale_x + gb_min_x + x_off,
                     gb_max_y - point[0] * scale_y + y_off)
                    for point in smoothed]
            if len(path) >= 2:
                paths.append(path)

        return paths

    def generate_alignment_mark_gcode(self, cx: float, cy: float,
                                       tool, cut_depth: float,
                                       laser: bool = False,
                                       with_drill_hole: bool = False) -> List[str]:
        """Generate G-code for an alignment mark: X crosshair inside a 3mm circle.
        cx, cy is the mark centre in absolute coordinates.
        For milling, cut_depth is the Z plunge depth (positive mm).
        For laser, laser=True omits Z moves and uses S power instead."""
        r = 1.5                    # circle radius → 3mm diameter (≤5mm limit)
        arm = r / (2 ** 0.5)      # arm half-length so endpoints touch the circle

        lines = [f"; Alignment mark  centre=({cx:.3f},{cy:.3f})  diam=3mm"]

        if laser:
            power = tool.power
            feed = tool.feed_rate
            # X arm 1: lower-left → upper-right
            lines.append(f"G0 X{cx - arm:.4f} Y{cy - arm:.4f}")
            lines.append(f"G1 X{cx + arm:.4f} Y{cy + arm:.4f} S{power} F{feed}")
            lines.append(f"G1 X{cx + arm:.4f} Y{cy + arm:.4f} S0")
            # X arm 2: upper-left → lower-right
            lines.append(f"G0 X{cx - arm:.4f} Y{cy + arm:.4f}")
            lines.append(f"G1 X{cx + arm:.4f} Y{cy - arm:.4f} S{power} F{feed}")
            lines.append(f"G1 X{cx + arm:.4f} Y{cy - arm:.4f} S0")
            # Full circle (CCW) as small segments, start/end at left-most point
            lines.append(f"G0 X{cx - r:.4f} Y{cy:.4f}")
            import math
            circumference = 2 * math.pi * r
            num_segments = max(6, int(math.ceil(circumference / 0.1)))
            angle_step = 2 * math.pi / num_segments
            for i in range(1, num_segments + 1):
                angle = math.pi + i * angle_step  # start from left, go CCW
                x = cx + r * math.cos(angle)
                y = cy + r * math.sin(angle)
                lines.append(f"G1 X{x:.4f} Y{y:.4f} S{power} F{feed}")
            lines.append(f"G1 X{cx - r:.4f} Y{cy:.4f} S0")
        else:
            feed = tool.feed_rate
            plunge = tool.plunge_rate
            safe_h = self.safe_height
            depth = -abs(cut_depth)
            # X arm 1: lower-left → upper-right
            lines.append(f"G0 X{cx - arm:.4f} Y{cy - arm:.4f} Z{safe_h}")
            lines.append(f"G1 Z{depth:.4f} F{plunge}")
            lines.append(f"G1 X{cx + arm:.4f} Y{cy + arm:.4f} F{feed}")
            lines.append(f"G0 Z{safe_h}")
            # X arm 2: upper-left → lower-right
            lines.append(f"G0 X{cx - arm:.4f} Y{cy + arm:.4f}")
            lines.append(f"G1 Z{depth:.4f} F{plunge}")
            lines.append(f"G1 X{cx + arm:.4f} Y{cy - arm:.4f} F{feed}")
            lines.append(f"G0 Z{safe_h}")
            # Full circle (CCW) as small segments, start/end at left-most point
            lines.append(f"G0 X{cx - r:.4f} Y{cy:.4f}")
            lines.append(f"G1 Z{depth:.4f} F{plunge}")
            import math
            circumference = 2 * math.pi * r
            num_segments = max(6, int(math.ceil(circumference / 0.1)))
            angle_step = 2 * math.pi / num_segments
            for i in range(1, num_segments + 1):
                angle = math.pi + i * angle_step  # start from left, go CCW
                x = cx + r * math.cos(angle)
                y = cy + r * math.sin(angle)
                lines.append(f"G1 X{x:.4f} Y{y:.4f} F{feed}")
            lines.append(f"G0 Z{safe_h}")

            if with_drill_hole:
                drill = self.drill_tool
                lines.append(f"; Alignment drill hole at centre — change to {drill.tool_diameter:.2f} mm drill bit")
                lines.extend(self.generate_tool_change(
                    f"alignment drill ({drill.tool_diameter:.2f} mm)", drill))
                lines.append(f"G0 X{cx:.4f} Y{cy:.4f} Z{self.safe_height}")
                if drill.cut_depth > 0 and drill.cut_depth < drill.total_depth:
                    # Peck drilling
                    current_depth = 0.0
                    while current_depth < drill.total_depth:
                        current_depth = min(current_depth + drill.cut_depth, drill.total_depth)
                        lines.append(f"G1 Z{-current_depth:.4f} F{drill.plunge_rate}")
                        lines.append(f"G0 Z{drill.retract_height}")
                    lines.append(f"G0 Z{self.safe_height}")
                else:
                    lines.append(f"G1 Z{-drill.total_depth:.4f} F{drill.plunge_rate}")
                    lines.append(f"G0 Z{self.safe_height}")

        return lines

    def generate_soldermask_png(self, mask_file: str, output_path: str):
        """Render solder mask Gerber to a printable PNG sized for physical printing.

        The PNG has embedded DPI metadata so printing at 100% scale produces the
        correct physical board size.  By default the image is inverted so that pad
        openings are dark (positive photoemulsion process: UV is blocked over pads,
        emulsion washes away there, leaving bare copper exposed).
        """
        from io import BytesIO

        print_dpi = self.soldermask_print_dpi
        invert = self.soldermask_invert

        try:
            gf = GerberFile.from_file(mask_file, file_type=FileTypeEnum.INFER_FROM_EXTENSION)
            parsed_file = gf.parse()

            info = parsed_file.get_info()
            bounds = (
                float(info.min_x_mm), float(info.min_y_mm),
                float(info.max_x_mm), float(info.max_y_mm),
            )
            width_mm = bounds[2] - bounds[0]
            height_mm = bounds[3] - bounds[1]

            dpmm = int(print_dpi / 25.4)
            buffer = BytesIO()
            parsed_file.render_raster(
                destination=buffer,
                dpmm=dpmm,
                pixel_format=PixelFormatEnum.RGBA,
                image_format=ImageFormatEnum.PNG,
            )
            buffer.seek(0)
            img = Image.open(buffer).convert('L')

            # Binarize before inversion: drawn areas (bright) → 255, background/transparent → 0
            arr = np.where(np.array(img) > 64, 255, 0).astype(np.uint8)
            if invert:
                arr = 255 - arr
            img = Image.fromarray(arr)

            # Save at the actual rendered DPI (dpmm * 25.4), not the requested print_dpi.
            # int(print_dpi / 25.4) truncates, so the rendered pixel density is slightly
            # different from print_dpi; using the actual value prevents a systematic
            # scale error of up to ~2.6% (≈1 mm over a 40 mm board at 600 DPI).
            actual_dpi = dpmm * 25.4
            img.save(output_path, dpi=(actual_dpi, actual_dpi))
            print(f"  Physical size: {width_mm:.2f} x {height_mm:.2f} mm "
                  f"({img.width} x {img.height} px @ {actual_dpi:.1f} DPI)")
            print(f"  {'Inverted — positive photoemulsion (dark pads)' if invert else 'Not inverted — negative photoemulsion (clear pads)'}")
            print(f"  Soldermask PNG written to: {output_path}")

        except Exception as e:
            print(f"Error generating soldermask PNG: {e}")
            import traceback
            traceback.print_exc()

    def process_back_traces(self, bitmap: np.ndarray,
                            gerber_bounds: Tuple[float, float, float, float],
                            board_bounds: Tuple[float, float, float, float] = None) -> List[str]:
        """Generate isolation G-code for the back copper layer (ground plane / back traces).

        The bitmap is mirrored horizontally to account for physically flipping the board
        left-to-right before milling the back side.  X coordinates in the output G-code
        therefore correspond to the correct machine positions when the board is flipped.
        Tool settings come from the back_isolation_tool (defaults to isolation settings).
        """
        lines = ["; Back copper isolation — board mirrored for back-side milling"]

        # Pad to board bounds BEFORE flipping.  If we flip first, the left/right sides
        # are swapped, so _pad_bitmap_to_bounds would add padding to the wrong sides.
        if board_bounds is not None:
            bitmap, _ = self._pad_bitmap_to_bounds(bitmap, gerber_bounds, board_bounds)
            gerber_bounds = board_bounds  # Force exact match — process_traces adds zero secondary padding

        mirrored = np.fliplr(bitmap)

        # Alignment marks and verification pause BEFORE back-side isolation traces.
        # After flipping the board left-right, set machine origin at the (W,0) drill hole
        # (now the lower-left).  The marks below should then land on the two bottom holes.
        if board_bounds is not None:
            x_off, y_off = self._coord_offset
            mark_a_x = board_bounds[0] + x_off   # = 0.0  (lower-left after flip = old lower-right hole)
            mark_a_y = board_bounds[1] + y_off   # = 0.0
            mark_b_x = board_bounds[2] + x_off   # = board_width (lower-right after flip = old lower-left hole)
            mark_b_y = mark_a_y
            print(f"  Back alignment mark A at ({mark_a_x:.2f}, {mark_a_y:.2f})")
            print(f"  Back alignment mark B at ({mark_b_x:.2f}, {mark_b_y:.2f})")
            lines.append("; === ALIGNMENT MARKS — milled before isolation, verify against drill holes ===")
            lines.append(f"; Mark A at ({mark_a_x:.3f}, {mark_a_y:.3f}) — lower-left (old lower-right drill hole)")
            lines.extend(self.generate_alignment_mark_gcode(
                mark_a_x, mark_a_y, self.back_isolation_tool,
                self.back_isolation_tool.cut_depth, laser=False, with_drill_hole=False))
            lines.append("")
            lines.append(f"; Mark B at ({mark_b_x:.3f}, {mark_b_y:.3f}) — lower-right (old lower-left drill hole)")
            lines.extend(self.generate_alignment_mark_gcode(
                mark_b_x, mark_b_y, self.back_isolation_tool,
                self.back_isolation_tool.cut_depth, laser=False, with_drill_hole=False))
            lines.append("")
            lines.append("; === PAUSE — inspect alignment marks vs drill holes, then resume ===")
            lines.append(f"G0 Z{self.safe_height}    ; Raise to safe height")
            lines.append("M5          ; Stop spindle")
            lines.append("M0          ; ** INSPECT: marks should be centred on drill holes — resume to mill traces **")
            lines.append(f"M3 S{self.back_isolation_tool.spindle_speed}  ; Restart spindle")
            lines.append("G4 P2       ; Dwell for spindle")
            lines.append("")

        # Generate isolation toolpaths using back isolation tool settings.
        saved_tool = self.isolation_tool
        self.isolation_tool = self.back_isolation_tool
        try:
            isolation_lines = self.process_traces(mirrored, gerber_bounds, board_bounds,
                                                  add_alignment_mark=False)
        finally:
            self.isolation_tool = saved_tool

        lines.extend(isolation_lines)
        return lines

    def process_traces(self, bitmap: np.ndarray,
                       gerber_bounds: Tuple[float, float, float, float],
                       board_bounds: Tuple[float, float, float, float] = None,
                       add_alignment_mark: bool = True) -> List[str]:
        """Generate G-code for isolation routing of traces.

        When add_alignment_mark=True (front side), two alignment marks are milled at
        (0,0) and (board_width,0) BEFORE the isolation traces, followed by an M0 pause
        so the operator can verify alignment against the drill holes before milling starts.
        """
        tool = self.isolation_tool
        gcode_lines = ["; Isolation routing"]

        scale_factor = 25.4 / self.dpi

        # Alignment marks and verification pause BEFORE isolation traces (front side only).
        # The operator checks that the milled marks land on the drilled alignment holes;
        # if they do, the board is correctly registered and milling can proceed.
        if add_alignment_mark and board_bounds is not None:
            x_off, y_off = self._coord_offset
            mark_a_x = board_bounds[0] + x_off   # = 0.0
            mark_a_y = board_bounds[1] + y_off   # = 0.0
            mark_b_x = board_bounds[2] + x_off   # = board_width
            mark_b_y = mark_a_y
            print(f"  Alignment mark A at ({mark_a_x:.2f}, {mark_a_y:.2f})  [lower-left]")
            print(f"  Alignment mark B at ({mark_b_x:.2f}, {mark_b_y:.2f})  [lower-right, {mark_b_x - mark_a_x:.2f} mm]")
            gcode_lines.append("; === ALIGNMENT MARKS — milled before isolation, centres match drill holes ===")
            gcode_lines.append(f"; Mark A at ({mark_a_x:.3f}, {mark_a_y:.3f}) — lower-left drill hole")
            gcode_lines.extend(self.generate_alignment_mark_gcode(
                mark_a_x, mark_a_y, tool, tool.cut_depth, laser=False, with_drill_hole=False))
            gcode_lines.append("")
            gcode_lines.append(f"; Mark B at ({mark_b_x:.3f}, {mark_b_y:.3f}) — lower-right drill hole")
            gcode_lines.extend(self.generate_alignment_mark_gcode(
                mark_b_x, mark_b_y, tool, tool.cut_depth, laser=False, with_drill_hole=False))
            gcode_lines.append("")
            gcode_lines.append("; === PAUSE — inspect alignment marks vs drill holes, then resume ===")
            gcode_lines.append(f"G0 Z{self.safe_height}    ; Raise to safe height")
            gcode_lines.append("M5          ; Stop spindle")
            gcode_lines.append("M0          ; ** INSPECT: marks should be centred on drill holes — resume to mill traces **")
            gcode_lines.append(f"M3 S{tool.spindle_speed}  ; Restart spindle")
            gcode_lines.append("G4 P2       ; Dwell for spindle")
            gcode_lines.append("")

        # Pad bitmap to board bounds so traces at the edges of the copper region
        # get fully closed contours and coordinates align with edge cuts / drill layers.
        if board_bounds is not None:
            bitmap, _ = self._pad_bitmap_to_bounds(bitmap, gerber_bounds, board_bounds)
            gerber_bounds = board_bounds  # Force exact bounds so both sides share the same origin
            print(f"  Bitmap padded to board bounds: "
                  f"X={board_bounds[0]:.2f}-{board_bounds[2]:.2f} mm, "
                  f"Y={board_bounds[1]:.2f}-{board_bounds[3]:.2f} mm")

        if tool.isolation_border > 0:
            n_extra = max(0, int(np.ceil(tool.isolation_border / tool.step_over)))
        else:
            n_extra = max(0, tool.passes - 1)

        for pass_num in range(n_extra + 1):
            # Offset the contour outward by the tool RADIUS so the bit's EDGE (not its
            # centerline) kisses the copper boundary. Without this every feature loses
            # half a bit width per side — a track as wide as the bit mills away entirely.
            erosion_mm = tool.tool_diameter / 2.0 + pass_num * tool.step_over
            erosion_px = erosion_mm / scale_factor
            paths = self.bitmap_to_toolpaths(bitmap, gerber_bounds, erosion_px=erosion_px)
            label = "primary" if pass_num == 0 else f"border +{erosion_mm:.2f}mm"
            print(f"  Pass {pass_num + 1} ({label}): {len(paths)} contours")
            gcode_lines.append(f"; Isolation pass {pass_num + 1} ({label})")

            for path in paths:
                if len(path) < 2:
                    continue
                x, y = path[0]
                gcode_lines.append(f"G0 X{x:.4f} Y{y:.4f} Z{self.safe_height}")
                gcode_lines.append(f"G1 Z{-tool.cut_depth:.4f} F{tool.plunge_rate}")
                for x, y in path[1:]:
                    gcode_lines.append(f"G1 X{x:.4f} Y{y:.4f} F{tool.feed_rate}")
                gcode_lines.append(f"G0 Z{self.safe_height}")

            gcode_lines.append("")

        return gcode_lines

    def generate_laser_gcode_header(self, tool) -> str:
        """Generate G-code header for laser operation (GRBL laser mode, $32=1)"""
        mode = "M4" if tool.dynamic_mode else "M3"
        return "\n".join([
            "; Laser G-code — requires GRBL laser mode ($32=1)",
            "G21         ; mm",
            "G90         ; absolute",
            "G94         ; feed per minute",
            f"{mode} S0   ; enable laser driver (power off)",
            "",
        ])

    def generate_laser_gcode_footer(self) -> str:
        """Generate G-code footer for laser operation"""
        return "\n".join(["", "M5    ; Laser off", "G0 X0 Y0", "M2", ""])

    def detect_pads_and_traces(self, bitmap: np.ndarray,
                               gerber_bounds: Tuple[float, float, float, float]):
        """Classify copper regions into pads and traces using connected component analysis.
        Returns (pad_mask, trace_mask) boolean arrays."""
        from skimage import measure

        val_min, val_max = bitmap.min(), bitmap.max()
        binary = bitmap > (val_min + (val_max - val_min) * 0.4)

        labeled = measure.label(binary)
        regions = measure.regionprops(labeled)
        scale_factor = 25.4 / self.dpi
        min_area_px = self.laser_tool.pad_min_area / (scale_factor ** 2)

        pad_mask = np.zeros_like(binary, dtype=bool)
        trace_mask = np.zeros_like(binary, dtype=bool)

        for region in regions:
            if (region.area > min_area_px
                    and region.eccentricity < self.laser_tool.pad_max_eccentricity
                    and region.solidity > 0.7):
                pad_mask[labeled == region.label] = True
            else:
                trace_mask[labeled == region.label] = True

        print(f"  Detected {measure.label(pad_mask).max()} pad region(s)")
        return pad_mask, trace_mask

    def generate_pad_fills_from_mask(self, mask_bitmap: np.ndarray,
                                     gerber_bounds: Tuple[float, float, float, float]) -> List[str]:
        """Generate raster fill G-code directly from a solder mask Gerber bitmap.
        Every bright region in the mask layer is a mask opening and gets filled."""
        from skimage import measure

        tool = self.laser_tool
        scale_factor = 25.4 / self.dpi
        gb_min_x, _, _, gb_max_y = gerber_bounds
        fill_step_px = max(1.0, tool.fill_line_spacing / scale_factor)

        val_min, val_max = mask_bitmap.min(), mask_bitmap.max()
        binary = mask_bitmap > (val_min + (val_max - val_min) * 0.4)

        labeled = measure.label(binary)
        regions = measure.regionprops(labeled)
        print(f"  Filling {len(regions)} mask opening(s) from solder mask layer")

        gcode_lines = ["; Laser pad fill from solder mask (raster)"]
        for region in regions:
            min_row, _, max_row, _ = region.bbox
            region_mask = (labeled == region.label)
            row = float(min_row)
            direction = 1
            while row < max_row:
                row_int = min(int(row), max_row - 1)
                cols = np.where(region_mask[row_int, :])[0]
                if len(cols) >= 2:
                    c_s, c_e = (cols[0], cols[-1]) if direction == 1 else (cols[-1], cols[0])
                    x_s = c_s * scale_factor + gb_min_x
                    x_e = c_e * scale_factor + gb_min_x
                    y = gb_max_y - row_int * scale_factor
                    gcode_lines.append(f"G0 X{x_s:.4f} Y{y:.4f}")
                    gcode_lines.append(f"G1 X{x_e:.4f} Y{y:.4f} S{tool.pad_power} F{tool.pad_feed_rate}")
                    direction *= -1
                row += fill_step_px
        return gcode_lines

    def generate_pad_fills(self, pad_mask: np.ndarray,
                           gerber_bounds: Tuple[float, float, float, float]) -> List[str]:
        """Generate boustrophedon raster fill G-code for pad regions"""
        from skimage import measure

        tool = self.laser_tool
        scale_factor = 25.4 / self.dpi
        gb_min_x, _, gb_max_x, gb_max_y = gerber_bounds
        fill_step_px = max(1.0, tool.fill_line_spacing / scale_factor)

        gcode_lines = ["; Laser pad fill (raster)"]
        labeled = measure.label(pad_mask)
        for region in measure.regionprops(labeled):
            min_row, _, max_row, _ = region.bbox
            region_mask = (labeled == region.label)
            row = float(min_row)
            direction = 1
            while row < max_row:
                row_int = min(int(row), max_row - 1)
                cols = np.where(region_mask[row_int, :])[0]
                if len(cols) >= 2:
                    c_s, c_e = (cols[0], cols[-1]) if direction == 1 else (cols[-1], cols[0])
                    x_s = c_s * scale_factor + gb_min_x
                    x_e = c_e * scale_factor + gb_min_x
                    y = gb_max_y - row_int * scale_factor
                    gcode_lines.append(f"G0 X{x_s:.4f} Y{y:.4f}")
                    gcode_lines.append(f"G1 X{x_e:.4f} Y{y:.4f} S{tool.pad_power} F{tool.pad_feed_rate}")
                    direction *= -1
                row += fill_step_px
        return gcode_lines

    def _pad_bitmap_to_bounds(self, bitmap: np.ndarray,
                              gerber_bounds: Tuple[float, float, float, float],
                              target_bounds: Tuple[float, float, float, float]
                              ) -> Tuple[np.ndarray, Tuple[float, float, float, float]]:
        """Pad a bitmap with zeros so its coordinate space extends to target_bounds.
        Returns (padded_bitmap, new_gerber_bounds). target_bounds must be >= gerber_bounds."""
        scale_factor = 25.4 / self.dpi
        gb_min_x, gb_min_y, gb_max_x, gb_max_y = gerber_bounds
        tb_min_x, tb_min_y, tb_max_x, tb_max_y = target_bounds

        # Pixels to add on each side (image row 0 = Gerber max_y, so top/bottom are flipped)
        pad_left   = max(0, round((gb_min_x - tb_min_x) / scale_factor))
        pad_right  = max(0, round((tb_max_x - gb_max_x) / scale_factor))
        pad_top    = max(0, round((tb_max_y - gb_max_y) / scale_factor))
        pad_bottom = max(0, round((gb_min_y - tb_min_y) / scale_factor))

        padded = np.pad(bitmap, ((pad_top, pad_bottom), (pad_left, pad_right)),
                        mode='constant', constant_values=0)
        new_bounds = (
            gb_min_x - pad_left   * scale_factor,
            gb_min_y - pad_bottom * scale_factor,
            gb_max_x + pad_right  * scale_factor,
            gb_max_y + pad_top    * scale_factor,
        )
        return padded, new_bounds

    def process_laser(self, bitmap: np.ndarray,
                      gerber_bounds: Tuple[float, float, float, float],
                      mask_bitmap: np.ndarray = None,
                      mask_gerber_bounds: Tuple[float, float, float, float] = None,
                      board_bounds: Tuple[float, float, float, float] = None):
        """Generate laser G-code for solder mask removal.
        Returns (trace_lines, pad_lines) as separate lists for writing to separate files.
        If mask_bitmap is provided it is used directly for pad fills using its own
        mask_gerber_bounds; otherwise pad regions are detected heuristically from the
        copper layer bitmap."""
        tool = self.laser_tool

        if tool.trace_border_passes > 0:
            if board_bounds is None:
                raise ValueError(
                    f"trace_border_passes={tool.trace_border_passes} requires board boundary "
                    "information, but none is available.\n"
                    "Supply an edge cuts file (-e) or a copper traces file (-t) so a board "
                    "boundary can be established, or set trace_border_passes to 0."
                )
            bitmap, gerber_bounds = self._pad_bitmap_to_bounds(
                bitmap, gerber_bounds, board_bounds)
            print(f"  Bitmap padded to board bounds: "
                  f"X={board_bounds[0]:.2f}-{board_bounds[2]:.2f} mm, "
                  f"Y={board_bounds[1]:.2f}-{board_bounds[3]:.2f} mm")

        scale_factor = 25.4 / self.dpi
        trace_lines = ["; Laser trace isolation contours"]
        for pass_num in range(tool.trace_border_passes + 1):
            dilation_mm = pass_num * tool.fill_line_spacing
            dilation_px = dilation_mm / scale_factor
            paths = self.bitmap_to_toolpaths(bitmap, gerber_bounds, erosion_px=dilation_px)
            label = "edge" if pass_num == 0 else f"outside +{dilation_mm:.2f}mm"
            print(f"  Trace border pass {pass_num + 1} ({label}): {len(paths)} contour(s)")
            trace_lines.append(f"; Trace border pass {pass_num + 1} ({label})")
            for path in paths:
                if len(path) < 2:
                    continue
                x, y = path[0]
                trace_lines.append(f"G0 X{x:.4f} Y{y:.4f}")
                for x, y in path:
                    trace_lines.append(f"G1 X{x:.4f} Y{y:.4f} S{tool.power} F{tool.feed_rate}")
                trace_lines.append(f"G1 X{x:.4f} Y{y:.4f} S0")

        if mask_bitmap is not None:
            pad_bounds = mask_gerber_bounds if mask_gerber_bounds is not None else gerber_bounds
            pad_lines = self.generate_pad_fills_from_mask(mask_bitmap, pad_bounds)
        else:
            pad_mask, _ = self.detect_pads_and_traces(bitmap, gerber_bounds)
            pad_lines = self.generate_pad_fills(pad_mask, gerber_bounds) if np.any(pad_mask) else []

        # Alignment mark appended to trace file so it is cut with the same
        # tool/power settings as the trace borders.
        if board_bounds is not None:
            x_off, y_off = self._coord_offset
            mark_x = board_bounds[0] + x_off - 3.5   # = -3.5 (outside board lower-left)
            mark_y = board_bounds[1] + y_off - 3.5   # = -3.5
            print(f"  Alignment mark at ({mark_x:.2f}, {mark_y:.2f})")
            trace_lines.append("")
            trace_lines.extend(self.generate_alignment_mark_gcode(
                mark_x, mark_y, tool, 0, laser=True))

        return trace_lines, pad_lines

    def parse_edge_cuts_to_outline(self, bitmap: np.ndarray,
                                   gerber_bounds: Tuple[float, float, float, float]
                                   ) -> List[Tuple[float, float]]:
        """Parse edge cuts bitmap and return board outline in absolute Gerber coordinates.

        The outline is offset OUTWARD by the edge-cuts tool radius so the bit's
        EDGE (not its centerline) kisses the Edge.Cuts line. Without this every
        edge loses half a bit width per side — a 0.8 mm bit on a 24x47 board
        mills out at ~23x46. Mirrors the isolation tool-radius offset.
        """
        from skimage import measure, morphology
        from scipy import ndimage

        val_min, val_max = bitmap.min(), bitmap.max()
        threshold = val_min + (val_max - val_min) * 0.4
        binary = bitmap > threshold

        # Offset the (zero-width) Edge.Cuts line outward by tool_radius so the
        # bit's inner edge rides the drawn line and the finished board matches
        # the drawn outline (no per-side undersizing). The Edge.Cuts renders as
        # a thin outline, so: pad with zeros (room to expand past the gerber
        # bounds), fill the interior (solid region -> single outer contour
        # instead of a band with inner+outer), dilate by tool_radius (grow
        # outward), then trace the one remaining contour and un-pad.
        # Actual pixel pitch from the rendered bitmap (pygerber rounds px/mm down).
        W, H = bitmap.shape[1], bitmap.shape[0]
        gb_min_x, gb_min_y, gb_max_x, gb_max_y = gerber_bounds
        scale_x = (gb_max_x - gb_min_x) / (W - 1) if W > 1 else (25.4 / self.dpi)
        scale_y = (gb_max_y - gb_min_y) / (H - 1) if H > 1 else (25.4 / self.dpi)
        scale_factor = (scale_x + scale_y) / 2.0
        tool_radius_mm = self.edge_cuts_tool.tool_diameter / 2.0
        # Filling the rendered Edge.Cuts line and dilating from its OUTER edge
        # adds half the line width. Subtract that so the bit centre lands one
        # tool radius from the Edge.Cuts centreline.
        line_half_mm = getattr(self, "edge_line_width_mm", 0.0) / 2.0
        tool_radius_px = (tool_radius_mm - line_half_mm) / scale_factor
        disk_r = max(1, round(tool_radius_px))
        pad = disk_r + 5
        binary = np.pad(binary, pad, mode='constant', constant_values=False)
        binary = ndimage.binary_fill_holes(binary)
        # EDT threshold == euclidean-disk dilation but O(n) (same trick as the
        # isolation tool-radius offset above). skimage <0.25 routes bool images
        # through the slow greyscale path here (scipy grey_dilation with a
        # 2*disk_r+1 footprint); on the Pi 3B (Debian's skimage 0.19.3) that
        # takes tens of minutes for a board-size bitmap instead of seconds.
        binary = ndimage.distance_transform_edt(~binary) <= disk_r

        contours = measure.find_contours(binary.astype(float), 0.5)

        if not contours:
            return []

        largest_contour = max(contours, key=len) - pad  # un-pad
        smoothed = self.smooth_path(largest_contour, tolerance=1.0)
        gb_min_x, gb_min_y, gb_max_x, gb_max_y = gerber_bounds

        outline = [(point[1] * scale_x + gb_min_x,
                    gb_max_y - point[0] * scale_y)
                   for point in smoothed]

        return outline

    def impute_edge_cuts(self, trace_bounds: Tuple[float, float, float, float]) -> List[Tuple[float, float]]:
        """Generate rectangular board outline from trace bounds plus margin"""
        min_x, min_y, max_x, max_y = trace_bounds
        # Add tool radius so the bit's edge (not centerline) hits the intended
        # outline — mirrors the parse_edge_cuts_to_outline dilation offset.
        margin = self.edge_margin + self.edge_cuts_tool.tool_diameter / 2.0

        outline = [
            (min_x - margin, min_y - margin),
            (max_x + margin, min_y - margin),
            (max_x + margin, max_y + margin),
            (min_x - margin, max_y + margin),
            (min_x - margin, min_y - margin),
        ]

        return outline

    def process_edge_cuts(self, outline: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for board cutout"""
        if not outline:
            return []

        tool = self.edge_cuts_tool
        gcode_lines = [
            "; Board cutout",
            f"; Total depth: {tool.total_depth} mm",
        ]

        passes_needed = max(1, int(np.ceil(tool.total_depth / tool.cut_depth)))
        x_off, y_off = self._coord_offset

        for pass_num in range(passes_needed):
            current_depth = min((pass_num + 1) * tool.cut_depth, tool.total_depth)
            gcode_lines.append(f"; Cutout pass {pass_num + 1} (depth: {current_depth:.2f} mm)")

            x, y = outline[0]
            gcode_lines.append(f"G0 X{x + x_off:.4f} Y{y + y_off:.4f} Z{self.safe_height}")
            gcode_lines.append(f"G1 Z{-current_depth:.4f} F{tool.plunge_rate}")

            for x, y in outline[1:]:
                gcode_lines.append(f"G1 X{x + x_off:.4f} Y{y + y_off:.4f} F{tool.feed_rate}")

            gcode_lines.append(f"G0 Z{self.safe_height}")

        return gcode_lines

    def parse_drill_file(self, drill_file: str) -> List[Tuple[float, float, float]]:
        """Parse Excellon drill file and return list of (x, y, diameter) holes"""
        holes = []

        try:
            with open(drill_file, 'r') as f:
                content = f.read()

            # Parse tool definitions (T01C0.8 = tool 1, 0.8mm diameter)
            tool_diameters = {}
            tool_pattern = re.compile(r'T(\d+)C([\d.]+)')
            for match in tool_pattern.finditer(content):
                tool_num = int(match.group(1))
                diameter = float(match.group(2))
                tool_diameters[tool_num] = diameter

            # Parse coordinates
            current_tool = None
            # Excellon format can be INCH or METRIC
            unit_scale = 1.0  # Assume mm by default

            if 'INCH' in content.upper():
                unit_scale = 25.4
            elif 'METRIC' in content.upper() or 'M71' in content:
                unit_scale = 1.0

            lines = content.split('\n')
            for line in lines:
                line = line.strip()

                # Tool selection
                tool_select = re.match(r'^T(\d+)$', line)
                if tool_select:
                    current_tool = int(tool_select.group(1))
                    continue

                # Coordinate (X123Y456 format)
                coord_match = re.match(r'^X([-\d.]+)Y([-\d.]+)', line)
                if coord_match and current_tool is not None:
                    x = float(coord_match.group(1))
                    y = float(coord_match.group(2))

                    # Handle integer format (divide by 10000 for 2.4 format)
                    if '.' not in coord_match.group(1):
                        x = x / 10000.0
                    if '.' not in coord_match.group(2):
                        y = y / 10000.0

                    x *= unit_scale
                    y *= unit_scale

                    diameter = tool_diameters.get(current_tool, self.drill_tool.tool_diameter)
                    holes.append((x, y, diameter))

            print(f"Found {len(holes)} drill holes")
            return holes

        except Exception as e:
            print(f"Error parsing drill file: {e}")
            return []

    def process_drill_holes(self, holes: List[Tuple[float, float, float]],
                            board_bounds: Tuple[float, float, float, float] = None) -> List[str]:
        """Generate G-code for drilling holes.

        If board_bounds is provided, three alignment holes are drilled first at the board
        corners (0,0), (W,0), and (W,H) so the board can be precisely re-homed when
        flipped for back-side milling.
        """
        tool = self.drill_tool
        gcode_lines = [
            "; Drilling",
            f"; Total depth: {tool.total_depth} mm",
            "",
        ]

        def _bore_at(gx: float, gy: float, diameter: float) -> List[str]:
            # A single helix at (D-d)/2 only cuts an annulus. For D > 2d that
            # leaves a central pillar, so use concentric full-depth passes at
            # radii down to the bit radius (the bit then reaches the centre).
            # Uses small G1 segments instead of G2 arcs to avoid firmware arc
            # segmentation. Each pass gets a flat final circle to clean its floor.
            import math
            bit_radius = tool.tool_diameter / 2.0
            r_outer = (diameter - tool.tool_diameter) / 2.0
            step = tool.tool_diameter * 0.5
            radii = []
            r = r_outer
            while True:
                radii.append(r)
                if r <= bit_radius + 1e-9:
                    break
                r = max(0.0, r - step)

            cmds = [
                f"; Bore {diameter:.2f} mm "
                f"(radii={','.join('%.3f' % rr for rr in radii)})"
            ]
            for r in radii:
                circumference = 2 * math.pi * r
                num_segments = max(6, int(math.ceil(circumference / 0.1)))
                angle_step = 2 * math.pi / num_segments
                cmds += [
                    f"G0 X{gx + r:.4f} Y{gy:.4f} Z{self.safe_height}",
                    f"G1 Z0 F{tool.plunge_rate}",
                ]

                depth = 0.0
                while depth < tool.total_depth:
                    prev = depth
                    depth = min(depth + tool.bore_step_down, tool.total_depth)
                    # Helix down one revolution with segments
                    for i in range(1, num_segments + 1):
                        angle = i * angle_step
                        x = gx + r * math.cos(angle)
                        y = gy + r * math.sin(angle)
                        # Z descends gradually during the revolution (prev -> depth)
                        z = -(prev + (depth - prev) * i / num_segments)
                        cmds.append(f"G1 X{x:.4f} Y{y:.4f} Z{z:.4f} F{tool.feed_rate}")

                # Final flat circle at bottom to clean this pass's floor
                for i in range(1, num_segments + 1):
                    angle = i * angle_step
                    x = gx + r * math.cos(angle)
                    y = gy + r * math.sin(angle)
                    cmds.append(f"G1 X{x:.4f} Y{y:.4f} Z{-tool.total_depth:.4f} F{tool.feed_rate}")

                cmds.append(f"G0 Z{self.safe_height}")

            # Remove the last tiny centre nub left by the innermost tangent pass.
            if len(radii) > 1:
                cmds += [
                    f"G0 X{gx:.4f} Y{gy:.4f} Z{self.safe_height}",
                    f"G1 Z{-tool.total_depth:.4f} F{tool.plunge_rate}",
                    f"G0 Z{self.safe_height}",
                ]
            return cmds

        def _drill_at(gx: float, gy: float, diameter: float = None) -> List[str]:
            if diameter is not None and diameter - tool.tool_diameter > tool.bore_tolerance + 1e-6:
                return _bore_at(gx, gy, diameter)
            cmds = [f"G0 X{gx:.4f} Y{gy:.4f} Z{self.safe_height}"]
            if tool.cut_depth > 0 and tool.cut_depth < tool.total_depth:
                current_depth = 0.0
                while current_depth < tool.total_depth:
                    current_depth = min(current_depth + tool.cut_depth, tool.total_depth)
                    cmds.append(f"G1 Z{-current_depth:.4f} F{tool.plunge_rate}")
                    cmds.append(f"G0 Z{tool.retract_height}")
                cmds.append(f"G0 Z{self.safe_height}")
            else:
                cmds.append(f"G1 Z{-tool.total_depth:.4f} F{tool.plunge_rate}")
                cmds.append(f"G0 Z{self.safe_height}")
            return cmds

        # Alignment holes at board corners — drilled before PCB holes so the user
        # can immediately verify position.  Three non-collinear corners fully constrain
        # translation AND rotation; (0,0), (W,0), and (W,H) form an L along the bottom
        # and right edge, matching the flip workflow.
        if board_bounds is not None:
            x_off, y_off = self._coord_offset
            corners = [
                (board_bounds[0] + x_off, board_bounds[1] + y_off),  # (0, 0)  lower-left
                (board_bounds[2] + x_off, board_bounds[1] + y_off),  # (W, 0)  lower-right
                (board_bounds[2] + x_off, board_bounds[3] + y_off),  # (W, H)  upper-right
            ]
            labels = ["(0,0) lower-left", "(W,0) lower-right", "(W,H) upper-right"]
            gcode_lines.append("; === ALIGNMENT HOLES — drill before flipping, used for front/back registration ===")
            for (ax, ay), lbl in zip(corners, labels):
                gcode_lines.append(f"; Alignment hole {lbl}")
                gcode_lines.extend(_drill_at(ax, ay))
            gcode_lines.append("; === END ALIGNMENT HOLES ===")
            gcode_lines.append("")

        if not holes:
            return gcode_lines

        # Group PCB holes by diameter for efficient tool changes
        holes_by_diameter: Dict[float, List[Tuple[float, float]]] = {}
        for x, y, d in holes:
            holes_by_diameter.setdefault(d, []).append((x, y))

        x_off, y_off = self._coord_offset
        for diameter, hole_list in sorted(holes_by_diameter.items()):
            gcode_lines.append(f"; PCB holes: {diameter:.2f} mm diameter ({len(hole_list)} holes)")
            for x, y in hole_list:
                gcode_lines.extend(_drill_at(x + x_off, y + y_off, diameter))

        return gcode_lines

    def generate_edge_cuts_gerber(self, outline: List[Tuple[float, float]], output_file: str):
        """Generate a Gerber file for the board edge cuts"""
        gerber_lines = [
            "G04 Edge cuts generated by Gerber to G-code converter*",
            "%FSLAX36Y36*%",
            "%MOIN*%",
            "%ADD10C,0.01*%",
            "D10*",
            "G01*",
        ]

        for i, (x, y) in enumerate(outline):
            x_inch = x / 25.4
            y_inch = y / 25.4
            x_gerber = int(x_inch * 1000000)
            y_gerber = int(y_inch * 1000000)

            if i == 0:
                gerber_lines.append(f"X{x_gerber}Y{y_gerber}D02*")
            else:
                gerber_lines.append(f"X{x_gerber}Y{y_gerber}D01*")

        gerber_lines.append("M02*")

        with open(output_file, 'w') as f:
            f.write('\n'.join(gerber_lines))

        print(f"Edge cuts Gerber written to {output_file}")

    def convert(self, traces_file: str = None, edge_cuts_file: str = None,
                drill_file: str = None, output_file: str = None,
                generate_edge_cuts: str = None, laser_layer: str = None,
                mask_layer: str = None, back_traces_file: str = None,
                soldermask_png: str = None):
        """Main conversion process"""

        self.traces_file = traces_file
        self.edge_cuts_file = edge_cuts_file
        self.drill_file = drill_file
        self.output_file = output_file or 'output.nc'

        operations = []
        gcode_sections = {}

        # Phase 1: Render trace bitmap (geometry only; G-code generated after board
        # bounds are known so the bitmap can be padded to the full board extent).
        trace_bitmap = None
        trace_gerber_bounds = None
        if traces_file and os.path.exists(traces_file):
            print(f"\n=== Processing traces: {traces_file} ===")
            trace_bitmap, trace_gerber_bounds = self.render_gerber_to_bitmap(traces_file)
            self.trace_bounds = self.get_trace_bounds_from_bitmap(
                trace_bitmap, trace_gerber_bounds)
            print(f"Trace bounds: X={self.trace_bounds[0]:.2f}-{self.trace_bounds[2]:.2f} mm, "
                  f"Y={self.trace_bounds[1]:.2f}-{self.trace_bounds[3]:.2f} mm")

        # Phase 2: Determine board outline before generating isolation G-code so the
        # trace bitmap can be padded to board bounds (ensures edge paths are complete
        # and all layers share the same coordinate origin).
        if edge_cuts_file and os.path.exists(edge_cuts_file):
            print(f"\n=== Processing edge cuts: {edge_cuts_file} ===")
            self.edge_line_width_mm = self._read_edge_line_width(edge_cuts_file)
            ec_bitmap, ec_gerber_bounds = self.render_gerber_to_bitmap(edge_cuts_file)
            self.board_outline = self.parse_edge_cuts_to_outline(ec_bitmap, ec_gerber_bounds)
            print(f"Loaded board outline with {len(self.board_outline)} points")
        elif self.trace_bounds:
            print(f"\n=== Imputing edge cuts from trace bounds ===")
            self.board_outline = self.impute_edge_cuts(self.trace_bounds)
            width = self.trace_bounds[2] - self.trace_bounds[0] + 2 * self.edge_margin
            height = self.trace_bounds[3] - self.trace_bounds[1] + 2 * self.edge_margin
            print(f"Imputed board size: {width:.2f} x {height:.2f} mm")

        # Generate edge cuts Gerber if requested
        if generate_edge_cuts and self.board_outline:
            self.generate_edge_cuts_gerber(self.board_outline, generate_edge_cuts)

        # Compute board bounding box once; reused for isolation padding and laser passes.
        board_bounds = None
        if self.board_outline:
            xs = [p[0] for p in self.board_outline]
            ys = [p[1] for p in self.board_outline]
            board_bounds = (min(xs), min(ys), max(xs), max(ys))
            # Shift all G-code output so the board's lower-left corner (front alignment
            # mark) is at machine (0, 0), regardless of Gerber file coordinate origin.
            self._coord_offset = (-board_bounds[0], -board_bounds[1])
        else:
            self._coord_offset = (0.0, 0.0)

        # Phase 3: Generate isolation G-code now that board bounds are known.
        if trace_bitmap is not None:
            gcode_sections['isolation'] = self.process_traces(
                trace_bitmap, trace_gerber_bounds, board_bounds)
            operations.append('isolation')

        # Add edge cuts to operations if we have an outline
        if self.board_outline:
            gcode_sections['edge_cuts'] = self.process_edge_cuts(self.board_outline)
            operations.append('edge_cuts')

        # Process drill file (alignment holes are added even if no PCB holes)
        holes = []
        if drill_file and os.path.exists(drill_file):
            print(f"\n=== Processing drill file: {drill_file} ===")
            holes = self.parse_drill_file(drill_file)
        if holes or board_bounds is not None:
            drill_lines = self.process_drill_holes(holes, board_bounds)
            if drill_lines:
                gcode_sections['drill'] = drill_lines
                operations.append('drill')

        # Process laser etching layer (always written to separate files)
        if laser_layer and os.path.exists(laser_layer):
            print(f"\n=== Processing laser etching layer: {laser_layer} ===")
            bitmap, gerber_bounds = self.render_gerber_to_bitmap(laser_layer)

            mask_bitmap = None
            mask_gerber_bounds = None
            if mask_layer and os.path.exists(mask_layer):
                print(f"  Using solder mask layer: {mask_layer}")
                mask_bitmap, mask_gerber_bounds = self.render_gerber_to_bitmap(mask_layer)
            elif mask_layer:
                print(f"Warning: Solder mask file not found: {mask_layer}")
                print("  Falling back to copper layer heuristic pad detection")
            else:
                print("Warning: No solder mask file provided (-m / --mask-layer).")
                print("  Falling back to heuristic pad detection from the copper layer.")
                print("  For best results, supply the solder mask Gerber (.gts / .gbs).")

            trace_lines, pad_lines = self.process_laser(
                bitmap, gerber_bounds, mask_bitmap, mask_gerber_bounds, board_bounds)
            header = self.generate_laser_gcode_header(self.laser_tool)
            footer = self.generate_laser_gcode_footer()

            traces_filename = f"{self.file_prefix}_laser_traces.nc"
            with open(traces_filename, 'w') as f:
                f.write(header)
                f.write('\n'.join(trace_lines))
                f.write(footer)
            print(f"Laser trace G-code written to {traces_filename}")

            if pad_lines:
                pads_filename = f"{self.file_prefix}_laser_pads.nc"
                with open(pads_filename, 'w') as f:
                    f.write(header)
                    f.write('\n'.join(pad_lines))
                    f.write(footer)
                print(f"Laser pad G-code written to {pads_filename}")
            else:
                print("  No pads detected — skipping pad fill file")
        elif laser_layer:
            print(f"Warning: Laser layer file not found: {laser_layer}")

        # Generate soldermask overlay PNG for photoemulsion printing
        if soldermask_png:
            if mask_layer and os.path.exists(mask_layer):
                print(f"\n=== Generating soldermask overlay PNG ===")
                self.generate_soldermask_png(mask_layer, soldermask_png)
            elif mask_layer:
                print(f"Warning: Mask layer file not found: {mask_layer}")
                print("  Skipping soldermask PNG generation")
            else:
                print("Warning: --soldermask-png requires --mask-layer (-m) to be specified")

        # Process back copper layer (ground plane / double-sided PCB back traces)
        if back_traces_file and os.path.exists(back_traces_file):
            print(f"\n=== Processing back copper layer: {back_traces_file} ===")
            back_bitmap, back_gerber_bounds = self.render_gerber_to_bitmap(back_traces_file)
            back_lines = self.process_back_traces(back_bitmap, back_gerber_bounds, board_bounds)

            back_filename = f"{self.file_prefix}_back_isolation.nc"
            with open(back_filename, 'w') as f:
                f.write(self.generate_gcode_header('back_isolation', self.back_isolation_tool))
                f.write('\n'.join(back_lines))
                f.write(self.generate_gcode_footer())
            print(f"Back isolation G-code written to {back_filename}")
        elif back_traces_file:
            print(f"Warning: Back traces file not found: {back_traces_file}")

        # Generate output
        if self.separate_files:
            self._write_separate_files(gcode_sections)
        else:
            self._write_combined_file(operations, gcode_sections)

    def _write_separate_files(self, gcode_sections: Dict[str, List[str]]):
        """Write separate G-code files for each operation"""
        tools = {
            'isolation': self.isolation_tool,
            'edge_cuts': self.edge_cuts_tool,
            'drill': self.drill_tool
        }

        for operation, gcode_lines in gcode_sections.items():
            if not gcode_lines:
                continue

            filename = f"{self.file_prefix}_{operation}.nc"
            tool = tools[operation]

            with open(filename, 'w') as f:
                f.write(self.generate_gcode_header(operation, tool))
                f.write('\n'.join(gcode_lines))
                f.write(self.generate_gcode_footer())

            print(f"Written {filename}")

    def _write_combined_file(self, operations: List[str], gcode_sections: Dict[str, List[str]]):
        """Write combined G-code file with tool changes"""
        tools = {
            'isolation': self.isolation_tool,
            'edge_cuts': self.edge_cuts_tool,
            'drill': self.drill_tool
        }

        gcode = []

        # Header for first operation
        if operations:
            first_op = operations[0]
            gcode.append(self.generate_gcode_header(first_op, tools[first_op]))
            gcode.extend(gcode_sections[first_op])

            # Subsequent operations with tool changes
            for operation in operations[1:]:
                if operation in gcode_sections and gcode_sections[operation]:
                    gcode.extend(self.generate_tool_change(operation, tools[operation]))
                    gcode.extend(gcode_sections[operation])

        gcode.append(self.generate_gcode_footer())

        with open(self.output_file, 'w') as f:
            f.write('\n'.join(gcode))

        print(f"\nG-code written to {self.output_file}")
        print(f"Operations: {', '.join(operations)}")


def main():
    parser = argparse.ArgumentParser(
        description='Convert Gerber files to GRBL G-code for PCB milling',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic isolation routing
  %(prog)s -t traces.gbr -o output.nc

  # Full PCB with all files
  %(prog)s -t traces.gbr -e edges.gbr -d holes.drl -o output.nc

  # Use custom config file
  %(prog)s -t traces.gbr --config my_config.yaml

  # Generate separate files for each operation
  %(prog)s -t traces.gbr -e edges.gbr --separate

  # Double-sided PCB: top isolation + back copper ground plane
  %(prog)s -t front.gtl -b back.gbl -e edges.gbr -o front.nc

  # Soldermask overlay PNG for photoemulsion printing
  %(prog)s -m board.gts --soldermask-png soldermask_top.png
        """
    )

    # Input files
    parser.add_argument('-t', '--traces', type=str,
                        help='Copper traces Gerber file (.gbr, .gtl, .gbl)')
    parser.add_argument('-e', '--edge-cuts', type=str,
                        help='Edge cuts Gerber file for board outline')
    parser.add_argument('-d', '--drill', type=str,
                        help='Excellon drill file (.drl, .xln)')
    parser.add_argument('-l', '--laser-layer', type=str,
                        help='Copper layer Gerber for laser solder mask removal (.gtl, .gbr)')
    parser.add_argument('-m', '--mask-layer', type=str,
                        help='Solder mask Gerber for accurate pad fill (.gts top, .gbs bottom)')
    parser.add_argument('-b', '--back-traces', type=str,
                        help='Back copper Gerber for double-sided ground plane isolation (.gbl, .gbr)')

    # Legacy positional argument support
    parser.add_argument('input', nargs='?',
                        help='Input Gerber file (legacy, use -t instead)')

    # Output
    parser.add_argument('-o', '--output', type=str, default='output.nc',
                        help='Output G-code file (default: output.nc)')
    parser.add_argument('--separate', action='store_true',
                        help='Generate separate files for each operation')
    parser.add_argument('--generate-edge-cuts', type=str,
                        help='Output file for generated edge cuts Gerber')

    # Configuration
    parser.add_argument('--config', type=str, default='config.yaml',
                        help='YAML configuration file (default: config.yaml)')

    # Quick overrides (optional, config file takes precedence)
    parser.add_argument('--edge-margin', type=float,
                        help='Override edge margin for imputed board outline (mm)')
    parser.add_argument('--dpi', type=int,
                        help='Override DPI for Gerber rendering')
    parser.add_argument('--soldermask-png', type=str,
                        help='Output PNG file for soldermask overlay (requires -m; for photoemulsion printing)')
    parser.add_argument('--print-dpi', type=int,
                        help='Override print DPI for soldermask PNG (default from config, typically 600)')

    args = parser.parse_args()

    # Handle legacy positional argument
    traces_file = args.traces or args.input
    if not traces_file and not args.edge_cuts and not args.drill and not args.laser_layer and not args.mask_layer and not args.back_traces:
        parser.print_help()
        print("\nError: At least one input file is required (-t, -e, or -d)")
        sys.exit(1)

    # Create converter
    converter = GerberToGcode(args.config)

    # Apply command-line overrides
    if args.edge_margin is not None:
        converter.edge_margin = args.edge_margin
    if args.dpi is not None:
        converter.dpi = args.dpi
    if args.separate:
        converter.separate_files = True
    if args.print_dpi is not None:
        converter.soldermask_print_dpi = args.print_dpi

    # Run conversion
    converter.convert(
        traces_file=traces_file,
        edge_cuts_file=args.edge_cuts,
        drill_file=args.drill,
        output_file=args.output,
        generate_edge_cuts=args.generate_edge_cuts,
        laser_layer=args.laser_layer,
        mask_layer=args.mask_layer,
        back_traces_file=args.back_traces,
        soldermask_png=args.soldermask_png,
    )


if __name__ == '__main__':
    main()
