"""Bring Klipper back by itself after it loses the machine's controller board.

systemd can't do this: when the MCU connection drops, Klipper's *process* stays up and
just reports state "shutdown" (or "error" if it never connected), so there is no crash
to restart. Only FIRMWARE_RESTART recovers it. This polls /printer/info and issues one
for the failures that are about the link, not about the machine:

  * recovers   "Lost communication with MCU", "Unable to connect", ...
  * leaves     everything else, in particular an emergency stop (M112) or a
               machine-side fault. Those need a person (the page's Restart Klipper button).

Safety rails: only after the fault has persisted for DEBOUNCE seconds, never closer than
MIN_GAP apart, and at most MAX_ATTEMPTS per WINDOW, so a board that keeps dropping
(bad power, loose cable) can't be restarted in a loop. A restart moves nothing, but it
does clear homing, the copper datum and the mesh, which the job page then asks for again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

from . import moonraker

log = logging.getLogger("pcb-mill.watchdog")

INTERVAL = 10.0          # poll period (s)
DEBOUNCE = 10.0          # fault must persist this long before we act (s)
MIN_GAP = 30.0           # minimum time between restarts (s)
WINDOW = 1800.0          # ...and at most MAX_ATTEMPTS inside this window (s)
MAX_ATTEMPTS = 5
REMEMBER = 3600.0        # how long the page keeps telling you about a restart (s)

RECOVERABLE = ("lost communication with mcu", "unable to connect",
               "unable to issue reset", "timeout on connect")

last_event: dict | None = None     # {"at": epoch, "reason": str, "ok": bool}


def recoverable(info: dict | None) -> bool:
    if not info or info.get("state") not in ("shutdown", "error"):
        return False
    msg = (info.get("state_message") or "").lower()
    return any(k in msg for k in RECOVERABLE)


def recent_event(now: float | None = None) -> dict | None:
    """The last automatic restart, if it was within REMEMBER seconds."""
    if last_event and (now or time.time()) - last_event["at"] < REMEMBER:
        return last_event
    return None


class Watchdog:
    def __init__(self, restart=moonraker.firmware_restart, clock=time.monotonic):
        self.restart, self.clock = restart, clock
        self.bad_since: float | None = None
        self.attempts: deque[float] = deque()

    def tick(self, info: dict | None) -> str | None:
        """One poll. -> "restarted" / "failed" when it acted, else None."""
        global last_event
        if info is None or info.get("state") == "ready" or not recoverable(info):
            self.bad_since = None
            return None
        now = self.clock()
        if self.bad_since is None:
            self.bad_since = now
            return None
        if now - self.bad_since < DEBOUNCE:
            return None
        while self.attempts and now - self.attempts[0] > WINDOW:
            self.attempts.popleft()
        if len(self.attempts) >= MAX_ATTEMPTS or (self.attempts and now - self.attempts[-1] < MIN_GAP):
            return None
        self.attempts.append(now)
        reason = (info.get("state_message") or "").strip().splitlines()[0]
        try:
            self.restart()
            ok = True
        except Exception as e:           # Moonraker/Klipper not answering: try again later
            log.warning("auto FIRMWARE_RESTART failed: %s", e)
            ok = False
        last_event = {"at": time.time(), "reason": reason, "ok": ok}
        log.warning("Klipper: %s -> FIRMWARE_RESTART %s", reason, "sent" if ok else "FAILED")
        return "restarted" if ok else "failed"

    async def run(self):
        while True:
            try:
                info = await asyncio.to_thread(moonraker.info)
                await asyncio.to_thread(self.tick, info)
            except Exception as e:           # never let the loop die
                log.debug("watchdog poll: %s", e)
            await asyncio.sleep(INTERVAL)


def enabled() -> bool:
    """On the Pi (MOONRAKER_URL set by the service) unless PCB_MILL_WATCHDOG=0."""
    return bool(os.environ.get("MOONRAKER_URL")) and os.environ.get("PCB_MILL_WATCHDOG", "1") != "0"
