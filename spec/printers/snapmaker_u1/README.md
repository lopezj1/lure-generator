# Snapmaker U1

- `machine.toml`: slicer apps (upstream OrcaSlicer 2.5.1 primary, Snapmaker Orca 2.4.1 fallback) with pinned tags and AppImage URLs, stock profile names, lure CLI overrides, pause command, runtime (host or container) (#32)
- `profiles/<app>/`: flattened Orca machine/process/filament JSON from `lure sync-profiles snapmaker_u1 [--app orca|snapmaker]`; `source.json` records the tag and inheritance chain of each (#31). Regenerate, don't hand-edit.
- `reference/`: owner-supplied sample G-code, sanitized (#26)

The AppImages are Ubuntu 24.04 builds (glibc 2.38). On an older host, `luregen.fab.printer.orca` runs the extracted AppImage inside `tools/orca-runtime/Dockerfile` (built on first use). Set `LUREGEN_SLICER=/path/to/orca-slicer` to use a specific binary and `LUREGEN_SLICER_RUNTIME=host|docker` to force the runtime.
