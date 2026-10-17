# Firmware for the Snapmaker Original controller

Two binaries, one flash procedure, and a tested rollback path. Flashing happens
via the Snapmaker's own bootloader — **no JTAG needed, fully reversible.**

## Files

| File | What | SHA-256 |
|---|---|---|
| `v0.13.0-20260104-r510-UART-250000.Bin` | **Flash target.** Mainline Klipper v0.13.0 MCU firmware for the GD32F105, UART @ 250000 baud (matches `klipper/printer.cfg`). From UlinProject's `Klipper_12` zip. | `e08e8ff3…03c1c35` |
| `32Base-2_11Original.Bin` | **Rollback / undo button.** Stock Snapmaker firmware 2.11 (the final release for the Original). Restores touchscreen + Luban workflow. | `8150c1bc…e30f4e` |
| `Klipper_12_v0.13.0_2025.zip` | Source archive with other baud rates (115200–1843200) if ever needed. | `0090ffc2…d469d548` |
| `ulin-*.md` | UlinProject's own docs (flashing, machine tweaks) for reference. | — |

Source: https://github.com/UlinProject/snapmaker-original/tree/main/firmware
Note: this is NOT a "CNC fork" — it's unmodified mainline Klipper compiled for
this MCU. All CNC behavior comes from our `printer.cfg`.

## Flash procedure (same for target and rollback)

1. FAT32 USB stick. Copy the desired `.Bin` onto it **renamed to exactly
   `Update.Bin`** (capital U, capital B).
2. Power the machine **off**. Insert the stick in the controller's USB port.
3. Power on. Watch the controller LED:
   - blue LED blinks slowly ~5 s
   - LED off ~2 s
   - LED blinks fast → **done**
4. **Remove the USB stick.** (Klipper's bootloader does not delete `Update.Bin`;
   leaving it in reflashes on every boot.)
5. First Klipper boot: the red LED on the controller stays lit until klippy
   connects from the Pi, then goes out (that's the "firmware is alive and
   talking" signal).

## Rollback (undo)

Repeat the procedure with `32Base-2_11Original.Bin` as `Update.Bin`.
Machine boots back to stock with touchscreen working. Nothing on the Pi
needs to change; OctoPrint on the same Pi is unaffected either way.

## Before flashing — capture machine state

- Note the stock firmware version shown on the touchscreen (Settings → About)
  as a sanity check that rollback worked later.
- The machine stores no user calibration worth backing up (work origins etc.
  are transient). The PCB-bit length offset etc. live in our docs, not the MCU.

## Known behavior after flashing Klipper

- **Touchscreen goes dark/dead.** It speaks Snapmaker's protocol; nothing
  drives it under Klipper. This is expected; reflashing stock revives it.
- The linear modules, endstops, and LEDs all behave the same electrically —
  only the brains changed.
