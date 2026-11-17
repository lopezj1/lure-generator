"""Unit constants shared across the package."""

G_PER_OZ = 28.349523125


def g_to_oz(grams: float) -> float:
    return grams / G_PER_OZ


def oz_to_g(ounces: float) -> float:
    return ounces * G_PER_OZ
