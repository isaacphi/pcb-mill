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
from web import config_render, excellon  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "board"
PTH = BOARD / "AQI esp32 pcb-PTH.drl"
NPTH = BOARD / "AQI esp32 pcb-NPTH.drl"


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
        if st["state"] in ("done", "error"):
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
    assert st["state"] == "done", (tmp_path / "log.txt").read_text()[-2000:]
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
