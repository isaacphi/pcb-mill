"""Optional push of finished .nc files to Moonraker (the Klipper API on the Pi).

Disabled unless MOONRAKER_URL is set (e.g. http://127.0.0.1:7125 when this app
runs on the Pi, or http://anycubic-octopi.local:7125 from a laptop). When
disabled, the job page just offers file downloads.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx


def url() -> str | None:
    u = os.environ.get("MOONRAKER_URL", "").strip().rstrip("/")
    return u or None


def upload(path: Path, subdir: str = "") -> dict:
    """Upload one file into Moonraker's gcodes root. -> API response json."""
    base = url()
    if not base:
        raise RuntimeError("MOONRAKER_URL not set")
    with path.open("rb") as f:
        r = httpx.post(
            f"{base}/server/files/upload",
            files={"file": (path.name, f, "application/octet-stream")},
            data={"path": subdir} if subdir else {},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()
