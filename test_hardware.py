#!/usr/bin/env python3
"""Interactive hardware test for probe and spindle relay."""

import httpx
import time

MOONRAKER = "http://anycubic-octopi.local:7125"

def query_probe() -> bool:
    """Return True if probe is triggered (bit touching copper).

    NOTE: the probe `last_query` status only refreshes when QUERY_PROBE is
    issued — it is NOT live-polled by the MCU. Always issue QUERY_PROBE first.
    """
    httpx.post(f"{MOONRAKER}/printer/gcode/script", data={"script": "QUERY_PROBE"}, timeout=5).raise_for_status()
    r = httpx.get(f"{MOONRAKER}/printer/objects/query?probe", timeout=5)
    r.raise_for_status()
    return r.json()["result"]["status"]["probe"]["last_query"]

def get_spindle() -> float:
    """Return spindle relay value (0=off, 1=on)."""
    r = httpx.get(f"{MOONRAKER}/printer/objects/query?output_pin%20spindle", timeout=5)
    r.raise_for_status()
    return r.json()["result"]["status"]["output_pin spindle"]["value"]

def send_gcode(cmd: str):
    """Send G-code command to Klipper."""
    r = httpx.post(f"{MOONRAKER}/printer/gcode/script", 
                   data={"script": cmd}, timeout=10)
    r.raise_for_status()
    return r.json()

def test_probe():
    """Test probe by querying its state repeatedly."""
    print("\n=== PROBE TEST ===")
    print("The probe should read OPEN when bit is NOT touching copper.")
    print("Touch the probe clip to the copper (or touch bit to copper) to trigger.\n")
    
    for i in range(10):
        triggered = query_probe()
        status = "TRIGGERED!" if triggered else "open"
        print(f"  [{i+1}/10] Probe: {status}")
        if i < 9:
            time.sleep(0.5)
    
    print("\nIf you saw 'TRIGGERED' when touching, the probe works!")
    print("If probe always reads 'open', check:")
    print("  - Clip 1 is on bit shank")
    print("  - Clip 2 is on copper pour")
    print("  - Door switch is disconnected from enclosure port")

def test_relay():
    """Test spindle relay by toggling it on and off."""
    print("\n=== SPINDLE RELAY TEST ===")
    print("This will toggle the spindle relay. You should hear a click.")
    print("Make sure spindle power is SAFE (unplug from mains if unsure).\n")
    
    resp = input("Toggle relay? [y/N] ")
    if resp.lower() != 'y':
        print("Skipping relay test.")
        return
    
    print("\nTurning relay ON (you should hear a click)...")
    send_gcode("SET_PIN PIN=spindle VALUE=1")
    time.sleep(1)
    val = get_spindle()
    print(f"  Relay value: {val} (expected 1.0)")
    
    resp = input("Did you hear/see the relay click on? [y/N] ")
    
    print("\nTurning relay OFF...")
    send_gcode("SET_PIN PIN=spindle VALUE=0")
    time.sleep(0.5)
    val = get_spindle()
    print(f"  Relay value: {val} (expected 0.0)")
    
    print("\nRelay test complete.")

if __name__ == "__main__":
    print("PCB Mill Hardware Test")
    print("=" * 40)
    
    # Check connectivity
    try:
        r = httpx.get(f"{MOONRAKER}/server/info", timeout=5)
        info = r.json()
        print(f"Connected to Moonraker (API {info['result'].get('api_version_string', 'unknown')})")
    except Exception as e:
        print(f"ERROR: Cannot connect to Moonraker: {e}")
        exit(1)
    
    # Check Klipper state
    r = httpx.get(f"{MOONRAKER}/printer/objects/query?webhooks", timeout=5)
    state = r.json()["result"]["status"]["webhooks"]["state"]
    print(f"Klipper state: {state}")
    
    if state != "ready":
        print("WARNING: Klipper not ready. Some tests may fail.")
    
    # Run tests
    test_probe()
    test_relay()
    
    print("\n" + "=" * 40)
    print("Hardware test complete.")
