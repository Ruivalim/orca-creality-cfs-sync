# Creality CFS Sync

OrcaSlicer plugin that makes Orca's own Moonraker filament sync see the Creality CFS
(the multi-material unit of the Creality Hi).

## The problem

Orca's filament sync for Moonraker printers reads the `lane_data` namespace of the
Moonraker database. That is the format AFC and Happy Hare publish. Creality's Klipper
keeps the CFS state in a `box` object instead and never writes lane data, so on a
Creality Hi the sync button finds nothing.

## What the plugin does

It reads `box` from the printer and writes one `lane_data` entry per loaded CFS slot,
with the material and the colour. Orca's sync button then works unchanged, exactly as
it does for an AFC printer.

- Syncs as soon as you select the printer in Orca, then refreshes every 15 seconds.
- Writes only what changed, and removes the entry of a slot you emptied.
- Owns only the keys it created (prefix `cfs_`). Entries from other tools are left alone.
- Does nothing on a printer without a `box` object, so it is safe to keep enabled.

## Install

You need an OrcaSlicer with the plugin system: 2.5.0 alpha or a nightly build.

- **Orca Cloud Plugin Hub:** subscribe to *Creality CFS Sync*, then in OrcaSlicer open
  *File > Plugins*, click *Refresh* and activate it.
- **Local file:** download `creality_cfs_sync_any.py` from the latest release, then in
  *File > Plugins* open the *Browse plugins* dropdown and pick *Install local plugin*.

## Use

1. Add the printer to Orca as a Moonraker/Klipper device, with the same host and port you
   open in the browser for Fluidd. On a Creality Hi that is `http://<printer-ip>:4408`.
2. Select the printer in the *Device* tab. Orca asks for two permissions, network access
   to the printer and a background thread. Allow both. Orca remembers the answer.
3. In the filament panel, click the sync button. The loaded CFS slots show up with their
   material and colour.

To force a sync, open *File > Plugins*, select *Creality CFS Sync* and click *Run*.

### Settings

The capability's settings in *File > Plugins*:

| Key                | Default | Meaning                                                                    |
| ------------------ | ------- | -------------------------------------------------------------------------- |
| `host`             | empty   | `http://host:port` of the printer. Filled in when you select the device.   |
| `interval_seconds` | `15`    | Seconds between refreshes while the printer stays selected. Minimum 5.     |

### What gets written

One entry per loaded slot, in the `lane_data` namespace of the Moonraker database:

| Key        | Value                                                   |
| ---------- | ------------------------------------------------------- |
| `cfs_T1A`  | `{"lane": "0", "material": "PLA", "color": "#3DDF57"}`  |
| `cfs_T1B`  | `{"lane": "1", "material": "PETG", "color": "#FFFFFF"}` |

`T1` to `T4` are the CFS units, `A` to `D` the slots, and `lane` counts from 0 across
units. Empty slots, disconnected units and unknown material ids are skipped, so Orca
shows them as empty. The known material ids are PLA, ABS, PETG, TPU, ASA, PA, PC, PLA-CF,
PETG-CF and PA-CF.

## Tested on

A Creality Hi with one CFS unit, against an OrcaSlicer nightly from October 2026. The
K2 Plus and the K1 series with a CFS report the same `box` object, but I have not tested
them. Reports welcome.

## Development

```bash
make setup   # .venv with ruff; the plugin and the tests only need the stdlib
make check   # lint and tests, the same thing CI runs
```

The tests run the plugin against a stand-in `orca` module and an in-process fake
Moonraker, so no printer is needed. They cover the Hi's real `box` answer, foreign lane
data, HTTP errors, timeouts, concurrent syncs and the plugin loader's permission rules.

## Releasing

Bump `version` in the header of `creality_cfs_sync_any.py`, push, and create a GitHub
release tagged `vX.Y.Z` with the release notes as the body. The
`publish-orcacloud.yml` workflow uploads the file to Orca Cloud through GitHub OIDC,
with the tag as the version and the notes as the changelog. The tag must be higher than
the version currently on Orca Cloud.

## License

MIT.
