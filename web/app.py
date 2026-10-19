"""pcb-mill web UI (v1): upload Gerbers -> run mill.py -> preview -> download/push.

Run locally:   uv run uvicorn web.app:app --port 8090
On the Pi:     MOONRAKER_URL=http://127.0.0.1:7125 uv run uvicorn web.app:app --host 0.0.0.0 --port 8090

Machine control (jog/probe/mesh/pause/estop) stays in Fluidd — this app only
does the conversion layer. See docs/gerber-web-ui-design.md.
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

from . import config_render, excellon, moonraker

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
    _set_status(job, state="done", files=files,
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


@app.get("/jobs/{name}", response_class=HTMLResponse)
async def job_page(request: Request, name: str):
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
    return templates.TemplateResponse(request=request, name="job.html", context={
        "name": name, "st": st, "log": log, "elapsed": elapsed,
        "moonraker": moonraker.available(), "fluidd": moonraker.fluidd()})


@app.get("/jobs/{name}/files/{path:path}")
async def job_file(name: str, path: str):
    f = (JOBS / name / path).resolve()
    if not f.is_file() or JOBS.resolve() not in f.parents:
        raise HTTPException(404)
    return FileResponse(f, filename=f.name)


@app.post("/jobs/{name}/send")
async def send_to_machine(name: str):
    job = JOBS / name
    st = _status(job)
    if st.get("state") != "done":
        raise HTTPException(409, "job not finished")
    sent = []
    for fn in st["files"]:
        moonraker.upload(job / "out" / fn, subdir=f"pcb-mill/{name}")
        sent.append(fn)
    _set_status(job, sent=sent)
    return RedirectResponse(f"/jobs/{name}", status_code=303)
