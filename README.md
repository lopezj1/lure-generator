# lure-generator

Code-first, config-driven pipeline for designing and manufacturing fishing lures:

**profile image + TOML spec → balanced CAD model (STEP/STL) → G-code** for a 3D printer (Snapmaker U1 via OrcaSlicer) or a CNC router (Shapeoko 3 XXL / Carbide Motion).

The first lure family is a two-section jointed **glide bait** that hangs level and is tuned to float, suspend or slow-sink in salt water once hardware is added.

Hobby project, work in progress. The plan and progress are tracked in the issues, starting from the epic (#1).

## Layout (planned)

```
spec/      data only: designs/, printers/, routers/, hardware/, materials.toml
src/       the `luregen` package (profile, geometry, physics, dfm, fab)
tools/     standalone dev scripts
tests/
builds/    generated outputs (gitignored)
field/     tank-test results used to calibrate targets
```

## Usage (planned)

```
uv run lure build spec/designs/glide/variants/<variant>.toml
uv run lure fab builds/glide/<variant>/latest --format print   # or --format cnc
```
