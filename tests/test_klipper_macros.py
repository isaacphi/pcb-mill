"""Render the safety-critical Klipper macros offline (no machine contact).

Klipper renders a macro body with Jinja once, at call time, against a
`printer` status object. We do the same with a fake status and check the
G-code that would be emitted, and that the guards raise.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest

CFG = (Path(__file__).resolve().parent.parent / "klipper" / "printer.cfg").read_text()


class MacroError(Exception):
    pass


def body(name: str) -> str:
    m = re.search(r"\[gcode_macro %s\]\n(?:[^\n]*\n)*?gcode:\n((?:[ \t]+[^\n]*\n|[ \t]*\n)+)" % re.escape(name), CFG)
    assert m, name
    return m.group(1)


def render(name: str, params=None, printer=None, variables=None):
    """-> (emitted gcode lines, info messages)."""
    infos: list[str] = []

    def raise_error(msg):
        raise MacroError(msg)

    ctx = dict(
        params=params or {},
        printer=printer or fake_printer(),
        action_raise_error=raise_error,
        action_respond_info=lambda s: infos.append(s) or "",
    )
    text = jinja2.Environment("{%", "%}", "{", "}").from_string(body(name)).render(**ctx)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(";")]
    return [re.sub(r"\s*;.*$", "", ln) for ln in lines], infos


def fake_printer(homed="xyz", datum_done=1, profile="default", pos=(26.0, 30.0, 5.0),
                 gcode_pos=(0.0, 0.0, 5.0), mesh_min=(2.0, 2.0), mesh_max=(120.0, 118.0)):
    return {
        "toolhead": {"homed_axes": homed, "position": list(pos)},
        "gcode_move": {"gcode_position": list(gcode_pos)},
        "gcode_macro PCB_DATUM": {"done": datum_done},
        "bed_mesh": {"profile_name": profile, "mesh_min": list(mesh_min), "mesh_max": list(mesh_max),
                     "probed_matrix": [[0.0, 0.1], [0.05, 0.2]]},
        "probe": {"last_probe_position": [26.0, 30.0, 0.44]},
    }


# ---------------------------------------------------------------- config sanity
def test_z_floor_is_half_a_mm():
    assert re.search(r"\[stepper_z\][^\[]*position_min:\s*-0\.5\b", CFG)


def test_braces_balance():
    assert CFG.count("{") == CFG.count("}")


# ---------------------------------------------------------------------- datum
def test_datum_search_window_never_exceeds_depth():
    lines, infos = render("PCB_DATUM", {"DEPTH": "3"})
    # relabel so floor (-0.5) is exactly DEPTH below the start, then PROBE
    assert "SET_KINEMATIC_POSITION Z=2.5" in lines
    assert lines.index("SET_KINEMATIC_POSITION Z=2.5") < lines.index("PROBE SAMPLES=1")
    assert "G28" not in " ".join(lines)


@pytest.mark.parametrize("depth", ["0", "-1", "4.5", "10"])
def test_datum_rejects_unbounded_depth(depth):
    with pytest.raises(MacroError):
        render("PCB_DATUM", {"DEPTH": depth})


def test_datum_requires_homing():
    with pytest.raises(MacroError):
        render("PCB_DATUM", printer=fake_printer(homed="xy"))


def test_datum_dry_run_emits_no_motion():
    lines, infos = render("PCB_DATUM", {"DRY": "1"})
    assert not any(l.startswith(("G0", "G1", "PROBE", "SET_KINEMATIC")) for l in lines)
    assert infos and "at most 3.00 mm" in infos[0]


def test_datum_done_relabels_to_zero_and_lifts_relatively():
    lines, _ = render("_PCB_DATUM_DONE")
    assert lines[0] == "SET_KINEMATIC_POSITION Z=0"
    i = lines.index("G91")
    assert lines[i + 1].startswith("G1 Z1.5") and lines[i + 2] == "G90"
    # no absolute Z move straight after a relabel (the earlier plunge)
    assert not any(re.match(r"G0 .*Z", l) for l in lines)


# ----------------------------------------------------------------------- mesh
def mesh_params(**kw):
    p = {"X0": "28", "Y0": "32", "X1": "46", "Y1": "50", "PTSX": "4", "PTSY": "4"}
    p.update(kw)
    return p


def test_mesh_needs_a_datum():
    with pytest.raises(MacroError, match="PCB_DATUM"):
        render("PCB_MESH", mesh_params(), fake_printer(datum_done=0))


@pytest.mark.parametrize("missing", ["X0", "Y0", "X1", "Y1"])
def test_mesh_requires_explicit_region(missing):
    p = mesh_params()
    del p[missing]
    with pytest.raises(MacroError):
        render("PCB_MESH", p)


def test_mesh_travels_at_fixed_clearance_and_picks_bicubic():
    lines, _ = render("PCB_MESH", mesh_params(PTSX="10", PTSY="11"))
    cal = next(l for l in lines if l.startswith("BED_MESH_CALIBRATE"))
    assert "HORIZONTAL_MOVE_Z=2.0" in cal and "ALGORITHM=bicubic" in cal
    assert "PROBE_COUNT=10,11" in cal and "SAMPLES=1" in cal
    assert "MESH_MIN=28.0,32.0" in cal


def test_mesh_small_grid_uses_lagrange():
    lines, _ = render("PCB_MESH", mesh_params(PTSX="3", PTSY="3"))
    assert "ALGORITHM=lagrange" in next(l for l in lines if l.startswith("BED_MESH_CALIBRATE"))


def test_finish_rejects_excess_spread_and_clears_mesh():
    pr = fake_printer()
    pr["bed_mesh"]["probed_matrix"] = [[0.0, 2.0], [0.0, 0.1]]
    lines, infos = render("_PCB_FINISH", {"MAX_SPREAD": "1.5"}, pr)
    assert "BED_MESH_CLEAR" in lines and "REJECTED" in infos[0]


def test_finish_accepts_flat_mesh():
    lines, infos = render("_PCB_FINISH", {"MAX_SPREAD": "1.5", "PX": "4", "PY": "4"})
    assert "BED_MESH_CLEAR" not in lines and "Mesh OK" in infos[0]


# ---------------------------------------------------------------------- ready
READY = {"X0": "0.77", "Y0": "0.77", "X1": "90.0", "Y1": "88.0"}


def test_ready_ok_when_mesh_covers_job():
    # origin = toolhead - gcode pos = (26, 30): job spans machine 26.8..116, 30.8..118
    _, infos = render("PCB_READY", READY)
    assert "PCB_READY ok" in infos[0]


@pytest.mark.parametrize("kw,match", [
    ({"homed": "xy"}, "not homed"),
    ({"datum_done": 0}, "datum"),
    ({"profile": ""}, "no mesh"),
    ({"mesh_max": (100.0, 118.0)}, "outside the mesh"),     # mesh doesn't reach the job
    ({"mesh_min": (60.0, 2.0)}, "outside the mesh"),
    ({"pos": (40.0, 30.0, 5.0)}, "outside the mesh"),       # board origin moved since meshing
])
def test_ready_refuses(kw, match):
    with pytest.raises(MacroError, match=match):
        render("PCB_READY", READY, fake_printer(**kw))


def test_ready_needs_extents():
    with pytest.raises(MacroError, match="extents"):
        render("PCB_READY", {})


def test_homing_invalidates_datum_and_mesh_before_moving():
    sec = CFG[CFG.index("[homing_override]"):]
    sec = sec[: sec.index("\n[", 5)]
    i_var = sec.index("SET_GCODE_VARIABLE MACRO=PCB_DATUM VARIABLE=done VALUE=0")
    i_clr = sec.index("BED_MESH_CLEAR")
    assert i_var < sec.index("G28 X") and i_clr < sec.index("G28 X")
    assert i_clr < sec.index("G1 Z")        # no mesh-adjusted lift
    assert "G28 Z" not in sec.replace("NEVER G28 Z", "")
