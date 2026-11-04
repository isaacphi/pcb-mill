"""Tests for the pcb-mill web app: excellon parsing/merging, config rendering,
and an end-to-end conversion of the sample board through the real mill.py."""

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path

import pytest

os.environ["PCB_MILL_JOBS"] = tempfile.mkdtemp(prefix="pcbmill-test-jobs-")

from fastapi.testclient import TestClient  # noqa: E402

from web import app as webapp  # noqa: E402
from web import config_render, excellon, moonraker  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "board"
PTH = BOARD / "AQI esp32 pcb-PTH.drl"
NPTH = BOARD / "AQI esp32 pcb-NPTH.drl"


# ---------- moonraker handoff ----------


def test_upload_direct_write(tmp_path, monkeypatch):
    """On the Pi the gcodes dir exists locally: files land there directly."""
    monkeypatch.setenv("GCODE_DIR", str(tmp_path))
    monkeypatch.delenv("MOONRAKER_URL", raising=False)
    src = tmp_path / "onesided.nc"
    src.write_text("G1 X1\n")
    out = moonraker.upload(src, subdir="pcb-mill/job42")
    assert (tmp_path / "pcb-mill" / "job42" / "onesided.nc").read_text() == "G1 X1\n"
    assert out["via"] == "direct-write"
    assert moonraker.available()


def test_upload_nothing_configured(monkeypatch):
    monkeypatch.setenv("GCODE_DIR", "/nonexistent")
    monkeypatch.delenv("MOONRAKER_URL", raising=False)
    assert not moonraker.available()
    with pytest.raises(RuntimeError):
        moonraker.upload(Path("whatever.nc"))


# ---------- excellon ----------

def test_parse_sample_pth():
    d = excellon.parse(PTH.read_text())
    assert d.metric
    assert d.tools == {1: 0.4, 2: 0.8, 3: 1.0}
    assert all(len(pts) > 0 for pts in d.holes.values())
    assert sum(len(p) for p in d.holes.values()) > 10


def test_hole_report_classification_vs_08_bit():
    rows = excellon.hole_report(excellon.parse(PTH.read_text()), 0.8, 0.05)
    by_dia = {r["diameter"]: r["method"] for r in rows}
    assert "undersized" in by_dia[0.4]          # vias < bit → plunged oversized
    assert by_dia[0.8] == "plunged"
    assert by_dia[1.0] == "helically bored"


def test_merge_roundtrip_with_empty_npth():
    merged = excellon.merge(PTH.read_text(), NPTH.read_text())
    d = excellon.parse(merged)
    assert d.tools == {1: 0.4, 2: 0.8, 3: 1.0}
    assert sum(len(p) for p in d.holes.values()) == \
        sum(len(p) for p in excellon.parse(PTH.read_text()).holes.values())


def test_merge_two_files_renumbers_tools():
    a = "M48\nMETRIC\nT1C0.800\n%\nG90\nT1\nX1.0Y2.0\nM30\n"
    b = "M48\nMETRIC\nT1C1.000\nT2C3.200\n%\nG90\nT1\nX5.0Y6.0\nT2\nX7.0Y8.0\nM30\n"
    d = excellon.parse(excellon.merge(a, b))
    assert sorted(d.tools.values()) == [0.8, 1.0, 3.2]
    assert sum(len(p) for p in d.holes.values()) == 3


# ---------- config rendering ----------

def test_render_defaults_from_inventory():
    cfg = config_render.render_config()
    assert cfg["tools"]["isolation"]["tool_diameter"] == 0.8
    assert cfg["tools"]["drill"]["feed_rate"] == 100
    assert cfg["tools"]["edge_cuts"]["total_depth"] == 1.6


def test_render_overrides_mirror_to_back_isolation():
    cfg = config_render.render_config(overrides={"isolation": {"feed_rate": "123"}})
    assert cfg["tools"]["isolation"]["feed_rate"] == 123
    assert cfg["tools"]["back_isolation"]["feed_rate"] == 123
    assert isinstance(cfg["tools"]["isolation"]["feed_rate"], int)


def test_dpi_env_override(monkeypatch):
    monkeypatch.delenv("PCB_MILL_DPI", raising=False)
    cfg = config_render.render_config()
    assert cfg["general"]["dpi"] == 1500  # tools.yaml default
    monkeypatch.setenv("PCB_MILL_DPI", "1000")
    cfg = config_render.render_config()
    assert cfg["general"]["dpi"] == 1000  # Pi deployment override


# ---------- end-to-end conversion ----------

SAMPLE = dict(
    copper=BOARD / "AQI esp32 pcb-F_Cu.gbr",
    edge=BOARD / "AQI esp32 pcb-Edge_Cuts.gbr",
    drill=PTH,
)


def _wait_done(job: Path, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = json.loads((job / "status.json").read_text())
        if st["state"] in ("done", "error", "rejected"):
            return st
        time.sleep(1)
    pytest.fail("conversion did not finish in time")


def test_rename_and_stop_routes(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path)
    job = _job("rename-me", title="Old name", sent=["1_isolation.gcode"], sent_folder="old")
    calls = []
    monkeypatch.setattr(moonraker, "cancel", lambda: calls.append(True))
    c = TestClient(webapp.app)
    r = c.post(f"/jobs/{job.name}/rename", data={"title": "New name"}, follow_redirects=False)
    assert r.status_code == 303
    assert json.loads((job / "status.json").read_text())["title"] == "New name"
    ms["print"] = {"state": "printing"}
    r = c.get(f"/jobs/{job.name}")
    assert ">Stop</button>" in r.text and "EMERGENCY STOP" not in r.text
    r = c.post(f"/jobs/{job.name}/stop", headers={"HX-Request": "true"})
    assert r.status_code == 200 and calls == [True]


def test_convert_sample_board_direct(tmp_path):
    """Run the _convert pipeline (mill.py + plot) against the sample board."""
    import yaml
    cfg = config_render.render_config()
    # Add one wide (3.2 mm) hole so the multi-pass bore path is exercised.
    wide = tmp_path / "wide.drl"
    wide.write_text("M48\nMETRIC\nT1C3.200\n%\nG90\nT1\nX50.0Y-65.0\nM30\n")
    drill = tmp_path / "drill.drl"
    drill.write_text(excellon.merge(PTH.read_text(), wide.read_text()))
    cfg_path = tmp_path / "mill_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    (tmp_path / "status.json").write_text(json.dumps({"state": "queued"}))
    asyncio.run(webapp._convert(tmp_path, str(SAMPLE["copper"]), "top",
                                str(SAMPLE["edge"]), str(drill), cfg_path))
    st = json.loads((tmp_path / "status.json").read_text())
    # the sample ESP32 board is ~70 x 100 mm: just fits the 100 mm pocket
    assert st["state"] == "done", (st.get("error"), (tmp_path / "log.txt").read_text()[-2000:])
    assert st["extents"] and 90 < st["extents"][3] <= 101
    assert st["edge_extents"]
    assert "onesided.nc" in st["files"]
    # Regression: pygerber renders int(dpi/25.4) px/mm, so using 25.4/dpi
    # shifted the copper/edge frame by ~0.4 mm relative to the drill file.
    assert 65.5 < st["mirror_x"] < 66.5, st["mirror_x"]
    assert (tmp_path / "out" / "toolpaths.png").exists()
    moves, longest = _check_drill_bores(tmp_path / "out" / "drill.nc")
    assert moves > 100                      # the sample board has bored holes
    assert longest <= 0.12, longest
    _check_wide_bore_clears_centre(tmp_path / "out" / "drill.nc")


def _check_drill_bores(path):
    """Helical bores must be real: G1 chords <= ~0.12 mm (Klipper's G2 needs
    resolution < the helix radius or it degenerates to a plunge), Z never
    rising mid-hole, and no G2/G3 left for Klipper to segment."""
    import math
    import re
    lines = path.read_text().splitlines()
    assert not any(re.match(r"\s*G0?[23]\b", l) for l in lines), "arcs in drill file"
    pos = None
    z = None
    moves = 0
    longest = 0.0
    for l in lines:
        m = re.match(r"G1 X(-?[\d.]+) Y(-?[\d.]+)(?: Z(-?[\d.]+))? ", l)
        if not m:
            pos = None if l.startswith("G0") else pos
            if l.startswith("G0") or l.startswith("G1 Z"):
                z = None if l.startswith("G0") else z
            continue
        x, y = float(m.group(1)), float(m.group(2))
        nz = float(m.group(3)) if m.group(3) else z
        if pos is not None:
            longest = max(longest, math.hypot(x - pos[0], y - pos[1]))
            if z is not None and nz is not None:
                assert nz <= z + 1e-6, f"Z rises inside a bore: {z} -> {nz}"
            moves += 1
        pos, z = (x, y), nz
    return moves, longest


def _cut_blocks(path):
    """Contiguous Z<0 blocks (one per helix pass / plunge)."""
    import re
    blocks, block, cur = [], [], (0.0, 0.0)
    for line in path.read_text().splitlines():
        m = dict(re.findall(r"([XYZ])(-?\d+\.?\d*)", line.split(";")[0]))
        if "X" in m:
            cur = (float(m["X"]), cur[1])
        if "Y" in m:
            cur = (cur[0], float(m["Y"]))
        if "Z" in m:
            if float(m["Z"]) < 0:
                block.append(cur)
            elif block:
                blocks.append(block)
                block = []
    if block:
        blocks.append(block)
    return blocks


def _check_wide_bore_clears_centre(path, bit_diameter=0.8):
    import math
    info = []
    for b in _cut_blocks(path):
        cx = sum(x for x, y in b) / len(b)
        cy = sum(y for x, y in b) / len(b)
        maxr = max(math.hypot(x - cx, y - cy) for x, y in b)
        info.append((cx, cy, maxr, b))
    wide = [i for i in info if i[2] > bit_diameter]
    assert wide, "no wide bore found in drill.nc"
    cx, cy, _, _ = wide[0]
    near = [i for i in info if math.hypot(i[0] - cx, i[1] - cy) < 0.05]
    minr = min(math.hypot(x - cx, y - cy) for i in near for x, y in i[3])
    assert minr <= bit_diameter / 2 + 1e-3, (
        f"wide bore leaves a central pillar: min radius {minr:.3f} mm "
        f"(bit radius {bit_diameter / 2:.3f} mm)"
    )


def test_http_job_flow():
    """POST the sample board through the form; the job runs to done."""
    client = TestClient(webapp.app)
    client.__enter__()  # keep the portal loop alive for the background task
    files = {
        "copper": ("F_Cu.gbr", SAMPLE["copper"].read_bytes()),
        "edge": ("Edge_Cuts.gbr", SAMPLE["edge"].read_bytes()),
        "drill": ("PTH.drl", SAMPLE["drill"].read_bytes()),
        "drill2": ("NPTH.drl", NPTH.read_bytes()),
    }
    data = {"side": "top", "isolation.feed_rate": "240"}
    r = client.post("/jobs", files=files, data=data, follow_redirects=False)
    assert r.status_code == 303
    name = r.headers["location"].rsplit("/", 1)[1]
    st = _wait_done(Path(os.environ["PCB_MILL_JOBS"]) / name)
    assert st["state"] == "done"
    assert st["report"]            # hole report was computed at upload
    # override made it into the job config
    import yaml
    cfg = yaml.safe_load((Path(os.environ["PCB_MILL_JOBS"]) / name / "mill_config.yaml").read_text())
    assert cfg["tools"]["isolation"]["feed_rate"] == 240
    r = client.get(f"/jobs/{name}")
    assert r.status_code == 200 and "toolpaths" in r.text


def test_combined_export_joins_the_three_run_files(tmp_path):
    job = _job("combined-1")
    for n in ("1_isolation.gcode", "2_drill.gcode", "3_edge.gcode"):
        (job / "out" / n).write_text(
            "PCB_READY X0=1 Y0=1 X1=2 Y1=2\nM3\nG1 X1 Y1\nM5\nG0 X0 Y0\nM2\n")
    r = TestClient(webapp.app).get(f"/jobs/{job.name}/combined.gcode")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    text = r.text
    assert text.count("M2") == 1 and text.count("PCB_READY") == 3
    assert "G1 X1 Y1" in text


def test_combined_export_builds_an_unsent_job(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("combined-unsent")
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0]], lo=0.0, hi=0.0)
    r = TestClient(webapp.app).get(f"/jobs/{job.name}/combined.gcode")
    assert r.status_code == 200
    assert r.text.count("PCB_READY") == 3 and r.text.count("M2") == 1
    assert r.text.rstrip().endswith("G0 X0 Y100 F3000")


def test_processing_page():
    """A converting job renders the processing screen with elapsed time."""
    job = Path(os.environ["PCB_MILL_JOBS"]) / "fakejob-processing"
    job.mkdir()
    (job / "status.json").write_text(json.dumps({
        "state": "converting", "created": time.strftime("%F %T"), "files": []}))
    client = TestClient(webapp.app)
    r = client.get(f"/jobs/{job.name}")
    assert r.status_code == 200
    assert "spinner" in r.text and "Converting" in r.text


# ---------- probe geometry ----------

from web import probe  # noqa: E402


def test_extents_from_gcode(tmp_path):
    nc = tmp_path / "edge.nc"
    nc.write_text(
        "G21\nG90\nM3 S20000\n"
        "G0 X0.4148 Y0.0000 Z1.0\n"
        "G1 Z-0.4 F45\n"
        "G1 X70.3664 Y0.0000 F220\n"
        "G1 X70.7813 Y100.3385 F220\n"
        "G1 X0.0000 Y100.7533 F220\n"
        "M5\n")
    ext = probe.extents_from_gcode([nc])
    assert ext == (0.0, 0.0, 70.7813, 100.7533)


def test_extents_ignores_non_moves(tmp_path):
    nc = tmp_path / "x.nc"
    nc.write_text("G21\n; X999 Y999 comment\nM3 S20000\nG4 P2\nG1 X5 Y5 F100\n")
    assert probe.extents_from_gcode([nc]) == (5.0, 5.0, 5.0, 5.0)


def test_validate_job_limits():
    ok = (0.8, 0.8, 90.0, 95.0)
    assert probe.validate_job(ok, (0.0, 0.0, 100.5, 100.8)) is None   # 100 mm board + edge tool
    assert "100" in probe.validate_job((0.0, 0.0, 101.5, 50.0))
    assert probe.validate_job((0.0, 0.0, 100.5, 50.0)) is None   # isolation overshoots the edge a little
    assert "100" in probe.validate_job((0.0, 0.0, 50.0, 101.2))
    assert "outline" in probe.validate_job(ok, (0.0, 0.0, 102.0, 90.0))
    assert probe.validate_job(None)


def test_mesh_plan_is_clipped_to_5_95_window():
    # full 100x100 board: cut 0.8..99.2 -> mesh only inside 5..95, origin (25.4, 27.5)
    p = probe.mesh_plan((0.8, 0.8, 99.2, 99.2), (25.4, 27.5))
    assert p.job == (5.0, 5.0, 95.0, 95.0)
    assert (p.x0, p.y0, p.x1, p.y1) == (30.4, 32.5, 120.4, 122.5)
    assert p.nx == 10 and p.ny == 10 and p.fits
    assert (p.x1 - p.x0) / (p.nx - 1) <= 10
    assert p.gcode().startswith("PCB_MESH X0=30.400 Y0=32.500 X1=120.400 Y1=122.500 PTSX=10 PTSY=10")


def test_mesh_plan_small_board_keeps_margin_inside_window():
    p = probe.mesh_plan((20.0, 30.0, 60.0, 70.0), (25.4, 27.5))
    assert p.job == (21.5, 31.5, 58.5, 68.5)


def test_mesh_plan_flags_travel_overrun():
    p = probe.mesh_plan((0.8, 0.8, 99.2, 99.2), (40.0, 27.5))        # mesh x1 = 135 > 126
    assert not p.fits and "X travel" in p.problem
    # mesh fits but the edge path doesn't
    p = probe.mesh_plan((0.8, 0.8, 99.2, 99.2), (25.4, 27.5), toolpaths=(0.0, 0.0, 101.0, 100.4))
    assert not p.fits and "X travel" in p.problem


def test_mesh_plan_min_points_and_tiny_board():
    p = probe.mesh_plan((0.0, 0.0, 12.0, 8.0), (10.0, 10.0))
    assert p.nx >= 4 and p.ny >= 4          # bicubic needs >= 4 per axis
    with pytest.raises(ValueError):
        probe.mesh_plan((0.0, 0.0, 2.0, 100.0), (0.0, 0.0))
    with pytest.raises(ValueError):
        probe.mesh_plan((96.0, 96.0, 99.0, 99.0), (0.0, 0.0))     # nothing left inside 5..95


def test_with_header_puts_ready_check_before_spindle():
    out = probe.with_header("G21\nM3 S20000\n", (5.0, 5.0, 95.0, 88.0))
    lines = out.splitlines()
    assert lines[0] == "PCB_READY X0=5.000 Y0=5.000 X1=95.000 Y1=88.000"
    assert lines[1] == "G21" and lines.index("M3 S20000") > 0
    deep = probe.with_header("G21\n", (5.0, 5.0, 95.0, 88.0), deep=True).splitlines()
    assert deep[0].startswith("PCB_READY") and deep[1] == "PCB_DEEP"
    skipped = probe.with_header("G21\n", (5.0, 5.0, 95.0, 88.0), mesh=False).splitlines()
    assert skipped[0].endswith("MESH=0")


# ------------------------------------------------ workflow routes (Klipper faked)

def _fake_machine(monkeypatch, tmp_path, **over):
    ms = {"klipper": "ready", "homed": "xyz", "position": [26.0, 30.0, 25.0],
          "origin": (25.4, 27.5), "datum_done": False,
          "print": {"state": "standby"},
          "mesh": {"profile": "", "min": None, "max": None, "matrix": [], "lo": None, "hi": None}}
    ms.update(over)
    sent = []
    monkeypatch.setenv("GCODE_DIR", str(tmp_path / "gcodes"))
    (tmp_path / "gcodes").mkdir(exist_ok=True)
    monkeypatch.delenv("MOONRAKER_URL", raising=False)
    monkeypatch.setattr(moonraker, "machine_state", lambda: ms)
    monkeypatch.setattr(moonraker, "gcode", lambda s, timeout=15: sent.append(s) or {"result": "ok"})
    webapp._RUNNING.clear()
    return ms, sent


def _job(name, **st):
    job = Path(os.environ["PCB_MILL_JOBS"]) / name
    (job / "out").mkdir(parents=True, exist_ok=True)
    for n in ("isolation", "drill", "edge"):
        (job / "out" / f"{n}.nc").write_text("G21\nM3 S20000\nG1 X1 Y1\nM5\nM2\n")
    base = {"state": "done", "files": ["isolation.nc"], "extents": [0.8, 0.8, 90.0, 88.0],
            "board": {"w": 89.2, "h": 87.2}, "setup_done": True, "board_ready": True}
    base.update(st)
    (job / "status.json").write_text(json.dumps(base))
    return job


def test_setup_route_homes_and_moves_bed_forward(tmp_path, monkeypatch):
    _, sent = _fake_machine(monkeypatch, tmp_path)       # origin Y = 27.5
    job = _job("setup-1", setup_done=False, board_ready=False)
    client = TestClient(webapp.app)
    page = client.get(f"/jobs/{job.name}")
    assert "Home &amp; move bed forward" in page.text
    assert "EMERGENCY STOP" not in page.text

    async def run():
        r = client.post(f"/jobs/{job.name}/setup", follow_redirects=False)
        assert r.status_code == 303
        await asyncio.sleep(0.2)

    asyncio.run(run())
    time.sleep(0.5)
    assert sent and "G28" in sent[0] and "G0 Y100.000 F3000" in sent[0]
    st = json.loads((job / "status.json").read_text())
    assert st["setup_done"] is True and st["board_ready"] is False
    page = client.get(f"/jobs/{job.name}")
    assert "Board attached" in page.text and ">Find copper</button>" not in page.text

    r = client.post(f"/jobs/{job.name}/board-ready", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert json.loads((job / "status.json").read_text())["board_ready"] is True
    assert "Go to probe start (5, 5)" in r.text


def test_steps_follow_live_klipper_state(tmp_path, monkeypatch):
    _fake_machine(monkeypatch, tmp_path, homed="")
    job = _job("wf-1")
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert ">Home</button>" in r.text and ">Find copper</button>" not in r.text
    _fake_machine(monkeypatch, tmp_path, datum_done=False)          # still at the corner, high
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert "Go to probe start (5, 5)" in r.text and ">Find copper</button>" not in r.text
    _fake_machine(monkeypatch, tmp_path, datum_done=False, position=[30.4, 32.5, 12.0])
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert ">Find copper</button>" in r.text and ">Probe mesh</button>" not in r.text and "▼ 1" in r.text
    _fake_machine(monkeypatch, tmp_path, datum_done=True)
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert ">Probe mesh</button>" in r.text and "10 × 10" in r.text


def test_mesh_route_sends_planned_macro(tmp_path, monkeypatch):
    _, sent = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-2")

    async def run():
        r = TestClient(webapp.app).post(f"/jobs/{job.name}/mesh", follow_redirects=False)
        assert r.status_code == 303
        await asyncio.sleep(0.2)

    # TestClient runs the app loop in a thread; give the background task time
    asyncio.run(run())
    time.sleep(0.5)
    assert sent and sent[0].startswith("PCB_MESH X0=30.400 Y0=32.500")
    assert "SAMPLES=2" in sent[0]


def test_start_and_datum_routes_target_the_mesh_corner(tmp_path, monkeypatch):
    _, sent = _fake_machine(monkeypatch, tmp_path, datum_done=False)
    job = _job("wf-8")
    with TestClient(webapp.app) as c:
        assert c.post(f"/jobs/{job.name}/start", follow_redirects=False).status_code == 303
        time.sleep(0.5)
        webapp._RUNNING.clear()
        assert c.post(f"/jobs/{job.name}/datum", follow_redirects=False).status_code == 303
        time.sleep(0.5)
    assert sent[0] == "PCB_TO_START X=5 Y=5"
    assert sent[1] == "PCB_DATUM X=5 Y=5 DEPTH=25"


def test_mesh_route_refuses_without_datum(tmp_path, monkeypatch):
    _, sent = _fake_machine(monkeypatch, tmp_path, datum_done=False)
    job = _job("wf-3")
    r = TestClient(webapp.app).post(f"/jobs/{job.name}/mesh")
    assert r.status_code == 409 and not sent


def test_mesh_route_refuses_when_it_does_not_fit(tmp_path, monkeypatch):
    _, sent = _fake_machine(monkeypatch, tmp_path, datum_done=True, origin=(40.0, 30.0))
    job = _job("wf-4")
    r = TestClient(webapp.app).post(f"/jobs/{job.name}/mesh")
    assert r.status_code == 409 and "travel" in r.text and not sent


def test_send_requires_mesh_covering_job(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-5")
    r = TestClient(webapp.app).post(f"/jobs/{job.name}/send", data={"spindle_on": "on", "clips_off": "on"})
    assert r.status_code == 409          # no mesh yet
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0, 0.1], [0.05, 0.2]], lo=0.0, hi=0.2)
    r = TestClient(webapp.app).post(f"/jobs/{job.name}/send")
    assert r.status_code == 409 and "spindle" in r.text
    r = TestClient(webapp.app).post(f"/jobs/{job.name}/send",
                                    data={"spindle_on": "on", "clips_off": "on"},
                                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith(":8080/#/jobs")        # straight on to Fluidd
    d = tmp_path / "gcodes" / "wf-5"
    iso = (d / "1_isolation.gcode").read_text()
    assert iso.startswith(f"PCB_READY X0={plan.job[0]:.3f}") and "PCB_DEEP" not in iso
    assert iso.rstrip().endswith("G0 X0 Y100 F3000")
    assert sorted(f.suffix for f in d.iterdir() if f.is_file()) == [".gcode"] * 4
    assert (d / "2_drill.gcode").read_text().splitlines()[1] == "PCB_DEEP"
    assert (d / "3_edge.gcode").read_text().splitlines()[1] == "PCB_DEEP"
    combined = (d / "combined.gcode").read_text()
    assert combined.count("PCB_READY") == 3 and combined.count("M2") == 1
    assert combined.rstrip().endswith("G0 X0 Y100 F3000")


def test_mesh_can_be_skipped_and_still_sent(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("skip-send")
    c = TestClient(webapp.app)
    page = c.get(f"/jobs/{job.name}")
    assert "Skip mesh" in page.text
    r = c.post(f"/jobs/{job.name}/skip-mesh", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert json.loads((job / "status.json").read_text())["mesh_skipped"] is True
    assert "Mesh skipped" in r.text and "Send to Fluidd" in r.text
    r = c.post(f"/jobs/{job.name}/send", data={"spindle_on": "on", "clips_off": "on"},
               follow_redirects=False)
    assert r.status_code == 303
    d = tmp_path / "gcodes" / "skip-send"
    assert (d / "1_isolation.gcode").read_text().splitlines()[0].endswith("MESH=0")
    assert (d / "2_drill.gcode").read_text().splitlines()[1] == "PCB_DEEP"


def test_klipper_rounded_mesh_bounds_still_count_as_covering(tmp_path, monkeypatch):
    """Klipper re-rounds mesh_max to its grid (planned 88.858 -> reported 88.84);
    that must not leave the page stuck on the mesh step."""
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-9", extents=[0.0, 0.0, 64.96, 99.63], edge_extents=[0.0, 0.0, 65.4, 100.05])
    plan = probe.mesh_plan((0.0, 0.0, 64.96, 99.63), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1 - 0.018, plan.y1],
                      matrix=[[0.0, 0.1], [0.05, 0.2]], lo=0.0, hi=0.2)
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert "Send to Fluidd" in r.text


def test_send_refuses_mesh_that_is_too_small(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-6")
    ms["mesh"].update(profile="default", min=[28.0, 32.0], max=[46.0, 50.0],
                      matrix=[[0.0]], lo=0.0, hi=0.0)       # the 18 mm test patch
    assert TestClient(webapp.app).post(f"/jobs/{job.name}/send").status_code == 409


def test_oversize_job_is_rejected_page(tmp_path, monkeypatch):
    _fake_machine(monkeypatch, tmp_path)
    job = _job("wf-7", state="rejected", error="cut area is 102.0 × 50.0 mm; the limit is 100 × 100 mm")
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert r.status_code == 200 and "limit is 100" in r.text and ">Home</button>" not in r.text


def test_index_has_collapsed_advanced():
    client = TestClient(webapp.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "<details>" in r.text and "Cutting parameters" in r.text
    assert "0.15" in r.text  # isolation depth default shown


def test_deepen_only_touches_negative_plunges():
    g = "G0 Z1.0\nG1 Z-0.0700 F80\nG1 X1 Y2 F250\nG0 Z1.0\nG1 Z-0.0700 F80\n"
    out = probe.deepen(g, 0.05)
    assert out.count("G1 Z-0.1200 F80") == 2 and out.count("G0 Z1.0") == 2
    assert "G1 X1 Y2 F250" in out and out.endswith("\n")
    assert probe.deepen(g, 0) == g


def test_send_applies_extra_depth_to_isolation_only(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-10")
    for n in ("isolation", "drill", "edge"):
        (job / "out" / f"{n}.nc").write_text("G0 Z1.0\nG1 Z-0.0700 F80\nM5\n")
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0]], lo=0.0, hi=0.0)
    c = TestClient(webapp.app)
    assert c.post(f"/jobs/{job.name}/send", data={"extra_depth": "0.5"}).status_code == 400
    r = c.post(f"/jobs/{job.name}/send",
               data={"extra_depth": "0.05", "spindle_on": "on", "clips_off": "on"},
               follow_redirects=False)
    assert r.status_code == 303
    d = tmp_path / "gcodes" / "wf-10"
    assert "Z-0.1200" in (d / "1_isolation.gcode").read_text()
    assert "Z-0.0700" in (d / "2_drill.gcode").read_text()      # untouched


def test_rapids_get_explicit_feed_and_never_leak_into_cuts():
    g = "G21\nG0 Z1.0\nG0 X1 Y2 Z1.0\nG1 Z-0.12 F80\nG1 X3 Y3 F250\nG1 X4 Y4\nG0 Z1.0\nG1 X5 Y5\nG0 X0 Y0 F900\nM5\n"
    out = probe.add_rapids(g, xy=4000, z=1000).splitlines()
    assert "G0 Z1.0 F1000" in out and "G0 X1 Y2 Z1.0 F4000" in out
    assert "G0 X0 Y0 F900" in out                          # explicit F kept
    assert "G1 X4 Y4" in out                               # already inherits the cutting F
    assert "G1 X5 Y5 F250" in out                          # F restored after a rapid


def test_send_adds_rapid_feeds(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-11")
    (job / "out" / "drill.nc").write_text("G0 X1 Y1\nG1 Z-1.9 F50\nG0 Z1.0\nG0 X2 Y2\n")
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0]], lo=0.0, hi=0.0)
    assert TestClient(webapp.app).post(
        f"/jobs/{job.name}/send", data={"spindle_on": "on", "clips_off": "on"},
        follow_redirects=False).status_code == 303
    text = (tmp_path / "gcodes" / "wf-11" / "2_drill.gcode").read_text()
    assert "G0 X2 Y2 F4000" in text and "G0 Z1.0 F1000" in text


def test_htmx_actions_return_the_fragment_not_a_redirect(tmp_path, monkeypatch):
    _, sent = _fake_machine(monkeypatch, tmp_path, datum_done=False, position=[30.4, 32.5, 12.0])
    job = _job("wf-12")
    with TestClient(webapp.app) as c:
        r = c.post(f"/jobs/{job.name}/datum", headers={"HX-Request": "true"}, follow_redirects=False)
        assert r.status_code == 200 and "<html>" not in r.text and "steps" in r.text
        assert "datum" in r.text                          # already shows the running step
        time.sleep(0.5)


def test_send_via_htmx_redirects_the_browser_to_fluidd(tmp_path, monkeypatch):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-13")
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0]], lo=0.0, hi=0.0)
    r = TestClient(webapp.app).post(
        f"/jobs/{job.name}/send", data={"spindle_on": "on", "clips_off": "on"},
        headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.headers["HX-Redirect"].endswith("/#/jobs")


# ------------------------------------------------------------ upload detection
from web import detect  # noqa: E402

BOARD = Path(__file__).resolve().parent.parent / "board"


def _board_files():
    return [(p.name, p.read_bytes()) for p in sorted(BOARD.iterdir())]


def test_detect_kicad_files_by_content():
    a = detect.assign(_board_files())
    assert a["side"] == "top" and a["title"] == "AQI esp32 pcb"
    assert a["copper"][0].endswith("F_Cu.gbr") and a["edge"][0].endswith("Edge_Cuts.gbr")
    assert a["drill"][0].endswith("-PTH.drl") and a["drill2"][0].endswith("-NPTH.drl")
    assert a["roles"]["AQI esp32 pcb-job.gbrjob"] == detect.IGNORE


def test_detect_survives_renamed_files():
    files = [(f"mystery{i}.bin", d) for i, (_, d) in enumerate(_board_files())]
    a = detect.assign(files)
    assert a["side"] == "top" and a["drill2"] is not None      # X2 attributes, not names


def test_detect_falls_back_to_names_and_reports_what_is_missing():
    files = [("b-B_Cu.gbr", b"G04*"), ("b-Edge_Cuts.gbr", b"G04*"), ("b-PTH.drl", b"M48")]
    a = detect.assign(files)
    assert a["side"] == "bottom"
    with pytest.raises(detect.DetectError, match="outline"):
        detect.assign([f for f in files if "Edge" not in f[0]])
    with pytest.raises(detect.DetectError, match="copper"):
        detect.assign([("x.txt", b"hi")])


def test_detect_generic_edge_gbr_by_name():
    assert detect.classify("edge.gbr", b"G04*") == detect.EDGE
    files = [("board-B_Cu.gbr", b"G04*"), ("edge.gbr", b"G04*"), ("board-PTH.drl", b"M48")]
    a = detect.assign(files)
    assert a["edge"][0] == "edge.gbr"
    assert a["title"] == "board"          # named from the copper file, not the pile


def test_detect_two_copper_layers_must_be_resolved_by_hand():
    files = _board_files() + [("AQI esp32 pcb-B_Cu.gbr", b"%TF.FileFunction,Copper,L2,Bot*%\nG04*")]
    with pytest.raises(detect.DetectError, match="both"):
        detect.assign(files)
    names = [n for n, _ in files]
    manual = ["ignore" if n.endswith("F_Cu.gbr") else "" for n in names]
    assert detect.assign(files, manual)["side"] == "bottom"
    manual = ["ignore" if n.endswith("B_Cu.gbr") else "" for n in names]
    assert detect.assign(files, manual)["side"] == "top"


def test_manual_role_overrides_metadata():
    files = _board_files()
    manual = ["copper_bottom" if n.endswith("F_Cu.gbr") else "" for n, _ in files]
    a = detect.assign(files, manual)
    assert a["side"] == "bottom" and a["copper"][0].endswith("F_Cu.gbr")
    with pytest.raises(detect.DetectError, match="unknown"):
        detect.assign(files, ["bogus"] * len(files))


def test_detect_endpoint_lists_roles(monkeypatch):
    with TestClient(webapp.app) as c:
        r = c.post("/detect", files=[("files", (n, d)) for n, d in _board_files()]).json()
        roles = {f["name"]: f["role"] for f in r["files"]}
        assert roles["AQI esp32 pcb-F_Cu.gbr"] == "copper_top"
        assert {"value": "ignore", "label": "ignore"} in r["choices"]


def test_upload_endpoint_creates_a_named_job(monkeypatch):
    started = []

    async def fake_convert(*a, **k):
        started.append(a)
    monkeypatch.setattr(webapp, "_convert", fake_convert)
    with TestClient(webapp.app) as c:
        r = c.post("/upload", files=[("files", (n, d)) for n, d in _board_files()], follow_redirects=False)
        assert r.status_code == 303
        name = r.headers["location"].rsplit("/", 1)[1]
        assert name.endswith("AQI-esp32-pcb")
        st = json.loads((Path(os.environ["PCB_MILL_JOBS"]) / name / "status.json").read_text())
        assert st["title"] == "AQI esp32 pcb" and st["slug"] == "AQI-esp32-pcb" and st["side"] == "top"
        assert c.post("/upload", files=[("files", ("x.txt", b"nope"))]).status_code == 400
        assert "AQI esp32 pcb" in c.get("/").text                # listed on the home page

        # the upload form can override the name taken from the copper Gerber
        r = c.post("/upload", files=[("files", (n, d)) for n, d in _board_files()],
                   data={"title": "My Board"}, follow_redirects=False)
        assert r.status_code == 303
        name2 = r.headers["location"].rsplit("/", 1)[1]
        st2 = json.loads((Path(os.environ["PCB_MILL_JOBS"]) / name2 / "status.json").read_text())
        assert st2["title"] == "My Board" and st2["slug"] == "My-Board"


# ------------------------------------------------------------ delete + replace files
def _upload_job(c, **data):
    r = c.post("/upload", files=[("files", (n, d)) for n, d in _board_files()],
               data=data, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def _jobdir(name):
    return Path(os.environ["PCB_MILL_JOBS"]) / name


def _no_convert(monkeypatch):
    started = []

    async def fake_convert(*a, **k):
        started.append(a)
    monkeypatch.setattr(webapp, "_convert", fake_convert)
    return started


def test_delete_job_removes_folder(monkeypatch):
    _no_convert(monkeypatch)
    with TestClient(webapp.app) as c:
        name = _upload_job(c, title="To Delete")
        webapp._set_status(_jobdir(name), state="done")
        assert _jobdir(name).is_dir()
        r = c.post(f"/jobs/{name}/delete", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert not _jobdir(name).exists()
        assert c.post(f"/jobs/{name}/delete").status_code == 404
        assert c.post("/jobs/..%2F/delete").status_code == 404


def test_delete_and_replace_refused_while_converting(monkeypatch):
    _no_convert(monkeypatch)
    with TestClient(webapp.app) as c:
        name = _upload_job(c, title="Busy")           # fake convert leaves state "queued"
        assert c.post(f"/jobs/{name}/delete").status_code == 409
        files = [("files", (n, d)) for n, d in _board_files()]
        assert c.post(f"/jobs/{name}/replace", files=files).status_code == 409
        assert _jobdir(name).is_dir()


def test_replace_files_restarts_conversion_and_keeps_name_and_params(monkeypatch):
    started = _no_convert(monkeypatch)
    with TestClient(webapp.app) as c:
        name = _upload_job(c, title="Keep Me", **{"isolation.tool_diameter": "0.3"})
        job = _jobdir(name)
        cfg_before = (job / "mill_config.yaml").read_text()
        # pretend the job finished and progressed on the machine
        webapp._set_status(job, state="done", board_ready=True, sent=["isolation.nc"],
                           extents=[0, 0, 1, 1], toolpaths="out/toolpaths.png")
        (job / "out").mkdir()
        (job / "out" / "isolation.nc").write_text("G0")
        (job / "log.txt").write_text("old log")

        files = [(n, d) for n, d in _board_files()]
        # new copper file given a mystery name; the dropdown says what it is
        files = [("renamed.bin" if n.endswith("F_Cu.gbr") else n, d) for n, d in files]
        r = c.post(f"/jobs/{name}/replace", files=[("files", f) for f in files],
                   data={"role": ["copper_bottom" if n == "renamed.bin" else "" for n, _ in files]},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/jobs/{name}"

        st = json.loads((job / "status.json").read_text())
        assert st["state"] == "queued" and st["title"] == "Keep Me" and st["side"] == "bottom"
        for stale in ("board_ready", "sent", "extents", "toolpaths"):
            assert stale not in st
        assert not (job / "out").exists() and not (job / "log.txt").exists()
        assert (job / "mill_config.yaml").read_text() == cfg_before
        assert (job / "copper.bin").exists() and (job / "drill.drl").exists()
        assert len(started) == 2                      # once for the upload, once for the replace


def test_replace_files_rejects_bad_upload_without_touching_the_job(monkeypatch):
    _no_convert(monkeypatch)
    with TestClient(webapp.app) as c:
        name = _upload_job(c, title="Intact")
        webapp._set_status(_jobdir(name), state="done")
        r = c.post(f"/jobs/{name}/replace", files=[("files", ("x.txt", b"nope"))])
        assert r.status_code == 400
        assert (_jobdir(name) / "copper.gbr").exists()
        assert c.post("/jobs/nope/replace", files=[("files", ("x.txt", b"n"))]).status_code == 404


def test_job_and_index_pages_offer_delete_and_replace(monkeypatch):
    _no_convert(monkeypatch)
    with TestClient(webapp.app) as c:
        name = _upload_job(c, title="UI")
        page = c.get(f"/jobs/{name}").text
        assert f'action="/jobs/{name}/replace"' in page and f'action="/jobs/{name}/delete"' in page
        assert 'id="nameRow"' not in page            # replace keeps the existing name
        assert f'action="/jobs/{name}/delete"' in c.get("/").text


def test_page_goes_straight_to_send_when_machine_is_already_located_and_meshed(tmp_path, monkeypatch):
    """A fresh page load must not restart the homing steps just because this job's own
    setup flags are unset (new job, replaced files, another browser) when Klipper
    already has the origin, datum and a covering mesh."""
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-live", setup_done=False, board_ready=False)
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0, 0.1], [0.05, 0.2]], lo=0.0, hi=0.2)
    page = TestClient(webapp.app).get(f"/jobs/{job.name}").text
    assert "Send to Fluidd" in page
    assert "Home &amp; move bed forward" not in page and "Board attached" not in page
    assert 'class="done">Set up board' in page


def test_page_still_walks_the_steps_when_machine_is_not_located(tmp_path, monkeypatch):
    # same unset flags, but no datum/mesh yet: the setup steps must still be offered
    _fake_machine(monkeypatch, tmp_path, datum_done=False)
    job = _job("wf-notlive", setup_done=False, board_ready=False)
    page = TestClient(webapp.app).get(f"/jobs/{job.name}").text
    assert "Home &amp; move bed forward" in page and "Send to Fluidd" not in page


# ------------------------------------------------------------ mesh recovery
def _meshed(monkeypatch, tmp_path, **st):
    ms, _ = _fake_machine(monkeypatch, tmp_path, datum_done=True)
    job = _job("wf-mesh", **st)
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    return ms, job, plan


def test_interrupted_phase_does_not_leave_the_page_stuck(tmp_path, monkeypatch):
    """status.json says 'running' but this process is not running it (app restart,
    Klipper dropped): the page must offer Probe/Skip again instead of a spinner."""
    _meshed(monkeypatch, tmp_path, phase={"name": "mesh", "state": "running", "script": "PCB_MESH"})
    page = TestClient(webapp.app).get("/jobs/wf-mesh").text
    assert "mesh failed: interrupted" in page
    assert ">Probe mesh</button>" in page and "Skip mesh" in page
    assert "spinner" not in page
    st = json.loads((Path(os.environ["PCB_MILL_JOBS"]) / "wf-mesh" / "status.json").read_text())
    assert st["phase"]["state"] == "error"


def test_running_phase_in_this_process_still_shows_progress_and_abort(tmp_path, monkeypatch):
    _meshed(monkeypatch, tmp_path, phase={"name": "mesh", "state": "running", "script": "PCB_MESH"})
    webapp._RUNNING.add("wf-mesh")
    try:
        page = TestClient(webapp.app).get("/jobs/wf-mesh").text
    finally:
        webapp._RUNNING.discard("wf-mesh")
    assert "spinner" in page and "Abort (emergency stop)" in page


def test_abort_emergency_stops_and_frees_the_page(tmp_path, monkeypatch):
    _meshed(monkeypatch, tmp_path, phase={"name": "mesh", "state": "running", "script": "PCB_MESH"})
    calls = []
    monkeypatch.setattr(moonraker, "emergency_stop", lambda: calls.append("m112"))
    webapp._RUNNING.add("wf-mesh")
    c = TestClient(webapp.app)
    r = c.post("/jobs/wf-mesh/abort", headers={"HX-Request": "true"})
    assert r.status_code == 200 and calls == ["m112"]
    assert "stopped by you" in r.text and "wf-mesh" not in webapp._RUNNING
    # nothing running any more -> refuse
    assert c.post("/jobs/wf-mesh/abort").status_code == 409


def test_klipper_restart_offered_when_klipper_is_down(tmp_path, monkeypatch):
    _fake_machine(monkeypatch, tmp_path)
    _job("wf-down")

    def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(moonraker, "machine_state", boom)
    calls = []
    monkeypatch.setattr(moonraker, "firmware_restart", lambda: calls.append("fr"))
    c = TestClient(webapp.app)
    assert "Restart Klipper" in c.get("/jobs/wf-down").text
    assert c.post("/jobs/wf-down/klipper-restart", follow_redirects=False).status_code == 303 and calls == ["fr"]
    webapp._RUNNING.add("wf-down")
    try:
        assert c.post("/jobs/wf-down/klipper-restart").status_code == 409
    finally:
        webapp._RUNNING.discard("wf-down")


def test_mesh_can_be_redone_or_swapped_for_skip_afterwards(tmp_path, monkeypatch):
    ms, job, plan = _meshed(monkeypatch, tmp_path)
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0, 0.1], [0.05, 0.2]], lo=0.0, hi=0.2)
    page = TestClient(webapp.app).get("/jobs/wf-mesh").text
    assert "Send to Fluidd" in page and ">Redo mesh</button>" in page

    # skipped (no mesh on the machine): Send is available and the mesh can still be probed
    ms["mesh"].update(profile="", min=None, max=None, matrix=[], lo=None, hi=None)
    webapp._set_status(job, mesh_skipped=True)
    page = TestClient(webapp.app).get("/jobs/wf-mesh").text
    assert "Mesh skipped" in page and ">Probe mesh instead</button>" in page and "Send to Fluidd" in page


def test_static_assets_are_versioned_so_deploys_beat_the_browser_cache():
    page = TestClient(webapp.app).get("/").text
    assert re.search(r'/static/style\.css\?v=\d+', page)
    assert re.search(r'/static/upload\.js\?v=\d+', page)


# ------------------------------------------------------------ klipper watchdog
from web import watchdog  # noqa: E402

LOST = {"state": "shutdown", "state_message": "Lost communication with MCU 'mcu'\nOnce the underlying..."}
NOCONN = {"state": "error", "state_message": "mcu 'mcu': Unable to connect\nOnce..."}
ESTOP = {"state": "shutdown", "state_message": "Shutdown due to M112 command\nOnce the underlying..."}


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _dog(restarts):
    clock = _Clock()
    return watchdog.Watchdog(restart=lambda: restarts.append(clock.t), clock=clock), clock


def test_watchdog_recovers_a_lost_mcu_after_debounce(monkeypatch):
    monkeypatch.setattr(watchdog, "last_event", None)
    calls = []
    dog, clock = _dog(calls)
    assert dog.tick(LOST) is None and not calls            # first sighting: wait
    clock.t += watchdog.DEBOUNCE - 1
    assert dog.tick(LOST) is None and not calls
    clock.t += 2
    assert dog.tick(LOST) == "restarted" and len(calls) == 1
    assert watchdog.recent_event()["reason"].startswith("Lost communication")
    # a failed-to-connect Klipper counts too
    dog2, c2 = _dog(calls)
    dog2.tick(NOCONN)
    c2.t += watchdog.DEBOUNCE + 1
    assert dog2.tick(NOCONN) == "restarted"


def test_watchdog_never_undoes_an_emergency_stop_or_acts_when_healthy():
    calls = []
    dog, clock = _dog(calls)
    for info in (ESTOP, {"state": "ready", "state_message": "Printer is ready"}, None,
                 {"state": "startup", "state_message": "Startup"}):
        for _ in range(5):
            clock.t += 60
            assert dog.tick(info) is None
    assert calls == []


def test_watchdog_is_rate_limited_so_a_flapping_board_is_not_restarted_in_a_loop():
    calls = []
    dog, clock = _dog(calls)
    dog.tick(LOST)
    clock.t += watchdog.DEBOUNCE + 1
    assert dog.tick(LOST) == "restarted"
    clock.t += 5                                           # still down, too soon
    assert dog.tick(LOST) is None and len(calls) == 1
    for _ in range(20):                                    # keeps failing every MIN_GAP
        clock.t += watchdog.MIN_GAP + 1
        dog.tick(LOST)
    assert len(calls) == watchdog.MAX_ATTEMPTS             # then it stops
    clock.t += watchdog.WINDOW + 1                         # a long quiet spell resets the budget
    assert dog.tick(LOST) == "restarted"


def test_watchdog_survives_a_failing_restart_call(monkeypatch):
    monkeypatch.setattr(watchdog, "last_event", None)

    def boom():
        raise RuntimeError("moonraker down")
    clock = _Clock()
    dog = watchdog.Watchdog(restart=boom, clock=clock)
    dog.tick(LOST)
    clock.t += watchdog.DEBOUNCE + 1
    assert dog.tick(LOST) == "failed" and watchdog.recent_event()["ok"] is False


def test_job_page_tells_you_klipper_was_restarted_automatically(tmp_path, monkeypatch):
    _fake_machine(monkeypatch, tmp_path)
    _job("wf-wd")
    monkeypatch.setattr(watchdog, "last_event", {"at": time.time(), "reason": "Lost communication", "ok": True})
    page = TestClient(webapp.app).get("/jobs/wf-wd").text
    assert "restarted automatically" in page
    monkeypatch.setattr(watchdog, "last_event", {"at": time.time() - 7200, "reason": "x", "ok": True})
    assert "restarted automatically" not in TestClient(webapp.app).get("/jobs/wf-wd").text


def test_watchdog_only_runs_where_moonraker_is_configured(monkeypatch):
    monkeypatch.delenv("MOONRAKER_URL", raising=False)
    assert not watchdog.enabled()
    monkeypatch.setenv("MOONRAKER_URL", "http://127.0.0.1:7125")
    assert watchdog.enabled()
    monkeypatch.setenv("PCB_MILL_WATCHDOG", "0")
    assert not watchdog.enabled()
