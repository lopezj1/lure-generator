"""Post-process sliced G-code: insert pauses at layer changes (#33).

Orca marks each layer with `;LAYER_CHANGE` followed by `;Z:<print z>` and `;HEIGHT:<h>`.
A pause for Z goes right before the `;LAYER_CHANGE` of the first layer whose print Z is at or
above it, so the layer below is finished and the head has not yet moved up. Each inserted
block starts with a marker comment, which makes re-running the insertion a no-op.
"""

import re
from dataclasses import dataclass
from pathlib import Path

LAYER_CHANGE = ";LAYER_CHANGE"
_Z_RE = re.compile(r"^;Z:\s*([-+]?\d*\.?\d+)")
MARKER = "; LUREGEN_PAUSE"
_MARKER_RE = re.compile(r"^; LUREGEN_PAUSE layer=(\d+)")
EPS = 1e-6


class PauseError(ValueError):
    pass


@dataclass(frozen=True)
class Layer:
    index: int  # 0-based
    line: int  # 0-based line number of its `;LAYER_CHANGE`
    z: float  # print Z (top of the layer), mm


@dataclass(frozen=True)
class Pause:
    z_request: float
    layer: Layer
    inserted: bool  # False when that layer already had a pause


def find_layers(lines: list[str]) -> list[Layer]:
    """Layers from `;LAYER_CHANGE` markers and the `;Z:` comment that follows each."""
    layers: list[Layer] = []
    for i, line in enumerate(lines):
        if line.strip() != LAYER_CHANGE:
            continue
        z = None
        for follow in lines[i + 1 : i + 4]:
            if m := _Z_RE.match(follow.strip()):
                z = float(m.group(1))
                break
        if z is None:
            raise PauseError(f"line {i + 1}: {LAYER_CHANGE} without a ;Z: comment")
        layers.append(Layer(index=len(layers), line=i, z=z))
    return layers


def layer_at_or_above(layers: list[Layer], z: float) -> Layer:
    for layer in layers:
        if layer.z >= z - EPS:
            return layer
    top = layers[-1].z if layers else None
    raise PauseError(f"no layer at or above Z={z:.3f} (top layer Z={top})")


def paused_layers(lines: list[str]) -> set[int]:
    return {int(m.group(1)) for line in lines if (m := _MARKER_RE.match(line))}


def pause_block(command: str, z_request: float, layer: Layer) -> list[str]:
    return [
        f"{MARKER} layer={layer.index} z={layer.z:.3f} requested_z={z_request:.3f}",
        "; pause for ballast/insert before printing this layer",
        *command.splitlines(),
    ]


def insert_pauses(text: str, zs: list[float], command: str = "M600") -> tuple[str, list[Pause]]:
    """Insert `command` before the first layer at or above each Z. Returns (text, pauses)."""
    lines = text.splitlines()
    layers = find_layers(lines)
    if not layers:
        raise PauseError(f"no {LAYER_CHANGE} markers; is this Orca G-code?")
    done = paused_layers(lines)
    pauses: list[Pause] = []
    for z in sorted(zs):
        layer = layer_at_or_above(layers, z)
        pauses.append(Pause(z_request=z, layer=layer, inserted=layer.index not in done))
        done.add(layer.index)

    # Insert bottom-up so earlier line numbers stay valid.
    for pause in sorted((p for p in pauses if p.inserted), key=lambda p: -p.layer.line):
        lines[pause.layer.line : pause.layer.line] = pause_block(
            command, pause.z_request, pause.layer
        )
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + trailing, pauses


def insert_pauses_file(
    path: Path, zs: list[float], command: str = "M600", out: Path | None = None
) -> list[Pause]:
    """`insert_pauses` on a file, rewriting it in place unless `out` is given."""
    text, pauses = insert_pauses(Path(path).read_text(), zs, command)
    Path(out or path).write_text(text)
    return pauses
