# Shapeoko 3 XXL (Carbide Motion, GRBL 1.1 fork)

- `machine.toml`: travel, controller and supported codes, BitSetter, router dial, rapid rates
- `post.toml`: post-processor template, Carbide Create style (#36, #37)
- `tools.toml`, `feeds.toml`: tool library (Carbide tool numbers) and feeds/speeds per material
- `reference/`: owner-supplied sample G-code, sanitized (#28)
  - `fusion_sample_excerpt.nc`: trimmed excerpt of a file from another CAM that ran on this
    machine (spaced words, IJK arcs in G17 and G18, G28 retracts, tool select without M6).
  - The owner's Carbide Create sample is not committed; `tests/fixtures/carbide_style.nc`
    reproduces its style synthetically.

Check a new reference file with `lure analyze-gcode <file.nc>`: it prints the inferred
settings, a proposed `post.toml` and a diff against this one.
