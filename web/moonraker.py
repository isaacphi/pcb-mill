"""Hand finished .nc files to the machine (Klipper via Moonraker).

Two paths, tried in order:
1. Direct write into Moonraker's gcodes root (GCODE_DIR env, default
   ~/printer_data/gcodes) when it exists locally — i.e. the app runs on the
   Pi. Moonraker's file watcher picks the files up and Fluidd shows them
   immediately; no HTTP round-trip.
2. Moonraker's HTTP upload API (MOONRAKER_URL) — for running the app on a
   laptop against the Pi. Fluidd shows the files the same way.

With neither configured, the job page just offers file downloads.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx


def url() -> str | None:
    u = os.environ.get("MOONRAKER_URL", "").strip().rstrip("/")
    return u or None


def gcode_dir() -> Path | None:
    """Moonraker's gcodes root, if reachable from this host."""
    d = os.environ.get("GCODE_DIR", "").strip() or "~/printer_data/gcodes"
    p = Path(d).expanduser()
    return p if p.is_dir() else None


def available() -> bool:
    return gcode_dir() is not None or url() is not None


def _api() -> str | None:
    """Moonraker HTTP base for control calls (gcode/query).

    On the Pi, Moonraker is local even when MOONRAKER_URL is unset (the
    upload path goes through the gcodes dir, but control needs HTTP).
    """
    u = url()
    if u:
        return u
    if gcode_dir() is not None:
        return "http://127.0.0.1:7125"
    return None


def control_available() -> bool:
    return _api() is not None


class KlipperError(RuntimeError):
    """A G-code script failed inside Klipper (message is Klipper's own)."""


def _error_message(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {r.status_code}"


def gcode(script: str, timeout: float = 15) -> dict:
    """Run G-code and block until Klipper finishes it. Raises KlipperError."""
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.post(f"{base}/printer/gcode/script", data={"script": script}, timeout=timeout)
    if r.status_code >= 400:
        raise KlipperError(_error_message(r))
    return r.json()


def cancel() -> None:
    """Normal stop: Klipper cancels the virtual-SD job after the current move."""
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.post(f"{base}/printer/print/cancel", timeout=10)
    if r.status_code >= 400:
        raise KlipperError(_error_message(r))


def pause() -> None:
    """PAUSE: lifts the bit and stops the spindle (see the PAUSE macro in printer.cfg)."""
    _post("/printer/print/pause")


def resume() -> None:
    """RESUME: spindle on, bit back down, carry on."""
    _post("/printer/print/resume")


def _post(path: str) -> None:
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.post(f"{base}{path}", timeout=30)
    if r.status_code >= 400:
        raise KlipperError(_error_message(r))


def info() -> dict | None:
    """Klipper's /printer/info (state + state_message), or None if Moonraker is unreachable."""
    base = _api()
    if not base:
        return None
    try:
        r = httpx.get(f"{base}/printer/info", timeout=5)
        r.raise_for_status()
        return r.json()["result"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def emergency_stop() -> None:
    """M112: halts everything at once. Klipper then sits in shutdown until restarted."""
    _post("/printer/emergency_stop")


def firmware_restart() -> None:
    """Reset the MCU and reload the config. Clears homing, datum and mesh."""
    _post("/printer/firmware_restart")


def start_print(filename: str) -> None:
    """Start a file already in Moonraker's gcodes folder (path relative to it, e.g. 'job/1_isolation.gcode')."""
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.post(f"{base}/printer/print/start", params={"filename": filename}, timeout=15)
    if r.status_code >= 400:
        raise KlipperError(_error_message(r))


def remove_folder(subdir: str) -> None:
    """Delete a job's folder from the gcodes directory (only possible when it is local)."""
    gd = gcode_dir()
    if gd is None or not subdir or "/" in subdir or subdir.startswith("."):
        return
    target = gd / subdir
    if target.is_dir() and gd.resolve() in target.resolve().parents:
        shutil.rmtree(target, ignore_errors=True)


def serial_present() -> bool | None:
    """Is the machine's USB serial device (printer.cfg [mcu] serial) there? None if unknown."""
    cfg = Path(os.environ.get("PRINTER_CFG", "~/printer_data/config/printer.cfg")).expanduser()
    try:
        text = cfg.read_text()
    except OSError:
        return None
    sect = None
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("["):
            sect = line.strip("[]").strip()
        elif sect == "mcu" and line.lower().startswith("serial"):
            return Path(line.split(":", 1)[-1].split("=", 1)[-1].strip()).exists()
    return None


def link_status() -> dict | None:
    """Is Klipper up and talking to the machine? None where there is no Moonraker control (dev Mac).

    -> {"state": ready|startup|shutdown|error|unreachable, "message": str, "adapter": bool|None}
    """
    if not control_available():
        return None
    i = info()
    adapter = serial_present()
    if i is None:
        return {"state": "unreachable", "message": "", "adapter": adapter}
    msg = (i.get("state_message") or "").strip().splitlines()
    return {"state": i.get("state", "unreachable"), "message": msg[0] if msg else "", "adapter": adapter}


def query(objects: str) -> dict:
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.get(f"{base}/printer/objects/query?{objects}", timeout=15)
    r.raise_for_status()
    return r.json()["result"]["status"]


def fluidd(host: str | None = None) -> str:
    """Fluidd's address: FLUIDD_URL if set, else port 8080 on whatever host the browser used
    to reach this page (so it works by IP, hostname or .local alike)."""
    env = os.environ.get("FLUIDD_URL", "").strip()
    if env:
        return env.rstrip("/")
    return f"http://{host or 'pcb-mill.local'}:8080"


def upload(path: Path, subdir: str = "") -> dict:
    """Hand one file to the machine; it shows up in Fluidd either way."""
    gd = gcode_dir()
    if gd is not None:
        dest_dir = gd / subdir if subdir else gd
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        shutil.copy2(path, dest)
        return {"result": "ok", "via": "direct-write", "path": str(dest.relative_to(gd))}
    base = url()
    if not base:
        raise RuntimeError("no gcodes dir and MOONRAKER_URL not set")
    with path.open("rb") as f:
        r = httpx.post(
            f"{base}/server/files/upload",
            files={"file": (path.name, f, "application/octet-stream")},
            data={"path": subdir} if subdir else {},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()


def machine_state() -> dict:
    """Live Klipper session facts the workflow depends on (read-only)."""
    s = query("toolhead&bed_mesh&gcode_macro%20PCB_DATUM&save_variables&webhooks&print_stats&virtual_sdcard")
    v = s["save_variables"]["variables"]
    mesh = s["bed_mesh"]
    origin = None
    if "board_x" in v and "board_y" in v:
        origin = (float(v["board_x"]), float(v["board_y"]))
    matrix = mesh.get("probed_matrix") or []
    flat = [z for row in matrix for z in row]
    return {
        "klipper": s["webhooks"]["state"],
        "print": {
            "state": s["print_stats"]["state"],
            "filename": s["print_stats"].get("filename") or "",
            "progress": (s.get("virtual_sdcard") or {}).get("progress") or 0.0,
        },
        "homed": s["toolhead"]["homed_axes"],
        "position": s["toolhead"]["position"][:3],
        "origin": origin,
        "datum_done": bool(s["gcode_macro PCB_DATUM"]["done"]),
        "mesh": {
            "profile": mesh.get("profile_name") or "",
            "min": mesh.get("mesh_min"), "max": mesh.get("mesh_max"),
            "matrix": matrix,
            "lo": min(flat) if flat else None, "hi": max(flat) if flat else None,
        },
    }
