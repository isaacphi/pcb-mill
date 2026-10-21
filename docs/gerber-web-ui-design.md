# Web UI for Gerber Upload + Multi-Tool Job Runner

Design for a web interface on the Pi (`anycubic-octopi.local`, see
`octopi-dual-stack.md`) that replaces the laptop-side `mill.py` workflow:
upload Gerbers in a browser, pick your bits and cutting parameters, convert on
the Pi, and run the job on the Snapmaker — with **guided tool changes** (the UI
tells you when to swap bits, and the machine re-zeros Z after each swap).

> **Status: design only.** Nothing here is implemented yet.

---

## 1. Requirements

- Upload Gerber/Excellon files from any browser (copper, edge cuts, drill; top +
  bottom for double-sided).
- Expose cutting parameters at upload time, most importantly **which bits you
  actually have** (diameters, type), plus feeds/speeds/depths overrides.
- Run `mill.py` on the Pi; show conversion output/errors.
- Multi-step jobs where each step may use a different tool. The UI walks you
  through: "install the 0.8 mm end mill" → runs step → "install the 1.0 mm
  drill bit" → re-zero → runs step → …
- Safe by construction: spindle never runs during a prompt; Z is re-established
  after every bit change; emergency stop always reachable.

**v1 scope: single 0.8 mm end mill** (matches `mill_config.yaml` today). The
tool inventory and per-operation parameter model are N-tool from day one —
extending to more bits is data + UI, not a redesign. Machine control
(jog/probe/mesh/pause/estop) stays in Fluidd in v1 and beyond — see below.

> **Update 2026-10-21 — probing moved into the app.** Implemented and tested:
> the job page now walks Convert → Set origin → Probe mesh → Send → Mill.
> Conversion records the board extents (scanned from the generated .nc
> files, which mill.py zero-bases at the board corner); "Set origin" issues
> `G92 X0 Y0` at the bit's position; "Probe board" runs `PCB_MESH` over
> exactly those extents with ~10 mm point spacing (single touch per point —
> measured repeatability ~0.01–0.02 mm). The page polls via htmx
> (`/jobs/{name}/fragment`, vendored `htmx.min.js`, no build step, no CDN).
> Jogging, estop and running the job still live in Fluidd. Because every
> job is surface-meshed, default isolation `cut_depth` dropped 0.2 →
> **0.07 mm** (2× the 35 µm copper; residual mesh error ~±0.03 mm).

## 2. Architecture — hybrid, NOT 100% custom

**Decision (revised after review): build only the conversion/job layer;
machine control stays in Fluidd.** Fluidd already provides jogging, probe
controls, a bed-mesh visualizer, macro buttons (`PCB_MESH`, `TOOL_CHANGE`, …),
pause/resume dialogs, gcode console and estop — all mature. Reimplementing any
of that is pure risk. The custom app exists because nothing else can: upload
Gerbers with cutting parameters, run mill.py, preview toolpaths, push the
result to the machine, and (later) sequence per-tool steps with prompts.

So: not an OctoPrint plugin (OctoPrint belongs to the Anycubic), not a
Moonraker component (can't own this UX), and **not a Fluidd replacement**
(Fluidd keeps driving the machine). The app talks to Klipper **through
Moonraker's REST/WebSocket API**, same as Fluidd does.

```
Browser
   │  http://anycubic-octopi.local:8090
   ▼
┌────────────────────────────────────────────────────────┐
│ pcb-mill-web (FastAPI + vanilla JS or htmx, no build)  │
│  /home/site3/pcb-mill-web, systemd service, user site3 │
│                                                        │
│  • upload + parameter form                             │
│  • tool inventory (YAML)                               │
│  • job runner state machine (one job at a time)        │
│    (phase 3+; v1 just converts + pushes files)         │
└───────┬──────────────────────────┬─────────────────────┘
        │ subprocess               │ REST + WebSocket
        ▼                          ▼
  mill.py / uv env          Moonraker :7125 ──► Klipper ──► Snapmaker MCU
  (existing repo)                │ upload .nc, start print,
                                 │ gcode scripts, status poll
                                 ▼
                    /home/site3/printer_data/gcodes/
```

Why port 8090: 80/443 = OctoPrint's haproxy, 5000 = OctoPrint, 7125 =
Moonraker, 8080 = Fluidd. (Later, haproxy could route `/cnc/` → 8090 so
everything lives on port 80; optional polish.)

### Moonraker API endpoints the app needs

| Purpose | Call |
|---|---|
| Upload a file | `POST /server/files/upload` (multipart, field `file`) |
| (v1 only needs this one — running stays in Fluidd) | |
| Start a print | `POST /printer/print/start?filename=job/step1.nc` |
| Run G-code | `POST /printer/gcode/script` `{ "script": "PCB_MESH X0=.. .." }` |
| Pause/resume/cancel | `POST /printer/print/pause|resume|cancel` |
| Status | WebSocket `printer.objects.subscribe` (print_stats, toolhead, gcode_move) or poll `GET /printer/objects/query` |
| Emergency stop | `POST /printer/emergency_stop` |

All reachable at `http://127.0.0.1:7125` from the app (no auth needed on
localhost if `127.0.0.1` is in `trusted_clients`).

## 3. Tool inventory → mill.py config

The app owns a **tool inventory** (`tools.yaml`) — the physical bits in the
drawer:

```yaml
tools:
  - id: em08
    name: "0.8 mm end mill"
    type: endmill
    diameter: 0.8
    default_for: [isolation, edge, bore]
  - id: em04
    name: "0.4 mm end mill"
    type: endmill
    diameter: 0.4
    default_for: [fine_isolation]
  - id: d10
    name: "1.0 mm drill"
    type: drill
    diameter: 1.0
    default_for: [drill]
spindle:
  max_rpm: 20000          # cosmetic until PWM speed control is wired
machine:
  safe_height: 1.0
  dpi: 1500
```

The upload form lets you, per job:

- pick which operations to run (isolation / drill / edge),
- pick the tool per operation from the inventory,
- override per-tool cutting params — these map **1:1 onto `mill_config.yaml`**
  (`feed_rate`, `plunge_rate`, `cut_depth`, `passes`, `step_over`,
  `isolation_border`, `total_depth`, peck/retract, bore params), with the
  inventory values pre-filled,
- set `--side top|bottom` or double-sided mode.

The app then **renders a `mill_config.yaml`** for the job and calls
`mill.py --config <rendered>` exactly as today (see `mill.py:run_gerber2gcode`).
Conversion stdout/stderr is captured and shown in the job page, followed by a
link to `plot_toolpaths.py` output (run it server-side, show the PNG inline —
it's already the "check before cutting" step).

## 4. Job model and the tool-change flow

### The Z problem (why bit changes need machinery)

Swapping bits changes the tool length, so **Z=0 is lost on every change**, and
after isolation has been milled you can no longer trust a random copper patch
for touch-off (it may be an isolated island). Solution: a **fixed tool-setter
pad** — a small metal pad screwed to the spoilboard at a known XY, wired in
parallel with the probe clips (same PC0 input; it reads TRIGGERED on contact
either way). All re-zeroing happens against the pad, referenced once to the
copper at job start.

Math: at job start (bit A) probe the copper (→ G92 Z0, mesh active as today via
`PCB_MESH`), then probe the pad and record `pad_z_A`. After installing bit B,
probe the pad again → `pad_z_B`. Bit B is `Δ = pad_z_B − pad_z_A` longer in
gcode space, so apply `SET_GCODE_OFFSET Z_ADJUST={-Δ}` and the frame (and mesh)
is correct for bit B without touching the copper.

### New Klipper macros (additions to printer.cfg)

```ini
[gcode_macro PAD_PROBE]
description: Probe the fixed tool-setter pad, report Z
gcode:
  G90
  G0 Z5 F300
  G0 X{pads_x} Y{pads_y} F3000     # pad location, set once
  PROBE
  G0 Z5 F300
  # caller reads printer["probe"].last_z_result

[gcode_macro TOOL_CHANGE]
description: Park for a bit change; spindle off
gcode:
  M5
  G0 Z25 F600                      # high enough to swap bits
  G0 X5 Y110 F3000                 # front corner, reachable collet
  PAUSE                            # Fluidd/app shows paused; user swaps bit

[gcode_macro TOOL_REF]
description: Re-zero Z against the pad after a bit change
  # params: OLD_Z (pad Z with previous bit)
gcode:
  {% set old = params.OLD_Z|float %}
  PAD_PROBE
  {% set new = printer["probe"].last_z_result %}
  SET_GCODE_OFFSET Z_ADJUST={-(new - old)}
  {action_respond_info("Tool offset applied: dz=%.3f" % (new - old))}
```

(Exact macro syntax to be validated against the installed Klipper version;
`last_z_result` and macro variables are standard. `PCB_MESH` from
`remote-cnc-workflow.md` stays as the job-start zero+mesh step.)

### Runner state machine (the app, one job at a time)

```
            ┌────────────────────────────────────────────────────────┐
            │                      JOB PAGE                          │
            └────────────────────────────────────────────────────────┘
 UPLOADED ──► CONVERTING ──► CONVERTED (toolpath PNG shown)
                                 │ "Start job"
                                 ▼
   for each step (per tool):
     PROMPT_TOOL ──"install 0.8 mm end mill, clip probe leads"──► user confirms
                                 ▼
     PROBE_ZERO (step 1: PCB_MESH on copper; later steps: TOOL_REF on pad)
                                 ▼
     READY ── user presses Run (or auto-run if "unattended within step" allowed)
                                 ▼
     RUNNING (Moonraker start; live progress, PAUSE/RESUME/CANCEL/ESTOP)
                                 ▼
     STEP_DONE ──► TOOL_CHANGE (park) ──► next PROMPT_TOOL
                                 │
                                 ▼ (last step)
                              DONE ── CNC_END (spindle off, park, motors off)
```

Every transition requires an explicit user click except while a step is
RUNNING. CANCEL from any state → Moonraker cancel + M5. ESTOP is a big red
button on every screen → `/printer/emergency_stop` (full MCU shutdown,
requires firmware restart — correct behavior for a mill).

Step-to-file mapping: the app uploads one `.nc` per step into Moonraker's
`gcodes/` (e.g. `myboard/01_isolation.nc`, `02_drill.nc`, `03_edge.nc`) and
starts them in order, rather than using the combined `onesided.nc`.

## 5. Toolchain gap: mill.py is currently single-flow (smaller than it looks)

Today `mill.py` emits programs around **one physical tool**. But the gap is
smaller than it first appears:

- `mill_config.yaml` already has **per-operation `tool_diameter`**
  (isolation / back_isolation / edge_cuts / drill sections) — different bits
  per operation are expressible in config today.
- `mill.py` already writes **separate `isolation.nc`, `drill.nc`, `edge.nc`**
  alongside the combined `onesided.nc`. Running them as separate per-tool
  steps needs no gerber2gcode changes — just sequencing + the Z re-reference
  flow from §4.
- What IS missing: no M6/tool-change markers anywhere (fine — steps are
  separate files, changes happen between files), and **drill holes are all
  handled by one bit** (≤ bit+bore_tolerance plunged, larger helically bored —
  `patches/02-bore.patch`). True multi-diameter drilling needs gerber2gcode
  work.

So for multi-tool support:

1. **Per-operation steps (easy, mostly done)**: one `.nc` per operation with
   per-op `tool_diameter` in the rendered config; the runner sequences them
   with TOOL_CHANGE/TOOL_REF between steps. Verify gerber2gcode honors
   per-section diameters in `--separate` output (expected, but test on a real
   conversion).
2. **Drill-bit assignment (harder)**: true multi-diameter drilling — use a real
   1.0 mm drill for 1.0 mm holes instead of boring them with the end mill —
   requires gerber2gcode-level work: group Excellon holes by diameter, map each
   group to the smallest inventory drill ≥ the hole, emit one drill program per
   bit. Until that lands, the UI *displays* the hole-size report (parse the
   Excellon) and notes which will be plunged/bored, so the user chooses bits
   knowingly.

Design the web app against the step model now; the drill-grouping upgrade just
turns one drill step into N drill steps later.

## 6. UI sketch (single page, three tabs)

```
┌─ pcb-mill ──────────────── [ESTOP] ─ status: idle ─┐
│ [New Job] [Current Job] [Tools]                    │
├─────────────────────────────────────────────────────┤
│ New Job:                                           │
│   copper: [B_Cu.gbr ▾]  side: (•) bottom ( ) top   │
│   edge:   [Edge_Cuts.gbr]  drill: [board.drl]      │
│                                                    │
│   Operation    Tool            Feed  Plunge Depth… │
│   ☑ isolation  [0.4 mm EM ▾]   250    80    0.2    │
│   ☑ drill      [0.8 mm EM ▾]   100    50    1.9    │
│   ☑ edge cut   [0.8 mm EM ▾]   220    45    1.6    │
│   holes found: 12× 0.9, 4× 1.0, 2× 3.2 (will bore) │
│                          [Convert]                 │
├─────────────────────────────────────────────────────┤
│ Current Job: myboard   ▓▓▓▓▓░░░░ 52%  (step 2/3)  │
│   ✓ 01_isolation (0.4 mm EM)                       │
│   ▶ 02_drill     (0.8 mm EM)  [Pause] [Cancel]     │
│   • 03_edge                                        │
│   ── next: "Install 0.8 mm end mill" ──            │
│   [Jog] [Probe pad] [Run next step]                │
├─────────────────────────────────────────────────────┤
│ Tools: 0.8 EM (default iso/edge/bore), 1.0 drill…  │
│   pad location: X__ Y__   [+ add tool]             │
└─────────────────────────────────────────────────────┘
```

Keep the frontend dependency-free (server-rendered Jinja + meta-refresh
polling; no JS build) — the Pi 3B serves it fine. The job page links/embeds
Fluidd (:8080) for the machine-control half instead of duplicating it.

## 7. Server pieces to build

| Piece | Notes |
|---|---|
| `web/` FastAPI app in this repo | runs anywhere mill.py runs (dev on macOS, prod on the Pi); uvicorn on `0.0.0.0:8090` |
| systemd unit `pcb-mill-web.service` | `User=site3`, `After=moonraker.service`; autorestart (Pi deploy only) |
| Upload handling | validate extensions (.gbr/.drl/.gm1…), store per job dir `web_jobs/<id>/` |
| Excellon hole report | parse drill file at upload → hole-size histogram + plunge/bore classification |
| Excellon merge | optional: merge separate PTH/NPTH files (KiCad splits them) into the one file mill.py wants |
| Config renderer | tools.yaml + form overrides → job `mill_config.yaml` (deep merge onto repo defaults) |
| Moonraker push | `POST /server/files/upload`; only active when `MOONRAKER_URL` is set (off on the dev Mac) |
| Runner (phase 3+) | asyncio state machine; Moonraker status via WebSocket |
| Auth | LAN-trusted like Moonraker today; add a shared-token header later if exposed beyond LAN |
| State persistence | per-job `status.json` (conversion state now; step index when the runner lands) |

**Deployment note:** `pyproject.toml` has `requires-python >= 3.12` but the Pi
has system Python 3.11 — run via `uv run` so uv fetches a managed 3.12
(python-build-standalone ships armv7l builds; verify on the Pi, fall back to
relaxing the pin to 3.11 if the deps allow).

## 8. Implementation phases

1. **Phase 0 — host setup**: `octopi-dual-stack.md` steps 0–3 (serial by-id,
   KIAUH install, ports). Physical: tool-setter pad wired in parallel with
   probe clips.
2. **Phase 1 — convert-only UI, single 0.8 mm bit** (v1): upload + params +
   run mill.py + toolpath PNG + push to Moonraker. All machine control in
   Fluidd. Tool inventory/params modeled N-tool from the start. (Delivers 80%
   of the convenience.)
3. **Phase 2 — single-tool runner**: start/pause/cancel from the app, progress
   via WebSocket, ESTOP button. Still one tool per job.
4. **Phase 3 — tool changes**: mill.py per-step output, PAD_PROBE/TOOL_CHANGE/
   TOOL_REF macros, prompt flow, Z re-referencing via the pad. This is the
   "told when to change bits" milestone.
5. **Phase 4 — multi-diameter drilling**: gerber2gcode hole grouping → one
   drill step per bit size; UI hole report drives bit selection.
6. **Phase 5 — polish**: job history (Moonraker `[history]` or app-side),
   haproxy `/cnc/` route, optional push notification (Moonraker
   `[notifier]` → e.g. ntfy/Telegram) so bit-change prompts reach your phone.

## 9. Safety invariants (encode in the runner, not in docs)

- Spindle is OFF in every PROMPT/TOOL_CHANGE state; the app checks
  `output_pin spindle` state before enabling "continue".
- Probe clips on ⇒ spindle start refused; app prompts "remove clips" after any
  probe step (mirrors the PCB_MESH warning).
- No step runs unless Z was (re-)established in this tool's lifetime:
  step 1 requires PCB_MESH; steps ≥2 require TOOL_REF since the last change.
- Every gcode script call has a timeout and the runner watches for Klipper
  `shutdown`/`error` states → immediately show + require acknowledgment.
- ESTOP available on every page; also document the physical power switch as
  the true last resort.
