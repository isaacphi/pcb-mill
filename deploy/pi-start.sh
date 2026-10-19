#!/bin/bash
# Start the pcb-mill web app on the Pi (port 8081, next to Fluidd on 8080).
#
# Pi 3B coexistence rules (learned the hard way 2026-10-18 — an uncapped
# conversion swap-thrashed the box and stalled an OctoPrint job):
#   nice -n 19        CPU yields to OctoPrint
#   prlimit --as=800M last-ditch address-space cap (systemd's MemoryMax is the
#                     real guard; this covers manual/nohup starts)
#   PCB_MILL_DPI=1000 1500-dpi bitmaps OOM 869MB RAM; 1000dpi = 25.4um/px,
#                     ample for 0.8mm tooling (vendor default is 1000 too)
cd "$(dirname "$0")/.."
exec prlimit --as=800M -- nice -n 19 env \
  MOONRAKER_URL=http://127.0.0.1:7125 \
  PCB_MILL_DPI=1000 \
  .venv/bin/python -m uvicorn web.app:app --host 0.0.0.0 --port 8081
