# lure-generator

Code-first, config-driven pipeline for designing and manufacturing fishing lures:

**profile image + TOML spec → balanced CAD model (STEP/STL) → G-code** for a 3D printer (Snapmaker U1 via OrcaSlicer) or a CNC router (Shapeoko 3 XXL / Carbide Motion).

The first lure family is a two-section jointed **glide bait** that hangs level and is tuned to float, suspend or slow-sink in salt water once hardware is added.

Hobby project, work in progress. The plan and progress are tracked in the issues, starting from the epic (#1).

## Layout

```
spec/      data only: designs/, printers/, routers/, hardware/, materials.toml
src/       the `luregen` package (profile, geometry, physics, dfm, fab)
tools/     standalone dev scripts
tests/
builds/    generated outputs (gitignored)
field/     tank-test results used to calibrate targets
```

## Setup

```
sudo apt-get install -y libgl1 libx11-6 xvfb   # OCP wheel needs libGL/libX11; xvfb for slicer thumbnails
uv sync
uv run lure --help
```

## Usage

```
uv run lure build spec/designs/glide/variants/striper_175_petg.toml [--set buoyancy.target_sink=suspend]
uv run lure check builds/glide/striper_175_petg
uv run lure fab builds/glide/striper_175_petg/latest --format print   # or --format cnc (planned)
```
