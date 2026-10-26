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


def gcode(script: str, timeout: float = 15) -> dict:
    """Run G-code and block until Klipper finishes it. Raises KlipperError."""
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.post(f"{base}/printer/gcode/script", data={"script": script}, timeout=timeout)
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = f"HTTP {r.status_code}"
        raise KlipperError(msg)
    return r.json()


def estop() -> None:
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    httpx.post(f"{base}/printer/emergency_stop", timeout=5)


def query(objects: str) -> dict:
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.get(f"{base}/printer/objects/query?{objects}", timeout=15)
    r.raise_for_status()
    return r.json()["result"]["status"]


def toolhead_xy() -> tuple[float, float]:
    p = query("toolhead")["toolhead"]["position"]
    return float(p[0]), float(p[1])


def bed_mesh() -> dict:
    return query("bed_mesh")["bed_mesh"]


def printer_state() -> str:
    base = _api()
    if not base:
        raise RuntimeError("Moonraker control not available")
    r = httpx.get(f"{base}/printer/info", timeout=15)
    r.raise_for_status()
    return r.json()["result"]["state"]


def fluidd() -> str:
    return os.environ.get("FLUIDD_URL", "http://anycubic-octopi.local:8080").rstrip("/")


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
    s = query("toolhead&bed_mesh&gcode_macro%20PCB_DATUM&save_variables&webhooks")
    v = s["save_variables"]["variables"]
    mesh = s["bed_mesh"]
    origin = None
    if "board_x" in v and "board_y" in v:
        origin = (float(v["board_x"]), float(v["board_y"]))
    matrix = mesh.get("probed_matrix") or []
    flat = [z for row in matrix for z in row]
    return {
        "klipper": s["webhooks"]["state"],
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
