"""Custom CAM for the router (F7b): 2.5D shapely ops, setup planner, job output.

- `ops2d`: face, pocket (offset rings), profile with tabs, peck drill / helical bore, groove.
  Each op returns IR moves (`luregen.fab.router.ir`) in work coordinates.
- `setups`: stock layout of the CNC halves, part → setup transforms, the flip about X, and
  the two-setup plan (glue face, then outer face).
- `job`: `lure fab --format cnc` from a build folder: plan, post, split by tool, verify.

3D heightmap roughing/finishing (#42) plugs into `setups.plan` through `SETUP2_3D_OPS`.
"""
