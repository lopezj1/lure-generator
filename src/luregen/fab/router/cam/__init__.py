"""Custom CAM for the router (F7b): 2.5D shapely ops, setup planner, job output.

- `ops2d`: face, pocket (offset rings), profile with tabs, peck drill / helical bore, groove.
  Each op returns IR moves (`luregen.fab.router.ir`) in work coordinates.
- `setups`: stock layout of the CNC halves, part → setup transforms, the flip about X, and
  the two-setup plan (glue face, then outer face).
- `heightmap`: z-buffer heightmaps of the half meshes, flat/ball drop-cutter dilations.
- `ops3d`: z-level roughing and parallel raster finishing on those heightmaps (#42).
- `job`: `lure fab --format cnc` from a build folder: plan, post, split by tool, verify,
  simulate against the halves and write the setup sheet (#43).
"""
