"""pcb-mill web UI: upload Gerbers -> run mill.py -> preview -> probe -> mill.

Run locally:   uv run uvicorn web.app:app --port 8090
On the Pi:     MOONRAKER_URL=http://127.0.0.1:7125 uv run uvicorn web.app:app --host 0.0.0.0 --port 8090

Beyond conversion the job page walks the machine workflow, one button per
step, reading live state from Klipper (so a restart or a moved board shows up
immediately): home -> find the copper (PCB_DATUM) -> mesh the cut area
(PCB_MESH, ~10 mm spacing) -> send the job to Fluidd. All motion logic lives
in Klipper macros (klipper/printer.cfg); this app only sequences them and
refuses to proceed when the live state doesn't match. The job file starts
with PCB_READY, which aborts the cut unless the session has a datum and a
mesh covering the job. Jogging and the real e-stop stay in Fluidd.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config_render, excellon, moonraker, probe

ROOT = Path(__file__).resolve().parent.parent
JOBS = Path(__import__("os").environ.get("PCB_MILL_JOBS", ROOT / "web_jobs"))
JOBS.mkdir(exist_ok=True)

app = FastAPI(title="pcb-mill")
templates = Jinja2Templates(Path(__file__).parent / "templates")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

ALLOWED = {".gbr", ".gm1", ".gbl", ".gtl", ".drl", ".txt", ".xln"}


def _save(job: Path, upload: UploadFile, name: str) -> Path | None:
    if not upload or not upload.filename:
        return None
    if Path(upload.filename).suffix.lower() not in ALLOWED:
        raise HTTPException(400, f"{upload.filename}: unsupported extension")
    dest = job / name
    dest.write_bytes(upload.file.read())
    return dest


def _status(job: Path) -> dict:
    f = job / "status.json"
    return json.loads(f.read_text()) if f.exists() else {"state": "unknown"}


def _set_status(job: Path, **kw):
    st = _status(job)
    st.update(kw)
    (job / "status.json").write_text(json.dumps(st, indent=2))


MIRROR_RE = re.compile(r"mirror axis X = ([\d.]+)")


async def _convert(job: Path, copper: str, side: str, edge: str, drill: str, config: Path):
    """Run mill.py + plot_toolpaths.py as subprocesses; track state in status.json."""
    out = job / "out"
    log = job / "log.txt"

    async def run(cmd):
        with log.open("ab") as lf:
            lf.write(("\n$ " + " ".join(str(c) for c in cmd) + "\n").encode())
            p = await asyncio.create_subprocess_exec(
                *map(str, cmd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            data, _ = await p.communicate()
            lf.write(data)
            return p.returncode, data.decode(errors="replace")

    _set_status(job, state="converting")
    rc, text = await run([sys.executable, ROOT / "mill.py", "--copper", copper, "--side", side,
                          "--edge", edge, "--drill", drill, "--config", config, "-o", out])
    if rc != 0:
        _set_status(job, state="error", error="mill.py failed — see log")
        return
    m = MIRROR_RE.search(text)
    if m:
        _set_status(job, mirror_x=float(m.group(1)))
        rc, _ = await run([sys.executable, ROOT / "plot_toolpaths.py", out, m.group(1)])
        if rc != 0:
            _set_status(job, state="error", error="plot_toolpaths.py failed — see log")
            return
    files = sorted(p.name for p in out.glob("*.nc"))
    iso = out / "isolation.nc"
    cut = probe.extents_from_gcode([iso] if iso.exists() else list(out.glob("*.nc")))
    edge_nc = out / "edge.nc"
    edge_ext = probe.extents_from_gcode([edge_nc]) if edge_nc.exists() else None
    board = None
    if cut:
        w, h = probe.size_of(cut)
        board = {"w": round(w, 2), "h": round(h, 2)}
    problem = probe.validate_job(cut, edge_ext)
    _set_status(job, state="rejected" if problem else "done", error=problem,
                files=files, extents=cut, edge_extents=edge_ext, board=board,
                toolpaths="out/toolpaths.png" if (out / "toolpaths.png").exists() else None)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    inv = config_render.load_inventory()
    return templates.TemplateResponse(request=request, name="index.html", context={
        "inv": inv, "moonraker": moonraker.available(), "fluidd": moonraker.fluidd()})


@app.post("/jobs")
async def create_job(
    copper: UploadFile = File(...),
    edge: UploadFile = File(...),
    drill: UploadFile = File(...),
    drill2: UploadFile | None = File(None),
    side: str = Form("bottom"),
    request: Request = None,
):
    job = JOBS / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    job.mkdir(parents=True)

    c = _save(job, copper, "copper" + Path(copper.filename).suffix.lower())
    e = _save(job, edge, "edge" + Path(edge.filename).suffix.lower())
    d1 = _save(job, drill, "drill_pth" + Path(drill.filename).suffix.lower())
    d2 = _save(job, drill2, "drill_npth" + Path(drill2.filename).suffix.lower()) if drill2 and drill2.filename else None

    # merge PTH+NPTH if two drill files were given; report holes vs the drill bit
    if d2:
        merged = job / "drill.drl"
        merged.write_text(excellon.merge(d1.read_text(), d2.read_text()))
        drill_path = merged
    else:
        drill_path = d1

    form = await request.form()
    overrides = {}
    tool_by_op = {}
    for op in ("isolation", "drill", "edge"):
        if t := form.get(f"tool.{op}"):
            tool_by_op[op] = t
        for key in config_render.SCALAR_KEYS[op]:
            if (v := form.get(f"{op}.{key}")) not in (None, ""):
                overrides.setdefault(op, {})[key] = v

    cfg = config_render.render_config(overrides, tool_by_op)
    cfg_path = job / "mill_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))

    report = excellon.hole_report(
        excellon.parse(drill_path.read_text()),
        cfg["tools"]["drill"]["tool_diameter"],
        cfg["tools"]["drill"]["bore_tolerance"])
    (job / "status.json").write_text(json.dumps({
        "state": "queued", "side": side, "report": report,
        "files": [], "created": time.strftime("%F %T")}, indent=2))

    asyncio.create_task(_convert(job, str(c), side, str(e), str(drill_path), cfg_path))
    return RedirectResponse(f"/jobs/{job.name}", status_code=303)


def _live() -> dict | None:
    if not moonraker.control_available():
        return None
    try:
        return moonraker.machine_state()
    except Exception:  # noqa: BLE001 - Klipper down: page still renders, steps show "offline"
        return None


def _plan_for(st: dict, ms: dict | None):
    """MeshPlan for the job at the saved board origin, or None if unknown."""
    if not (st.get("extents") and ms and ms.get("origin")):
        return None
    boxes = [tuple(b) for b in (st["extents"], st.get("edge_extents")) if b]
    union = (min(b[0] for b in boxes), min(b[1] for b in boxes),
             max(b[2] for b in boxes), max(b[3] for b in boxes))
    try:
        return probe.mesh_plan(tuple(st["extents"]), ms["origin"], toolpaths=union)
    except ValueError:
        return None


def _mesh_covers(plan, ms: dict) -> bool:
    m = ms["mesh"]
    if not (plan and m["profile"] and m["min"] and m["max"]):
        return False
    tol = 0.25   # Klipper re-rounds the mesh bounds to its grid (e.g. 88.858 -> 88.84)
    return (m["min"][0] <= plan.x0 + tol and m["min"][1] <= plan.y0 + tol
            and m["max"][0] >= plan.x1 - tol and m["max"][1] >= plan.y1 - tol)


def _job_context(request: Request, name: str) -> dict:
    job = JOBS / name
    if not job.is_dir():
        raise HTTPException(404)
    st = _status(job)
    log = (job / "log.txt").read_text(errors="replace")[-4000:] if (job / "log.txt").exists() else ""
    elapsed = None
    if st.get("created"):
        try:
            elapsed = int(time.time() - time.mktime(time.strptime(st["created"], "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            pass
    ms = _live() if st.get("state") == "done" else None
    plan = _plan_for(st, ms)
    steps = None
    if ms is not None:
        steps = {
            "origin": ms["origin"] is not None,
            "homed": all(a in ms["homed"] for a in "xyz"),
            "datum": ms["datum_done"],
            "mesh": _mesh_covers(plan, ms),
        }
        if plan and ms["origin"]:
            at_x = ms["position"][0] - ms["origin"][0]
            at_y = ms["position"][1] - ms["origin"][1]
            steps["at_start"] = abs(at_x - plan.job[0]) < 0.3 and abs(at_y - plan.job[1]) < 0.3
    return {"request": request, "name": name, "st": st, "log": log, "elapsed": elapsed,
            "moonraker": moonraker.available(), "moonraker_control": moonraker.control_available(),
            "fluidd": moonraker.fluidd(), "ms": ms, "plan": plan, "steps": steps,
            "phase": st.get("phase")}


@app.get("/jobs/{name}", response_class=HTMLResponse)
async def job_page(request: Request, name: str):
    return templates.TemplateResponse(request=request, name="job.html", context=_job_context(request, name))


@app.get("/jobs/{name}/fragment", response_class=HTMLResponse)
async def job_fragment(request: Request, name: str):
    """htmx poll target: just the dynamic body of the job page."""
    return templates.TemplateResponse(request=request, name="_job_body.html", context=_job_context(request, name))


@app.get("/jobs/{name}/files/{path:path}")
async def job_file(name: str, path: str):
    f = (JOBS / name / path).resolve()
    if not f.is_file() or JOBS.resolve() not in f.parents:
        raise HTTPException(404)
    return FileResponse(f, filename=f.name)


# ---------------------------------------------------------------------------
# machine phases: each runs ONE Klipper macro in the background, tracked in
# status.json so the page can poll it. Only one phase runs at a time.
# ---------------------------------------------------------------------------
_RUNNING: set[str] = set()


async def _run_phase(job: Path, phase: str, script: str, timeout: float):
    _RUNNING.add(job.name)
    _set_status(job, phase={"name": phase, "state": "running", "script": script,
                            "started": time.time()})
    try:
        await asyncio.to_thread(moonraker.gcode, script, timeout)
    except Exception as e:  # noqa: BLE001 - Klipper's message goes straight to the UI
        _set_status(job, phase={"name": phase, "state": "error", "script": script,
                                "error": str(e)[:400]})
    else:
        _set_status(job, phase={"name": phase, "state": "done", "script": script})
    finally:
        _RUNNING.discard(job.name)


def _job_ready(name: str) -> tuple[Path, dict]:
    job = JOBS / name
    st = _status(job)
    if st.get("state") != "done":
        raise HTTPException(409, "job not ready: " + str(st.get("error") or st.get("state")))
    if not moonraker.control_available():
        raise HTTPException(409, "Moonraker control not available")
    if _RUNNING:
        raise HTTPException(409, "a machine step is already running")
    return job, st


def _start(job: Path, phase: str, script: str, timeout: float = 120):
    asyncio.create_task(_run_phase(job, phase, script, timeout))
    return RedirectResponse(f"/jobs/{job.name}", status_code=303)


@app.post("/jobs/{name}/origin")
async def set_origin(name: str):
    """Save the bit's current XY as the board corner (SET_BOARD_ORIGIN, persisted)."""
    _job_ready(name)
    moonraker.gcode("SET_BOARD_ORIGIN")
    return RedirectResponse(f"/jobs/{name}", status_code=303)


@app.post("/jobs/{name}/home")
async def home(name: str):
    job, _ = _job_ready(name)
    return _start(job, "home", "G28", timeout=180)


@app.post("/jobs/{name}/datum")
async def datum(name: str):
    """Find the copper under the bit (bounded 3 mm search; run again if no contact)."""
    job, _ = _job_ready(name)
    ms = _live()
    if not ms or not all(a in ms["homed"] for a in "xyz"):
        raise HTTPException(409, "home first")
    plan = _plan_for(_status(job), ms)
    if plan is None:
        raise HTTPException(409, "board origin not set")
    # the macro moves to the start point first (a no-op if the bit is already there)
    return _start(job, "datum", f"PCB_DATUM X={plan.job[0]:g} Y={plan.job[1]:g} DEPTH=3", timeout=90)


@app.post("/jobs/{name}/start")
async def goto_start(name: str):
    """Move X/Y to the probe start point (mesh corner) at the current height."""
    job, st = _job_ready(name)
    plan = _plan_for(st, _live())
    if plan is None:
        raise HTTPException(409, "board origin not set")
    return _start(job, "start", f"PCB_TO_START X={plan.job[0]:g} Y={plan.job[1]:g}", timeout=60)


@app.post("/jobs/{name}/mesh")
async def mesh(name: str):
    job, st = _job_ready(name)
    ms = _live()
    if not ms or not ms["datum_done"]:
        raise HTTPException(409, "find the copper (datum) first")
    plan = _plan_for(st, ms)
    if plan is None:
        raise HTTPException(409, "board origin not set")
    if not plan.fits:
        raise HTTPException(409, plan.problem)
    return _start(job, "mesh", plan.gcode(), timeout=plan.seconds * 2 + 120)


@app.post("/jobs/{name}/lift")
async def lift(name: str):
    """MILL_LIFT: raise Z, return to the board origin. For getting out of trouble."""
    job, _ = _job_ready(name)
    return _start(job, "lift", "MILL_LIFT", timeout=60)


@app.post("/jobs/{name}/estop")
async def estop(name: str):
    moonraker.estop()
    return RedirectResponse(f"/jobs/{name}", status_code=303)


@app.post("/jobs/{name}/jog")
async def jog(name: str, dz: float = Form(...)):
    """Z jog. Up is a plain move; down is a bounded probe descent (stops on copper)."""
    _job_ready(name)
    if dz == 0 or abs(dz) > 10:
        raise HTTPException(400, "dz must be non-zero and within ±10 mm")
    try:
        await asyncio.to_thread(moonraker.gcode, f"JOG_Z DZ={dz:g}", 60)
    except moonraker.KlipperError as e:
        # a bounded descent that finds nothing is the normal outcome of "jog down"
        if "No trigger on probe" not in str(e):
            raise HTTPException(409, str(e))
    return RedirectResponse(f"/jobs/{name}", status_code=303)


# which generated file gets which header (drill/edge go below the copper -> PCB_DEEP)
RUN_FILES = (("isolation.nc", False), ("drill.nc", True), ("edge.nc", True))


@app.post("/jobs/{name}/send")
async def send_to_machine(name: str):
    """Upload isolation, drill and edge jobs (each with a PCB_READY header) once
    datum + mesh are live for this board position."""
    job, st = _job_ready(name)
    ms = _live()
    plan = _plan_for(st, ms)
    if not (ms and ms["datum_done"] and plan and _mesh_covers(plan, ms)):
        raise HTTPException(409, "datum and mesh must be done for this board position first")
    out = job / "out"
    if not (out / "isolation.nc").exists():
        raise HTTPException(409, "isolation.nc missing (double-sided jobs are not supported yet)")
    sent = []
    for src_name, deep in RUN_FILES:
        src = out / src_name
        if not src.exists():
            continue
        # Klipper's virtual_sdcard only lists .gcode/.g/.gco; a .nc file is
        # invisible to it and "Print" fails with "Unable to open file".
        # top level of the gcodes root (no folders to hunt through in Fluidd's Jobs list);
        # the job id prefix keeps jobs apart and the number gives the run order
        run = out / f"{name[-6:]}_{len(sent) + 1}_{src.stem}.gcode"
        run.write_text(probe.with_header(src.read_text(), plan.job, deep))
        moonraker.upload(run)
        sent.append(run.name)
    _set_status(job, sent=sent)
    # straight on to Fluidd's job list: that is where the cut is started
    return RedirectResponse(f"{moonraker.fluidd()}/#/jobs", status_code=303)
