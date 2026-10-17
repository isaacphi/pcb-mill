"""Render a job-specific mill_config.yaml from the tool inventory + form overrides.

Strategy: start from the repo's mill_config.yaml (the known-good schema for
gerber2gcode), then deep-merge inventory tool selection (tool_diameter,
spindle_speed per operation) and any user overrides on top. Output is always a
complete config that `mill.py --config` accepts unchanged.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "mill_config.yaml"
TOOLS_YAML = Path(__file__).parent / "tools.yaml"

# form field prefix -> (config section, key). Operation names in tools.yaml map
# onto mill_config.yaml tool sections.
OP_SECTION = {"isolation": "isolation", "drill": "drill", "edge": "edge_cuts"}
# isolation config is also mirrored to back_isolation (same physical pass, mirrored)
MIRROR_OPS = {"isolation": "back_isolation"}

SCALAR_KEYS = {  # keys we accept from the form, per section
    "isolation": ["feed_rate", "plunge_rate", "cut_depth", "passes", "step_over", "isolation_border"],
    "drill": ["feed_rate", "plunge_rate", "cut_depth", "total_depth", "retract_height",
              "bore_tolerance", "bore_step_down"],
    "edge": ["feed_rate", "plunge_rate", "cut_depth", "total_depth"],
}


def load_inventory(path: Path = TOOLS_YAML) -> dict:
    return yaml.safe_load(path.read_text())


def _coerce(v):
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


def render_config(overrides: dict | None = None,
                  tool_by_op: dict[str, str] | None = None,
                  inventory_path: Path = TOOLS_YAML) -> dict:
    """-> full mill_config dict.

    overrides: {"isolation": {"feed_rate": 200, ...}, "drill": {...}, "edge": {...},
                "general": {...}} — flat per-op dicts of scalars.
    tool_by_op: {"isolation": "em08", ...} — inventory tool id per operation.
    """
    cfg = yaml.safe_load(DEFAULT_CONFIG.read_text())
    inv = load_inventory(inventory_path)
    tools = {t["id"]: t for t in inv.get("tools", [])}

    cfg["general"]["safe_height"] = inv["machine"]["safe_height"]
    cfg["general"]["dpi"] = inv["machine"]["dpi"]

    for op, section in OP_SECTION.items():
        sections = [section] + ([MIRROR_OPS[op]] if op in MIRROR_OPS else [])
        tid = (tool_by_op or {}).get(op) or inv["operations"][op]["tool"]
        tool = tools.get(tid)
        for s in sections:
            if tool:
                cfg["tools"][s]["tool_diameter"] = tool["diameter"]
                cfg["tools"][s]["spindle_speed"] = tool["spindle_speed"]
            for key in SCALAR_KEYS[op]:
                val = (overrides or {}).get(op, {}).get(key)
                if val is None:
                    val = inv["operations"][op].get(key)
                if val is not None:
                    cfg["tools"][s][key] = _coerce(val)
    return cfg
