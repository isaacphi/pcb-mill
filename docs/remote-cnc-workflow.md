# Snapmaker CNC Remote Workflow with Klipper

Complete setup for running your Snapmaker Original as a dedicated CNC router with remote job control via web interface.

## Overview

This replaces the Snapmaker's proprietary controller with Klipper firmware + Raspberry Pi host, giving you:

- **Smooth arcs** (configurable resolution vs stock 1mm segments)
- **Remote job control** from any browser
- **File upload** via web interface
- **Spindle control** (on/off, optional speed)
- **No more SD cards** or USB transfers

```
Your Laptop/Phone
        ↓ (WiFi/Ethernet)
   ┌─────────────────────────────────────────┐
   │           Raspberry Pi A+               │
   │  ┌─────────┐  ┌──────────┐  ┌────────┐ │
   │  │  Fluidd │→ │ Moonraker│→ │Klipper │ │
   │  │ (Web UI)│  │  (API)   │  │(host)  │ │
   │  └─────────┘  └──────────┘  └────┬───┘ │
   │                                   │    │
   │  ┌────────────────────────────────┘    │
   │  │  G-code files, conversion scripts   │
   │  └─────────────────────────────────────┘
   └────────────────┬────────────────────────┘
                    │ UART/USB
        ┌───────────▼───────────┐
        │   Snapmaker Board     │
        │   (GD32F105 + A4988)  │
        └───────────┬───────────┘
                    │ RJ45 cables
        ┌───────────▼───────────┐
        │   Linear Modules      │
        │   (X, Y, Z steppers)  │
        └───────────────────────┘
                    │
        ┌───────────▼───────────┐
        │   300W ER11 Spindle   │
        │   (48V + SSR control) │
        └───────────────────────┘
```

---

## Hardware Requirements

### You Have
- [ ] Snapmaker Original (3-in-1)
- [ ] Raspberry Pi Model A+ (2014, 512MB RAM)
- [ ] 300W ER11 spindle with 48V supply
- [ ] MicroSD card (8GB+)

### You Need
| Item | Cost | Purpose |
|------|------|---------|
| SSR-40DA or similar | ~$10 | Solid state relay for spindle on/off |
| 5V relay module (optional) | ~$5 | If using opto-isolated relay instead of SSR |
| Dupont wires | ~$5 | Connections between Pi and Snapmaker board |
| USB cable (A to micro-B) | ~$5 | Alternative to UART wiring |
| 8GB+ microSD | ~$8 | Pi OS (have?) |

**About the Pi A+**: It has limited RAM (512MB) and single-core CPU. Fluidd will work but may feel sluggish. If it's too slow, consider:
- Pi 3A+ or 3B+ (~$25-35 used)
- Pi Zero 2W (~$15, same performance as 3A+)
- Keep A+ but use lighter web UI (Mainsail Lite?)

---

## Part 1: Flash Klipper to Snapmaker Board

### 1.1 Download Firmware

Use UlinProject's pre-built Klipper (GD32F105 = STM32F103 compatible):

```bash
# On your laptop
curl -LO https://github.com/UlinProject/snapmaker-original/raw/main/firmware/Klipper_6_2025Demo460800.Bin
mv Klipper_6_2025Demo460800.Bin Update.Bin
```

### 1.2 Flash via USB Stick

1. Copy `Update.Bin` to FAT32-formatted USB stick
2. Power off Snapmaker
3. Insert USB stick
4. Power on
5. Watch LED sequence:
   - Blue LED blinks slowly (~5s)
   - LED off (~2s)
   - LED blinks rapidly → flashing in progress
   - LED steady or off → complete
6. **Remove USB stick immediately** (or it will re-flash every boot)

### 1.3 Verify

Connect via serial (115200 baud) to check:
```bash
screen /dev/ttyUSB0 115200
# or
picocom -b 115200 /dev/ttyUSB0
```

Type `status` — should show Klipper version.

---

## Part 2: Raspberry Pi Setup

### 2.1 Install OS

Use **Raspberry Pi OS Lite** (64-bit if A+ supports, otherwise 32-bit).

**Note**: Pi A+ is ARMv6 (older architecture). Check if 64-bit is supported:
```bash
uname -m
# armv6l = 32-bit only
# aarch64 = 64-bit capable
```

If armv6l, you'll need **Raspberry Pi OS (Legacy, 32-bit)** or **DietPi** (lighter).

### 2.2 Install Klipper Stack

SSH into Pi:
```bash
ssh pi@raspberrypi.local
```

Use KIAUH (Klipper Install And Update Helper):
```bash
cd ~
git clone https://github.com/dw-0/kiauh
./kiauh/kiauh.sh
```

Select:
- **1) Install**
- **Klipper** → Install
- **Moonraker** → Install  
- **Fluidd** → Install (or Mainsail)

### 2.3 Connect Pi to Snapmaker

**Option A: USB (Simplest)**
```bash
# Pi USB port → Snapmaker USB port
ls /dev/ttyUSB*  # should show /dev/ttyUSB0
```

**Option B: Direct UART (Frees USB port)**

Pi GPIO → Snapmaker board:
| Pi Pin | Snapmaker Pin | Function |
|--------|---------------|----------|
| GPIO 14 (TXD) | PA10 (RXD) | Serial TX |
| GPIO 15 (RXD) | PA9 (TXD) | Serial RX |
| GND | GND | Ground |

Edit `/boot/firmware/config.txt` (or `/boot/config.txt` on older OS):
```ini
enable_uart=1
dtoverlay=disable-bt  # if using GPIO serial
```

Disable serial console:
```bash
sudo raspi-config
# Interface Options → Serial Port → No to login shell, Yes to serial hardware
```

### 2.4 Klipper Configuration

Create `/home/pi/printer_data/config/printer.cfg`:

```ini
# Snapmaker Original CNC Configuration
# Based on UlinProject pin mappings

[mcu]
serial: /dev/ttyUSB0  # or /dev/ttyAMA0 if UART
baud: 460800
restart_method: command

[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3600
max_z_velocity: 20
max_z_accel: 450
square_corner_velocity: 8.0

# X-axis (channel 1 on Snapmaker board)
[stepper_x]
step_pin: PB5
dir_pin: !PB6
enable_pin: !PB7
microsteps: 16
rotation_distance: 8
endstop_pin: ^!PC11
position_endstop: -4
position_min: -4
position_max: 120
homing_speed: 12

# Y-axis (channel 2)
[stepper_y]
step_pin: PB8
dir_pin: !PB9
enable_pin: !PB4
microsteps: 16
rotation_distance: 8
endstop_pin: ^!PC10
position_endstop: -4
position_min: -4
position_max: 120
homing_speed: 12

# Z-axis (channel 3)
[stepper_z]
step_pin: PB3
dir_pin: !PD2
enable_pin: !PC12
microsteps: 16
rotation_distance: 8
endstop_pin: ^!PA8
position_endstop: -3.25
position_min: -3.25
position_max: 125.5
homing_speed: 9

# Spindle control - on/off via SSR
[output_pin spindle]
pin: PB10  # Was heater pin, now spindle SSR
pwm: False
value: 0
shutdown_value: 0

# Optional: spindle speed PWM (if you add PWM-to-voltage converter)
# [output_pin spindle_speed]
# pin: PA7  # Was bed heater, now PWM out
# pwm: True
# cycle_time: 0.001  # 1kHz PWM
# value: 0

# Arc resolution - key for smooth circles!
[gcode_arcs]
resolution: 0.5  # mm max segment length (default 1.0)
# Lower = smoother circles, but more segments

# Optional: input shaping for reduced vibration
# [input_shaper]
# shaper_freq_x: 20
# shaper_freq_y: 40
# shaper_type_x: mzv
# shaper_type_y: mzv

[safe_z_home]
home_xy_position: 60, 60
z_hop: 5

# LED control (if you want status lights)
[output_pin LED_R]
pin: PC7
value: 0

[output_pin LED_B]
pin: PC8
value: 0

[output_pin GATE_CTRL]
pin: PB0
value: 1

[output_pin MAIN_POWER]
pin: PF2
value: 1

[output_pin STEPPER_POWER]
pin: PF1
value: 1

# Idle timeout - disable steppers after 5 min
[idle_timeout]
timeout: 300

[virtual_sdcard]
path: /home/pi/printer_data/gcodes

[force_move]
enable_force_move: 1

# Pause/resume for tool changes
[pause_resume]

# Display status
[display_status]

# Macros for CNC workflow
[gcode_macro M3]
description: Spindle on
gcode:
  SET_PIN PIN=spindle VALUE=1
  G4 P1000  # Wait 1s for spin-up

[gcode_macro M5]
description: Spindle off
gcode:
  SET_PIN PIN=spindle VALUE=0
  G4 P1000  # Wait 1s for spin-down

[gcode_macro CNC_START]
description: Home and start job
gcode:
  G28  # Home all
  G90  # Absolute positioning
  G21  # Millimeters

[gcode_macro CNC_END]
description: Safe park after job
gcode:
  M5   # Spindle off
  G0 Z10 F3000  # Raise Z
  G0 X0 Y0 F3000  # Return to origin
  M84  # Motors off

[gcode_macro PROBE_Z]
description: Z probe (if you add touch probe)
gcode:
  # Configure [probe] section first
  # G38.2 Z-10 F100  # Probe down
  # G92 Z0  # Set Z zero
  {action_respond_info("Manual probe - not implemented")}

[display_status]
```

### 2.5 Moonraker Configuration

Edit `/home/pi/printer_data/config/moonraker.conf`:

```ini
[server]
host: 0.0.0.0
port: 7125

[file_manager]
enable_inotify_warnings: True
enable_inotify_events: True
queue_gcode_uploads: False
# Upload .nc files directly - they'll appear in Fluidd

[authorization]
trusted_clients:
  192.168.1.0/24  # Your local network
  10.0.0.0/24     # VPN if applicable

[octoprint_compat]
# For compatibility with OctoPrint-compatible tools
enable_bed_adjust: True

[history]
```

Restart services:
```bash
sudo systemctl restart klipper moonraker
```

---

## Part 3: Spindle Wiring

### 3.1 SSR for On/Off Control

Wire SSR between 48V supply and spindle:

```
48V Supply (+) ────────────────────→ SSR INPUT (+) (DC side, 3-32V)
                                      │
Snapmaker Board PB10 ────────────────→ SSR INPUT (+)
Snapmaker Board GND ─────────────────→ SSR INPUT (-)
                                      │
48V Supply (-) ────────────────────→ SSR OUTPUT (-) (AC/DC load side)
Spindle 48V+ ──────────────────────→ SSR OUTPUT (+)
Spindle 48V- ──────────────────────→ 48V Supply (-)
```

**Note**: SSR-40DA handles DC input, AC output. For DC spindle, you need a **DC-to-DC SSR** or use a mechanical relay instead.

**Better option for DC spindle**: Use a **MOSFET module** (e.g., IRF520 or logic-level MOSFET):
```
48V Supply (+) → MOSFET Source
MOSFET Drain → Spindle (+)
MOSFET Gate → PB10 (via 10kΩ resistor to GND)
48V Supply (-) → Spindle (-) → GND
```

### 3.2 Optional: Speed Control

If you want to replace the manual potentiometer with PWM:

**PWM-to-0-5V Converter Module** (~$10):
```
PA7 (PWM out) → Converter VCC
Converter VCC → 5V
Converter GND → GND
Converter OUT → Spindle speed control input
```

Config:
```yaml
[output_pin spindle_speed]
pin: PA7
pwm: True
cycle_time: 0.001
value: 0

[gcode_macro M3]
gcode:
  SET_PIN PIN=spindle VALUE=1
  SET_PIN PIN=spindle_speed VALUE={params.get('S', 1.0)}
```

---

## Part 4: Gerber-to-G-code on Pi

### 4.1 Install Dependencies

```bash
cd ~
git clone https://github.com/yourusername/pcb-mill.git
cd pcb-mill

# Python 3.9+ required
python3 -m venv .venv
source .venv/bin/activate
pip install gerber2gcode  # or use fetch script
```

Or copy from laptop:
```bash
# On laptop
scp -r /Users/phil/dev/pcb-mill pi@raspberrypi.local:~/
```

### 4.2 Setup Conversion Service

Create `/home/pi/pcb-mill/convert.sh`:

```bash
#!/bin/bash
# Convert gerber files to G-code
# Usage: ./convert.sh <name> <copper.gbr> <edge.gbr> <drill.drl>

NAME=$1
COPPER=$2
EDGE=$3
DRILL=$4

OUTDIR=/home/pi/printer_data/gcodes
WORKDIR=/tmp/gerber_job_$$

mkdir -p $WORKDIR

# Run mill.py
python3 /home/pi/pcb-mill/mill.py \
  --copper "$COPPER" \
  --edge "$EDGE" \
  --drill "$DRILL" \
  -o "$OUTDIR/$NAME.nc"

# Cleanup
rm -rf $WORKDIR

echo "Created: $OUTDIR/$NAME.nc"
```

Make executable:
```bash
chmod +x /home/pi/pcb-mill/convert.sh
```

### 4.3 Samba Share (Optional)

For easy file access from laptop:

```bash
sudo apt install samba
```

Edit `/etc/samba/smb.conf`:
```ini
[gcodes]
path = /home/pi/printer_data/gcodes
browseable = yes
read only = no
guest ok = no
valid users = pi
```

Restart:
```bash
sudo systemctl restart smbd
```

Access from Mac: `⌘+K` → `smb://raspberrypi.local/gcodes`

---

## Part 5: Daily Workflow

### Option A: Laptop → Pi (Current, Simple)

```
Laptop:
  mill.py → board.nc
  drag board.nc → Fluidd upload area
  click Start
```

### Option B: Samba Share (Seamless)

```
Laptop:
  mill.py → board.nc
  copy to \\raspberrypi.local\gcodes\
  (appears instantly in Fluidd)
  click Start
```

### Option C: Run mill.py on Pi (Most Integrated)

SSH into Pi, or use web terminal in Fluidd:
```bash
cd ~/pcb-mill
./convert.sh myboard \
  /home/pi/gerbers/myboard/B_Cu.gbr \
  /home/pi/gerbers/myboard/Edge_Cuts.gbr \
  /home/pi/gerbers/myboard/myboard.drl
```

Then refresh Fluidd, file appears.

### Option D: Full Auto-Convert (Advanced)

Add Moonraker file processor:

Edit `moonraker.conf`:
```ini
[file_manager]
enable_observer_post_processors: True
post_processor: /home/pi/pcb-mill/auto_convert.py
```

Create `/home/pi/pcb-mill/auto_convert.py`:

```python
#!/usr/bin/env python3
"""Auto-convert Gerber files when uploaded to Moonraker"""

import sys
import subprocess
from pathlib import Path

def main():
    filepath = Path(sys.argv[1])
    
    if filepath.suffix != '.gbr':
        return
    
    # Look for matching drill and edge files
    base = filepath.stem
    drill = filepath.parent / f"{base}.drl"
    edge = filepath.parent / f"{base}_Edge_Cuts.gbr"
    
    if not all([drill.exists(), edge.exists()]):
        print(f"Missing files for {base}")
        return
    
    # Convert
    out = filepath.parent / f"{base}.nc"
    subprocess.run([
        'python3', '/home/pi/pcb-mill/mill.py',
        '--copper', str(filepath),
        '--drill', str(drill),
        '--edge', str(edge),
        '-o', str(out)
    ])
    
    print(f"Created: {out}")

if __name__ == '__main__':
    main()
```

---

## Troubleshooting

### Pi A+ Too Slow

| Issue | Fix |
|-------|-----|
| Fluidd sluggish | Use Mainsail Lite, or upgrade to Pi 3A+ |
| G-code stutter | Reduce `resolution` to 0.8mm, disable webcam |
| Upload slow | Use wired Ethernet (USB adapter), not WiFi |

### Klipper Not Connecting

```bash
# Check MCU firmware
screen /dev/ttyUSB0 115200
# Type 'status' - should respond

# Check Klipper log
tail -f /tmp/klippy.log

# Common fix: restart method
[mcu]
restart_method: command  # or 'rpi' if direct GPIO
```

### Spindle Not Responding

```bash
# Test SSR manually
SET_PIN PIN=spindle VALUE=1  # Should click/LED
SET_PIN PIN=spindle VALUE=0  # Should release

# Check voltage at SSR input
# Should be ~3.3V when VALUE=1
```

### Circles Still Angular

Increase arc resolution:
```yaml
[gcode_arcs]
resolution: 0.2  # 0.2mm segments = very smooth
```

Or pre-segment in `mill.py` output (modify gerber2gcode to emit G1 instead of G2/G3).

---

## Quick Reference

| Task | Command |
|------|---------|
| Access web UI | `http://raspberrypi.local` |
| SSH to Pi | `ssh pi@raspberrypi.local` |
| Restart Klipper | `sudo systemctl restart klipper` |
| View Klipper log | `tail -f /tmp/klippy.log` |
| View Moonraker log | `tail -f /tmp/moonraker.log` |
| Convert gerbers | `~/pcb-mill/convert.sh name copper.gbr edge.gbr drill.drl` |
| Emergency stop | Click "Emergency Stop" in Fluidd |
| Spindle on | `M3` or macro button |
| Spindle off | `M5` or macro button |

---

## Next Steps

1. [ ] Flash Klipper to Snapmaker board
2. [ ] Set up Pi with Klipper/Moonraker/Fluidd
3. [ ] Wire SSR for spindle control
4. [ ] Test basic motion (G0, G28, M3/M5)
5. [ ] Run test G-code file
6. [ ] Set up Samba share (optional)
7. [ ] Add auto-conversion (optional)

Questions or issues? Check Klipper Discord or UlinProject repo.