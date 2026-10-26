"""Tests for the pcb-mill web app: excellon parsing/merging, config rendering,
and an end-to-end conversion of the sample board through the real mill.py."""

import asyncio
import json
import os
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


def test_convert_sample_board_direct(tmp_path):
    """Run the _convert pipeline (mill.py + plot) against the sample board."""
    import yaml
    cfg = config_render.render_config()
    cfg_path = tmp_path / "mill_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    (tmp_path / "status.json").write_text(json.dumps({"state": "queued"}))
    asyncio.run(webapp._convert(tmp_path, str(SAMPLE["copper"]), "top",
                                str(SAMPLE["edge"]), str(SAMPLE["drill"]), cfg_path))
    st = json.loads((tmp_path / "status.json").read_text())
    # the sample ESP32 board is ~70 x 100 mm: just fits the 100 mm pocket
    assert st["state"] == "done", (st.get("error"), (tmp_path / "log.txt").read_text()[-2000:])
    assert st["extents"] and 90 < st["extents"][3] <= 101
    assert st["edge_extents"]
    assert "onesided.nc" in st["files"]
    assert st["mirror_x"] > 0
    assert (tmp_path / "out" / "toolpaths.png").exists()


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


def test_processing_page():
    """A converting job renders the processing screen with elapsed time."""
    job = Path(os.environ["PCB_MILL_JOBS"]) / "fakejob-processing"
    job.mkdir()
    (job / "status.json").write_text(json.dumps({
        "state": "converting", "created": time.strftime("%F %T"), "files": []}))
    client = TestClient(webapp.app)
    r = client.get(f"/jobs/{job.name}")
    assert r.status_code == 200
    assert "spinner" in r.text and "Converting" in r.text and "elapsed" in r.text


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


# ------------------------------------------------ workflow routes (Klipper faked)

def _fake_machine(monkeypatch, tmp_path, **over):
    ms = {"klipper": "ready", "homed": "xyz", "position": [26.0, 30.0, 25.0],
          "origin": (25.4, 27.5), "datum_done": False,
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
        (job / "out" / f"{n}.nc").write_text("G21\nM3 S20000\nG1 X1 Y1\nM5\n")
    base = {"state": "done", "files": ["isolation.nc"], "extents": [0.8, 0.8, 90.0, 88.0],
            "board": {"w": 89.2, "h": 87.2}}
    base.update(st)
    (job / "status.json").write_text(json.dumps(base))
    return job


def test_steps_follow_live_klipper_state(tmp_path, monkeypatch):
    _fake_machine(monkeypatch, tmp_path, homed="")
    job = _job("wf-1")
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert "Home (G28)" in r.text and "Find the copper" not in r.text
    _fake_machine(monkeypatch, tmp_path, datum_done=False)
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert "Find the copper" in r.text and "Probe &amp; mesh" not in r.text
    _fake_machine(monkeypatch, tmp_path, datum_done=True)
    r = TestClient(webapp.app).get(f"/jobs/{job.name}")
    assert "Probe &amp; mesh" in r.text and "10 × 10" in r.text


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
    assert "SAMPLES=1" in sent[0]


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
    r = TestClient(webapp.app).post(f"/jobs/{job.name}/send")
    assert r.status_code == 409          # no mesh yet
    plan = probe.mesh_plan((0.8, 0.8, 90.0, 88.0), (25.4, 27.5))
    ms["mesh"].update(profile="default", min=[plan.x0, plan.y0], max=[plan.x1, plan.y1],
                      matrix=[[0.0, 0.1], [0.05, 0.2]], lo=0.0, hi=0.2)
    r = TestClient(webapp.app).post(f"/jobs/{job.name}/send", follow_redirects=False)
    assert r.status_code == 303
    d = tmp_path / "gcodes" / "pcb-mill" / job.name
    iso = (d / "1_isolation_run.nc").read_text()
    assert iso.startswith(f"PCB_READY X0={plan.job[0]:.3f}") and "PCB_DEEP" not in iso
    assert (d / "2_drill_run.nc").read_text().splitlines()[1] == "PCB_DEEP"
    assert (d / "3_edge_run.nc").read_text().splitlines()[1] == "PCB_DEEP"


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
    assert r.status_code == 200 and "limit is 100" in r.text and "Home (G28)" not in r.text


def test_index_has_collapsed_advanced():
    client = TestClient(webapp.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "<details>" in r.text and "Cutting parameters" in r.text
    assert "0.07" in r.text  # shallow mesh-based isolation depth shown
