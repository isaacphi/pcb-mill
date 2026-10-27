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
                 gcode_pos=(0.0, 0.0, 5.0), mesh_min=(2.0, 2.0), mesh_max=(120.0, 118.0), deep=0, offz=None):
    return {
        "toolhead": {"homed_axes": homed, "position": list(pos)},
        "gcode_move": {"gcode_position": list(gcode_pos),
                       "homing_origin": [0.0, 0.0, deep if offz is None else offz, 0.0]},
        "gcode_macro PCB_DATUM": {"done": datum_done},
        "gcode_macro PCB_READY": {"no_mesh": 0},
        "bed_mesh": {"profile_name": profile, "mesh_min": list(mesh_min), "mesh_max": list(mesh_max),
                     "probed_matrix": [[0.0, 0.1], [0.05, 0.2]]},
        "probe": {"last_probe_position": [26.0, 30.0, 0.44]},
        "gcode_macro PCB_DEEP": {"shift": deep},
    }


# ---------------------------------------------------------------- config sanity
def test_z_floor_is_half_a_mm():
    assert re.search(r"\[stepper_z\][^\[]*position_min:\s*-0\.5\b", CFG)


def test_no_negative_number_right_after_open_brace():
    """Klipper's Jinja uses '{' as the variable delimiter, so '{-2.75 + x}' parses
    '-' as whitespace control and silently evaluates '2.75 + x'. This sign flip
    relabelled Z wrongly and drove the bit into the board. Use {% set %} instead."""
    assert not re.search(r"\{-", CFG)
    assert not re.search(r"\{\+", CFG)


def test_braces_balance():
    assert CFG.count("{") == CFG.count("}")


# ---------------------------------------------------------------------- datum
def test_datum_search_window_never_exceeds_depth():
    lines, infos = render("PCB_DATUM", {"DEPTH": "3"})
    # relabel so floor (-0.5) is exactly DEPTH below the start, then PROBE
    assert "SET_KINEMATIC_POSITION Z=2.5" in lines
    assert lines.index("SET_KINEMATIC_POSITION Z=2.5") < lines.index("PROBE SAMPLES=1")
    assert "G28" not in " ".join(lines)


@pytest.mark.parametrize("depth", ["0", "-1", "30.5", "40"])
def test_datum_rejects_unbounded_depth(depth):
    with pytest.raises(MacroError):
        render("PCB_DATUM", {"DEPTH": depth})


def test_datum_accepts_large_search_window():
    lines, _ = render("PCB_DATUM", {"DEPTH": "25"})
    assert "SET_KINEMATIC_POSITION Z=24.5" in lines
    assert lines.index("SET_KINEMATIC_POSITION Z=24.5") < lines.index("PROBE SAMPLES=1")


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
    assert "PROBE_COUNT=10,11" in cal and "SAMPLES=2 " in cal and "SAMPLES_TOLERANCE_RETRIES=3" in cal
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


def test_ready_allows_skipping_mesh():
    lines, infos = render("PCB_READY", {**READY, "MESH": "0"}, fake_printer(profile=""))
    assert "SET_GCODE_VARIABLE MACRO=PCB_READY VARIABLE=no_mesh VALUE=1" in lines
    assert any("MESH=0" in i for i in infos)


def test_deep_allows_no_mesh_when_skipped():
    printer = fake_printer(profile="", datum_done=1)
    printer["gcode_macro PCB_READY"]["no_mesh"] = 1
    lines, _ = render("PCB_DEEP", printer=printer)
    assert "SET_GCODE_OFFSET Z=3.0" in lines


def test_homing_invalidates_datum_and_mesh_before_moving():
    sec = CFG[CFG.index("[homing_override]"):]
    sec = sec[: sec.index("\n[", 5)]
    i_var = sec.index("SET_GCODE_VARIABLE MACRO=PCB_DATUM VARIABLE=done VALUE=0")
    i_clr = sec.index("BED_MESH_CLEAR")
    assert i_var < sec.index("G28 X") and i_clr < sec.index("G28 X")
    assert i_clr < sec.index("G1 Z")        # no mesh-adjusted lift
    assert "G28 Z" not in sec.replace("NEVER G28 Z", "")


# ------------------------------------------------------------------- PCB_DEEP
def test_deep_shifts_frame_without_moving():
    lines, _ = render("PCB_DEEP", printer=fake_printer(pos=(30.0, 30.0, 1.5)))
    assert "SET_KINEMATIC_POSITION Z=4.5" in lines          # label +3, no motion
    assert "SET_GCODE_OFFSET Z=3.0" in lines                # gcode Z0 stays on the copper
    assert not any(l.startswith(("G0", "G1", "PROBE")) for l in lines)
    assert any("VARIABLE=shift VALUE=3.0" in l for l in lines)


def test_deep_is_idempotent():
    lines, _ = render("PCB_DEEP", printer=fake_printer(deep=3.0))
    assert lines == []


@pytest.mark.parametrize("kw", [{"datum_done": 0}, {"profile": ""}])
def test_deep_needs_datum_and_mesh(kw):
    with pytest.raises(MacroError):
        render("PCB_DEEP", printer=fake_printer(**kw))


def test_mesh_refuses_in_deep_frame():
    with pytest.raises(MacroError, match="deep frame"):
        render("PCB_MESH", mesh_params(), fake_printer(deep=3.0))


def test_ready_accepts_deep_frame_and_refuses_stray_z_offset():
    render("PCB_READY", READY, fake_printer(deep=3.0))             # offset 3 == shift 3
    with pytest.raises(MacroError, match="Z offset"):
        render("PCB_READY", READY, fake_printer(deep=0, offz=0.5))  # someone nudged the offset
    with pytest.raises(MacroError, match="Z offset"):
        render("PCB_READY", READY, fake_printer(deep=3.0, offz=0.0))


def test_datum_and_homing_reset_deep_frame():
    lines, _ = render("PCB_DATUM", {"DEPTH": "3"})
    assert any("MACRO=PCB_DEEP VARIABLE=shift VALUE=0" in l for l in lines)
    sec = CFG[CFG.index("[homing_override]"):]
    assert "MACRO=PCB_DEEP VARIABLE=shift VALUE=0" in sec[: sec.index("\n[", 5)]


# --------------------------------------------------------------------- JOG_Z
def test_jog_up_is_a_relative_move():
    lines, _ = render("JOG_Z", {"DZ": "1"})
    assert lines == ["G91", "G1 Z1.0 F600", "G90"]


def test_jog_down_is_a_bounded_probe_descent():
    lines, _ = render("JOG_Z", {"DZ": "-2"}, fake_printer(datum_done=0))
    i = lines.index("SET_KINEMATIC_POSITION Z=1.5")          # floor (-0.5) is exactly 2 mm below
    assert lines[i + 1] == "PROBE SAMPLES=1"
    assert "G91" not in lines[:i]


@pytest.mark.parametrize("dz", ["0", "11", "-11"])
def test_jog_limits(dz):
    with pytest.raises(MacroError):
        render("JOG_Z", {"DZ": dz}, fake_printer(datum_done=0))


def test_jog_down_refused_after_datum():
    with pytest.raises(MacroError, match="datum is set"):
        render("JOG_Z", {"DZ": "-1"}, fake_printer(datum_done=1))
    render("JOG_Z", {"DZ": "1"}, fake_printer(datum_done=1))     # up is always fine


def test_origin_needs_xy_homed():
    with pytest.raises(MacroError, match="not homed"):
        render("SET_BOARD_ORIGIN", printer={**fake_printer(homed="")})


# ------------------------------------------------------- go to the probe start
def test_datum_moves_xy_before_opening_the_search_window():
    lines, _ = render("PCB_DATUM", {"DEPTH": "3", "X": "5", "Y": "5"})
    i_move, i_reloc, i_probe = (lines.index("G0 X5.0 Y5.0 F3000"),
                                lines.index("SET_KINEMATIC_POSITION Z=2.5"),
                                lines.index("PROBE SAMPLES=1"))
    assert i_move < i_reloc < i_probe
    assert not any(re.match(r"G0 .*Z", l) for l in lines)       # XY only: Z untouched


def test_datum_without_xy_stays_put():
    lines, _ = render("PCB_DATUM", {"DEPTH": "3"})
    # no X/Y given: "moves" to where it already is (gcode position)
    assert any(l.startswith("G0 X0.0 Y0.0") for l in lines)


@pytest.mark.parametrize("xy", [("-1", "5"), ("5", "101"), ("150", "5")])
def test_datum_and_start_refuse_outside_pocket(xy):
    with pytest.raises(MacroError, match="pocket"):
        render("PCB_DATUM", {"X": xy[0], "Y": xy[1]})
    with pytest.raises(MacroError, match="pocket"):
        render("PCB_TO_START", {"X": xy[0], "Y": xy[1]})


def test_to_start_is_xy_only_and_needs_homing():
    lines, _ = render("PCB_TO_START", {"X": "5", "Y": "5"})
    assert lines == ["G90", "G0 X5.0 Y5.0 F3000"]
    with pytest.raises(MacroError, match="not homed"):
        render("PCB_TO_START", {"X": "5", "Y": "5"}, fake_printer(homed=""))
