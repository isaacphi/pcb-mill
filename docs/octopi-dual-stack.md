# Running the CNC Stack Alongside OctoPrint on anycubic-octopi

Plan for hosting both the Anycubic 3D printer (OctoPrint) and the Snapmaker CNC
(Klipper/Moonraker/Fluidd) on the **same Raspberry Pi 3B** at
`anycubic-octopi.local` (192.168.1.198).

> **Status: documented, not yet executed.** A print was running at survey time;
> apply this when the printer is idle.

This supersedes the dedicated-Pi assumption in `remote-cnc-workflow.md` for the
host side. The Snapmaker-side steps there (flashing Klipper to the board, wiring
the spindle relay and probe, printer.cfg contents) are unchanged.

---

## Survey findings (2026-10, read-only)

| Item | Finding |
|---|---|
| Hardware | Pi 3 Model B Rev 1.2, quad-core, 869Mi RAM (660Mi available with OctoPrint running) |
| OS | Raspbian/Debian 12 (bookworm), armv7l 32-bit, kernel 6.6.51+rpt |
| Disk | 28G SD, 24G free |
| Python | 3.11.2 (Klipper needs ≥3.7 ✓); git, gcc, make, python3-venv present |
| OctoPrint | venv at `/opt/octopi/oprint`, service `octoprint.service`, listening on `127.0.0.1:5000` |
| Front proxy | **haproxy** on ports 80/443 → OctoPrint; also routes `/webcam/` to mjpg-streamer |
| Anycubic serial | CP2102 USB-UART at `/dev/ttyUSB0`, by-id: `usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`, baud 250000 |
| OctoPrint serial config | `~/.octoprint/config.yaml`: `port: /dev/ttyUSB0`, `autoconnect: true` ⚠️ see below |
| Linux user / SSH | `site3` (sudo requires password; works) |
| OctoPrint web login | `admin` (separate from Linux account) |
| Webcam stack | `mjpg-streamer` installed; **not needed** for either machine |
| Load | OctoPrint used ~3h CPU in 6 days uptime; plenty of headroom for Klipper |

Verdict: Pi 3B can run both stacks, even concurrently, with no webcam. Klipper's
host process is lightweight; Moonraker+Fluidd are modest. A webcam on OctoPrint
*plus* a Klipper job would be the thing to avoid — and we don't need webcams.

---

## Step 0 — Fix OctoPrint's serial port first (before plugging in the Snapmaker)

OctoPrint currently connects via `/dev/ttyUSB0`. USB serial enumeration order is
**not stable**: once the Snapmaker's USB adapter is plugged in, the Anycubic
could come up as `ttyUSB1` and OctoPrint would happily connect to the CNC (or
fail). Both machines must use `/dev/serial/by-id/` paths, which are tied to the
adapter's USB identity, not plug order.

Edit `~site3/.octoprint/config.yaml`:

```yaml
serial:
  autoconnect: true
  baudrate: 250000
  port: /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
  additionalPorts:
  - /dev/serial/by-id/*
```

Restart OctoPrint (`sudo systemctl restart octoprint`) and confirm the printer
reconnects. Do this while the printer is idle.

In Klipper's `printer.cfg` for the Snapmaker, likewise use the Snapmaker
adapter's by-id path (check `ls /dev/serial/by-id/` after first plugging it in):

```ini
[mcu]
serial: /dev/serial/by-id/usb-<snapmaker-adapter-id>
baud: 460800
restart_method: command
```

## Step 1 — Disable the webcam stack (optional, frees resources)

```bash
sudo systemctl disable --now webcamd
```

haproxy keeps its `/webcam/` routes; they'll just 503. Harmless.

## Step 2 — Install Klipper + Moonraker + Fluidd via KIAUH

As `site3`:

```bash
cd ~
git clone https://github.com/dw-0/kiauh
./kiauh/kiauh.sh
```

Install **one instance each** of Klipper, Moonraker, Fluidd. KIAUH won't touch
the `octoprint` service — OctoPi is not a Klipper install, so KIAUH sees a
clean slate.

**Critical: ports.** OctoPrint's haproxy owns 80/443; OctoPrint itself owns
127.0.0.1:5000. Assign:

| Service | Port | Notes |
|---|---|---|
| haproxy → OctoPrint | 80/443 | untouched |
| OctoPrint | 127.0.0.1:5000 | untouched |
| **Fluidd (nginx)** | **8080** | KIAUH asks for the port during Fluidd install; **do not accept the default 80** |
| **Moonraker API** | **7125** | default, no conflict |
| Klipper (Unix socket) | `/home/site3/printer_data/comms/klippy.sock` | no TCP port |

When done:

- `http://anycubic-octopi.local/` → OctoPrint (Anycubic)
- `http://anycubic-octopi.local:8080/` → Fluidd (Snapmaker CNC)

### Moonraker config

`~/printer_data/config/moonraker.conf` — same as `remote-cnc-workflow.md` §2.5,
plus trust the LAN:

```ini
[server]
host: 0.0.0.0
port: 7125

[authorization]
trusted_clients:
  192.168.1.0/24

[file_manager]
queue_gcode_uploads: False

[history]
```

### Klipper config

`~/printer_data/config/printer.cfg` — use the full CNC config from
`remote-cnc-workflow.md` §2.4, with the `[mcu] serial:` pointing at the
Snapmaker's by-id path (Step 0).

## Step 3 — Resource sanity checks after install

```bash
free -h                    # expect >300Mi available at idle
systemctl status klipper moonraker octoprint --no-pager
ss -tlnp                   # 80/443 haproxy, 5000 octoprint, 7125 moonraker, 8080 nginx
```

Operational rule of thumb for the 3B: printing on the Anycubic **and** milling
at the same time should work (both protocols buffer ahead), but if Fluidd ever
shows G-code stutter during simultaneous use, schedule jobs sequentially. If RAM
gets tight later, Mainsail-Lite-style trimming or swapping the A+ out for this
Pi's heavier sibling (Pi 4/5) is the escape hatch.

## Step 4 — Snapmaker-side work (unchanged, physical)

From `remote-cnc-workflow.md`: flash Klipper to the Snapmaker board (Part 1),
wire the spindle relay and probe clips (Part 3), then verify endstops/probe and run
the PCB_MESH flow (Part 5). The gerber→G-code conversion moves to the web UI —
see `gerber-web-ui-design.md` — so `convert.sh`/Samba (Part 4) is superseded.

## Step 5 — pcb-mill web app (port 8081)

Installed 2026-10-18. Lessons baked in from the first attempt (an uncapped
1500-dpi conversion swap-thrashed the Pi, froze it off the network for
~7 min, and stalled an OctoPrint job mid-print):

**Python deps** — Python is 3.11 and piwheels has NO scikit-image wheel for
cp311/armv7 (pip tries to build from source — hours). Use Debian's packages
for the scientific stack and a `--system-site-packages` venv for the rest.
Do NOT pip-install numpy 2.x into the venv — it shadows apt's numpy 1.24 and
breaks skimage's ABI.

```bash
sudo apt-get install -y python3-skimage python3-scipy python3-numpy \
                        python3-matplotlib python3-pil
rsync -az --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude web_jobs --exclude out --exclude board \
  ./ site3@anycubic-octopi.local:pcb-mill/
ssh site3@anycubic-octopi.local
cd ~/pcb-mill && python3 -m venv --system-site-packages .venv
.venv/bin/pip install fastapi uvicorn httpx jinja2 python-multipart pyyaml pygerber
# note: plain uvicorn, NOT uvicorn[standard] (uvloop won't build on armv7)
```

**Service** — `deploy/pcb-mill.service` → `/etc/systemd/system/`, then
`sudo systemctl enable --now pcb-mill`. The unit carries the coexistence
guardrails: `Nice=19`, `IOSchedulingClass=idle`, **`MemoryMax=450M`** (a
runaway conversion is OOM-killed inside its own cgroup; the rest of the
system, including an OctoPrint print, is unaffected), and
`PCB_MILL_DPI=1000` — 1500-dpi bitmaps of a ~50x66mm board need ~500MB+ of
RAM (RGBA render + float64 EDT transforms); 1000 dpi ≈ 25.4µm/px is ample
for 0.8 mm tooling. Manual starts: `deploy/pi-start.sh` applies the same
guards via `nice`/`prlimit`/env.

App runs at `http://anycubic-octopi.local:8081/` and talks to the local
Moonraker (`MOONRAKER_URL=http://127.0.0.1:7125`). Conversion of the AQI
test board (1000 dpi) is expected ~5–15 min on the 3B.

**File handoff**: "Send to machine" writes the .nc files straight into
`~/printer_data/gcodes/pcb-mill/<job>/` (Moonraker's watcher makes them
appear in Fluidd instantly — no HTTP upload needed when the app runs on the
Pi; the Moonraker API is the fallback for running the app on a laptop).
Override the location with `GCODE_DIR`, the Fluidd link with `FLUIDD_URL`.

## Rollback

Klipper stack removal is clean and doesn't affect OctoPrint:

```bash
./kiauh/kiauh.sh   # Advanced → Remove
# or: sudo systemctl disable --now klipper moonraker nginx
```

OctoPrint, haproxy, and the Anycubic are untouched throughout.
