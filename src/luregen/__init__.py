"""luregen: profile image + TOML spec → balanced lure CAD → print/CNC G-code.

Coordinate convention (all geometry, all features):
    x  along the body, 0 at the nose tip, increasing toward the tail
    z  up (dorsal positive); z = 0 is the horizontal body axis through the nose tip
    y  lateral; bodies are symmetric about y = 0
Units: mm, g, g/cm³ (reports also show oz).
"""

__version__ = "0.1.0"
