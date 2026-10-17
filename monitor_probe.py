#!/usr/bin/env python3
"""Continuous probe monitor."""
import httpx
import time

MOONRAKER = "http://anycubic-octopi.local:7125"

print("=== PROBE MONITOR ===")
print("Touch probe clips together (bit to copper) to test.")
print("Press Ctrl+C to stop.\n")

try:
    for i in range(30):  # 15 seconds
        # last_query only refreshes when QUERY_PROBE is issued!
        httpx.post(f"{MOONRAKER}/printer/gcode/script", data={"script": "QUERY_PROBE"}, timeout=2)
        r = httpx.get(f"{MOONRAKER}/printer/objects/query?probe", timeout=2)
        triggered = r.json()["result"]["status"]["probe"]["last_query"]
        status = "🔴 TRIGGERED!" if triggered else "⚪ open"
        print(f"  [{i+1:2d}] {status}", end="\r", flush=True)
        time.sleep(0.5)
except KeyboardInterrupt:
    print("\n\nStopped.")
