"""Turn print (OrcaSlicer) or CNC G-code into drawable line segments for the web viewers (#64).

Both extractors produce a `Toolpath`: one polyline of N+1 points, where segment i runs from
`points[i]` to `points[i + 1]`, plus per-segment attribute arrays and a JSON-able `meta` dict
(tables, layers, markers, stats). Discontinuities (homing, `G92` resets, files that start at an
unknown position) become **jump** segments (kind 0), which viewers never draw.

- **Print:** a regex line scanner: Orca output runs to 600k+ lines and mixes in Klipper
  macro lines, which the strict `luregen.fab.gcode` tokenizer rejects. It tracks M82/M83,
  G90/G91, G92, the layer from `;LAYER_CHANGE` / `;Z:` / `;HEIGHT:`, the feature from
  `;TYPE:`, `;WIDTH:`, the tool from bare `T<n>` lines (never the T in `M104 T1`) and pause
  commands. E > 0 extrudes; anything else travels.
- **CNC:** the shared tokenizer and modal interpreter (`fab.gcode`). Moves are rapid, feed, or
  plunge (a pure −Z feed); arcs in G17/G18/G19 are tessellated in their own plane to a chord
  tolerance. Machine-coordinate moves (`G53 G0 Z-5`, `G28 G91 Z0`) end at an unknown height:
  they are drawn at a clearance Z (`max finite Z + CLEARANCE_MARGIN`), with X/Y unchanged when
  the move doesn't name them.
"""

import contextlib
import math
import re
from array import array
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from luregen.fab.gcode import (
    PLANE_AXES,
    Comment,
    Dwell,
    GcodeSyntaxError,
    Motion,
    Plane,
    Spindle,
    Stop,
    ToolChange,
    Vec3,
    arc_sweep,
    interpret,
    tokenize_line,
)
from luregen.fab.printer.verify import parse_duration

Kind = Literal["print", "cnc"]

PRINT_KINDS = ("jump", "travel", "extrude")
CNC_KINDS = ("jump", "rapid", "feed", "plunge")
JUMP, TRAVEL, EXTRUDE = 0, 1, 2
RAPID, FEED, PLUNGE = 1, 2, 3

DEFAULT_TOLERANCE = 0.02  # mm chord sagitta for arc tessellation
DEFAULT_MAX_ARC_SEGMENTS = 512
# CNC time estimate only: per-axis rapid rates (mm/min) and the wait per M6 (prompt, swap,
# BitSetter probe), as in `spec/routers/shapeoko3_xxl/machine.toml` and `fab.router.verify`.
DEFAULT_RAPID_RATES = (15000.0, 15000.0, 1300.0)
DEFAULT_TOOL_CHANGE_S = 60.0
DEFAULT_PAUSE_COMMANDS = ("M600", "PAUSE", "M601")
CLEARANCE_MARGIN = 5.0  # mm above the highest known Z for machine-coordinate retracts
UNKNOWN_FEATURE = "Unknown"


@dataclass
class Toolpath:
    """Segments of one G-code file; arrays are per segment except `points` (N+1, 3)."""

    kind: Kind
    points: np.ndarray  # float32 (N+1, 3); empty (0, 3) when there are no segments
    seg_kind: np.ndarray  # uint8, index into meta["kinds"]
    tool: np.ndarray  # uint8, index into meta["tools"]
    feed: np.ndarray  # float32 mm/min (0 = unknown)
    line: np.ndarray  # uint32, 1-based source line
    feature: np.ndarray | None = None  # print: uint8, index into meta["features"]
    layer: np.ndarray | None = None  # print: uint16, index into meta["layers"]
    width: np.ndarray | None = None  # print: uint16 µm (0 on non-extruding segments)
    height: np.ndarray | None = None  # print: uint16 µm
    time: np.ndarray | None = None  # CNC: float32 (N, 2) job time in s at segment start/end
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def n_segments(self) -> int:
        return len(self.seg_kind)


class _Builder:
    """Accumulates the polyline; inserts jumps at discontinuities.

    Slowly-changing attributes (tool, feed, feature, layer, width, height) are stored as runs:
    the caller assigns `state` and sets `dirty`; the next segment opens a new run. Runs are
    expanded with numpy at the end, which keeps the per-segment cost to three appends.
    """

    def __init__(self, print_mode: bool) -> None:
        self.pts = array("d")
        self.kind = array("B")
        self.line = array("I")
        self.print_mode = print_mode
        self.run_start = array("I")
        self.runs: list[tuple[float, ...]] = []
        self.state: tuple[float, ...] = (0, 0.0, 0, 0, 0, 0)  # tool feed feature layer w h
        self.dirty = True
        self.last: tuple[float, float, float] | None = None

    def __len__(self) -> int:
        return len(self.kind)

    def add(self, start: tuple, end: tuple, kind: int, line: int) -> None:
        if self.dirty:
            self.run_start.append(len(self.kind))
            self.runs.append(self.state)
            self.dirty = False
        if self.last is None:
            self.pts.extend(start)
        elif start != self.last:
            self.kind.append(JUMP)
            self.line.append(line)
            self.pts.extend(start)
        self.kind.append(kind)
        self.line.append(line)
        self.pts.extend(end)
        self.last = end

    def arrays(self) -> dict[str, np.ndarray]:
        n = len(self.kind)
        kind = np.frombuffer(self.kind, dtype=np.uint8)
        if self.runs:
            counts = np.diff(np.append(np.frombuffer(self.run_start, dtype=np.uint32), n))
            runs = np.array(self.runs, dtype=np.float64)
            cols = [np.repeat(runs[:, k], counts) for k in range(runs.shape[1])]
        else:
            cols = [np.zeros(0) for _ in range(6)]
        feed = cols[1].astype(np.float32)
        feed[kind == JUMP] = 0.0
        out = {
            "points": np.frombuffer(self.pts, dtype=np.float64).reshape(-1, 3),
            "seg_kind": kind,
            "tool": cols[0].astype(np.uint8),
            "feed": feed,
            "line": np.frombuffer(self.line, dtype=np.uint32),
        }
        if self.print_mode:
            width = cols[4].astype(np.uint16)
            width[kind != EXTRUDE] = 0
            out["feature"] = cols[2].astype(np.uint8)
            out["layer"] = cols[3].astype(np.uint16)
            out["width"] = width
            out["height"] = cols[5].astype(np.uint16)
        return out


# --- arcs ---------------------------------------------------------------------------------


def arc_polyline(m: Motion, tol: float = DEFAULT_TOLERANCE, max_segments: int = 512) -> list[Vec3]:
    """Points along an arc motion (start excluded, end exact) with chord sagitta ≤ `tol`.

    Works in the motion's plane (G17/G18/G19); the normal axis moves linearly (helix). When
    the end radius differs slightly from the start radius, the radius is interpolated so the
    polyline lands on the end point. `max_segments` caps very large or many-turn arcs.
    """
    a, b, n = PLANE_AXES[m.plane]
    c = m.center
    assert c is not None
    s, e = m.start, m.end
    r0 = math.hypot(s[a] - c[a], s[b] - c[b])
    r1 = math.hypot(e[a] - c[a], e[b] - c[b])
    if r0 < 1e-9:
        return [e]
    sweep = arc_sweep(m)
    r = max(r0, r1)
    step = 2 * math.acos(1 - tol / r) if r > tol else math.pi / 2
    count = min(max_segments, max(1, math.ceil(sweep / step - 1e-9)))
    t0 = math.atan2(s[b] - c[b], s[a] - c[a])
    sign = -1.0 if m.mode == "cw" else 1.0
    pts: list[Vec3] = []
    p = [0.0, 0.0, 0.0]
    for k in range(1, count):
        f = k / count
        t = t0 + sign * sweep * f
        rr = r0 + (r1 - r0) * f
        p[a] = c[a] + rr * math.cos(t)
        p[b] = c[b] + rr * math.sin(t)
        p[n] = s[n] + (e[n] - s[n]) * f
        pts.append((p[0], p[1], p[2]))
    pts.append(e)
    return pts


def _arc_center(
    plane: Plane, start: Vec3, end: Vec3, offsets: dict[str, float], ccw: bool
) -> Vec3 | None:
    """Center from IJK offsets or an `R` word (as in `fab.gcode`); None if R can't reach."""
    a, b, _ = PLANE_AXES[plane]
    c = list(start)
    if "R" not in offsets:
        for k in (a, b):
            c[k] = start[k] + offsets.get("IJK"[k], 0.0)
        return (c[0], c[1], c[2])
    radius = offsets["R"]
    dx, dy = end[a] - start[a], end[b] - start[b]
    d = math.hypot(dx, dy)
    if d == 0 or abs(radius) < d / 2 - 1e-9:
        return None
    h = math.sqrt(max(radius * radius - d * d / 4, 0.0))
    sign = 1.0 if ccw == (radius > 0) else -1.0
    c[a] = start[a] + dx / 2 - sign * h * dy / d
    c[b] = start[b] + dy / 2 + sign * h * dx / d
    return (c[0], c[1], c[2])


# --- kind detection -----------------------------------------------------------------------

_PRINT_SIGNS = re.compile(
    r"^;\s*(?:generated by|FLAVOR:|LAYER_CHANGE|TYPE:|HEADER_BLOCK_START)", re.M | re.I
)
_EXTRUDE_WORD = re.compile(r"^G[0-3]\b[^;(\n]*\bE[-+.\d]", re.M)


def detect_kind(head: str) -> Kind:
    """'print' for slicer output (Orca header, layer/type comments, E words), else 'cnc'."""
    if _PRINT_SIGNS.search(head) or _EXTRUDE_WORD.search(head):
        return "print"
    return "cnc"


# --- print --------------------------------------------------------------------------------

_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_TOOL_RE = re.compile(r"^T(\d+)\s*$")
_HEADER_RES = {
    "generator": re.compile(r"^; generated by (.+?)(?: on \d.*)?$", re.M),
    "header_layers": re.compile(r"^; total layer number: (\d+)", re.M),
    "tool_changes": re.compile(r"^; total filament change = (\d+)", re.M),
    "estimated_time": re.compile(r"^; estimated printing time \(normal mode\) = (.+)$", re.M),
}
_CONFIG_KEYS = (
    "filament_colour",
    "filament_type",
    "filament_settings_id",
    "machine_pause_gcode",
    "layer_height",
)


def _print_info(text: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Header facts and a few CONFIG_BLOCK keys (searched only where they live)."""
    info: dict[str, Any] = {}
    head = text[:200_000]
    tail_at = text.rfind("; EXECUTABLE_BLOCK_END")
    tail = text[tail_at:] if tail_at >= 0 else text[-200_000:]
    for key, rx in _HEADER_RES.items():
        m = rx.search(head) or rx.search(tail)
        if m:
            info[key] = int(m.group(1)) if m.group(1).isdigit() else m.group(1).strip()
    if "estimated_time" in info:
        with contextlib.suppress(ValueError):
            info["estimated_time_s"] = parse_duration(info["estimated_time"])
    config: dict[str, str] = {}
    for key in _CONFIG_KEYS:
        if m := re.search(rf"^; {key} = (.*)$", tail, re.M):
            config[key] = m.group(1).strip()
    return info, config


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip().strip('"') for v in re.split(r"[;,]", value)]


def extract_print(
    text: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    max_arc_segments: int = DEFAULT_MAX_ARC_SEGMENTS,
    pause_commands: tuple[str, ...] | None = None,
) -> Toolpath:
    """Segments of slicer G-code: travel/extrude, layer, feature, tool, width/height, feed."""
    info, config = _print_info(text)
    pauses = set(pause_commands or DEFAULT_PAUSE_COMMANDS)
    if pause_commands is None and (cmd := config.get("machine_pause_gcode")):
        pauses.add(cmd.split()[0])

    b = _Builder(print_mode=True)
    features: dict[str, int] = {UNKNOWN_FEATURE: 0}
    layers: list[dict[str, Any]] = [{"z": None, "height": None, "start": 0, "line": 1}]
    markers: list[dict[str, Any]] = []
    e_by_tool: dict[int, float] = {}
    nan = math.nan
    x = y = z = nan
    known = False
    abs_xyz, rel_e = True, False
    m83 = False
    e_pos = 0.0
    feed = 0.0
    scale = 1.0
    plane: Plane = "xy"
    tool: int | None = None
    feature = layer = width = height = 0
    word_re = _WORD_RE

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw:
            continue
        c0 = raw[0]
        if c0 == " " or c0 == "\t":
            raw = raw.strip()
            if not raw:
                continue
            c0 = raw[0]
        if c0 == ";":
            c1 = raw[1:2]
            if c1 == "T" and raw.startswith(";TYPE:"):
                name = raw[6:].strip()
                feature = features.setdefault(name, len(features))
            elif c1 == "W" and raw.startswith(";WIDTH:"):
                width = round(float(raw[7:]) * 1000)
            elif c1 == "H" and raw.startswith(";HEIGHT:"):
                h = float(raw[8:])
                height = round(h * 1000)
                if layers[-1]["height"] is None and layer:
                    layers[-1]["height"] = h
            elif c1 == "Z" and raw.startswith(";Z:"):
                layers[-1]["z"] = float(raw[3:])
            elif c1 == "L" and raw.startswith(";LAYER_CHANGE"):
                layer += 1
                layers.append({"z": None, "height": None, "start": len(b), "line": lineno})
            else:
                continue
            b.state = (tool or 0, feed, feature, layer, width, height)
            b.dirty = True
            continue

        semi = raw.find(";")
        code = raw if semi < 0 else raw[:semi]
        if c0 == "G" or c0 == "g":
            words = word_re.findall(code)
            if not words:
                continue
            g = words[0][1]
            if g in ("1", "0", "01", "00", "2", "3", "02", "03"):
                nx = ny = nz = None
                e = None
                offs: dict[str, float] = {}
                for letter, val in words[1:]:
                    if letter == "X":
                        nx = float(val) * scale
                    elif letter == "Y":
                        ny = float(val) * scale
                    elif letter == "Z":
                        nz = float(val) * scale
                    elif letter == "E":
                        e = float(val)
                    elif letter == "F":
                        f = float(val) * scale
                        if f != feed:
                            feed = f
                            b.state = (tool or 0, feed, feature, layer, width, height)
                            b.dirty = True
                    elif letter in "IJKR":
                        offs[letter] = float(val) * scale
                de = 0.0
                if e is not None:
                    if rel_e:
                        de = e
                    else:
                        de = e - e_pos
                        e_pos = e
                    if de:
                        e_by_tool[tool or 0] = e_by_tool.get(tool or 0, 0.0) + de
                if nx is None and ny is None and nz is None:
                    continue
                start = (x, y, z)
                if abs_xyz:
                    x = x if nx is None else nx
                    y = y if ny is None else ny
                    z = z if nz is None else nz
                else:
                    x = x if nx is None else x + nx
                    y = y if ny is None else y + ny
                    z = z if nz is None else z + nz
                end = (x, y, z)
                if not known:
                    known = x == x and y == y and z == z
                    continue
                if end == start and g not in ("2", "3", "02", "03"):
                    continue
                kind = EXTRUDE if de > 0 else TRAVEL
                if g in ("2", "3", "02", "03"):
                    ccw = g in ("3", "03")
                    center = _arc_center(plane, start, end, offs, ccw)
                    if center is not None:
                        mode = "ccw" if ccw else "cw"
                        m = Motion(lineno, mode, start, end, plane=plane, center=center)
                        p0 = start
                        for p1 in arc_polyline(m, tolerance, max_arc_segments):
                            b.add(p0, p1, kind, lineno)
                            p0 = p1
                        continue
                b.add(start, end, kind, lineno)
            elif g in ("92",):
                for letter, val in words[1:]:
                    if letter == "E":
                        e_pos = float(val)
                    elif letter in "XYZ":
                        v = float(val) * scale
                        if letter == "X":
                            x = v
                        elif letter == "Y":
                            y = v
                        else:
                            z = v
                known = x == x and y == y and z == z
            elif g in ("90",):
                abs_xyz, rel_e = True, m83
            elif g in ("91",):
                abs_xyz, rel_e = False, True
            elif g in ("28",):
                axes = [letter for letter, _ in words[1:] if letter in "XYZ"]
                if not axes:
                    axes = ["X", "Y", "Z"]
                x = nan if "X" in axes else x
                y = nan if "Y" in axes else y
                z = nan if "Z" in axes else z
                known = False
            elif g in ("17", "18", "19"):
                plane = {"17": "xy", "18": "zx", "19": "yz"}[g]
            elif g == "20":
                scale = 25.4
            elif g == "21":
                scale = 1.0
            continue

        if c0 == "T" or c0 == "t":
            if m_t := _TOOL_RE.match(code.strip().upper()):
                new = int(m_t.group(1))
                if tool is not None and new != tool:
                    markers.append(
                        {
                            "type": "tool_change",
                            "line": lineno,
                            "layer": layer,
                            "segment": len(b),
                            "from": tool,
                            "tool": new,
                        }
                    )
                tool = new
                b.state = (tool, feed, feature, layer, width, height)
                b.dirty = True
            continue
        tok = code.split(None, 1)[0].upper() if code.strip() else ""
        if tok == "M83":
            m83 = rel_e = True
        elif tok == "M82":
            m83 = False
            rel_e = not abs_xyz
        elif tok in pauses:
            markers.append(
                {
                    "type": "pause",
                    "line": lineno,
                    "layer": layer,
                    "segment": len(b),
                    "command": tok,
                }
            )

    arrays = b.arrays()
    n_filaments = max(len(_split_list(config.get("filament_colour"))), 1)
    used = int(arrays["tool"].max()) + 1 if len(arrays["tool"]) else 0
    n_tools = max(n_filaments, used, (tool or 0) + 1)
    if n_tools > 256 or len(features) > 256 or layer > 65535:
        raise ValueError("too many tools, features or layers for the binary format")
    colours = _split_list(config.get("filament_colour"))
    types = _split_list(config.get("filament_type"))
    names = _split_list(config.get("filament_settings_id"))
    tools = []
    for i in range(n_tools):
        entry: dict[str, Any] = {"number": i}
        for key, values in (("color", colours), ("filament_type", types), ("filament", names)):
            if i < len(values) and values[i]:
                entry[key] = values[i]
        entry["filament_mm"] = round(e_by_tool.get(i, 0.0), 3)
        tools.append(entry)

    n_seg = len(arrays["seg_kind"])
    meta: dict[str, Any] = {
        "kinds": list(PRINT_KINDS),
        "features": list(features),
        "tools": tools,
        "layers": layers,
        "layer_offsets": [ly["start"] for ly in layers] + [n_seg],
        "markers": markers,
        "source": info,
        "params": {
            "tolerance": tolerance,
            "max_arc_segments": max_arc_segments,
            "pause_commands": sorted(pauses),
        },
    }
    return _finish("print", arrays, meta)


# --- CNC ----------------------------------------------------------------------------------

_FUSION_TOOL_RE = re.compile(
    r"^T(\d+)\s+D=([-+\d.]+)(?:\s+CR=([-+\d.]+))?(?:\s*-\s*ZMIN=([-+\d.]+))?(?:\s*-\s*(.+))?$"
)
_CARBIDE_TOOL_RE = re.compile(r"^TOOL/MILL,\s*([-+\d.]+),\s*([-+\d.]+)")
_STOCK_RE = re.compile(r"^stock(Min|Max):\s*([-+\d.]+)mm,\s*([-+\d.]+)mm,\s*([-+\d.]+)mm", re.I)


def extract_cnc(
    text: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    max_arc_segments: int = DEFAULT_MAX_ARC_SEGMENTS,
    rapid_rates: tuple[float, float, float] = DEFAULT_RAPID_RATES,
    tool_change_s: float = DEFAULT_TOOL_CHANGE_S,
) -> Toolpath:
    """Segments of router G-code: rapid/feed/plunge, tool, feed; arcs tessellated.

    Also a job timeline (`Toolpath.time`): feeds take length / F, rapids the slowest axis at
    `rapid_rates`, and every `M6` (`tool_change_s`) or `G4` dwell waits before the next move.
    """
    b = _Builder(print_mode=False)
    tool_index: dict[int | None, int] = {}
    tool_info: dict[int | None, dict[str, Any]] = {}
    pending: dict[str, Any] = {}  # Carbide (TOOL/MILL,…) info waiting for the next T
    stock: dict[str, list[float]] = {}
    markers: list[dict[str, Any]] = []
    skipped: list[int] = []
    waits: list[tuple[int, float]] = []  # (next segment, seconds)
    tool_changes = 0
    cur: dict[str, Any] = {"tool": None, "line": None}

    def index_of(number: int | None) -> int:
        if number not in tool_index:
            tool_index[number] = len(tool_index)
            tool_info.setdefault(number, {})
        return tool_index[number]

    def lines():
        for n, raw in enumerate(text.splitlines(), 1):
            try:
                ln = tokenize_line(raw, n)
            except GcodeSyntaxError:
                skipped.append(n)
                continue
            for comment in ln.comments:
                _cnc_comment(comment.strip(), tool_info, pending, stock)
            if (t := ln.get("T")) is not None:
                number = int(t.value)
                if pending:
                    tool_info.setdefault(number, {}).update(pending)
                    pending.clear()
                prev = cur["tool"]
                if prev is not None and number != prev:
                    markers.append(
                        {
                            "type": "tool_change",
                            "line": n,
                            "segment": len(b),
                            "from": index_of(prev),
                            "tool": index_of(number),
                        }
                    )
                cur["tool"] = number
                index_of(number)
            yield ln

    disp: list[float] | None = None  # displayed position; Z may be +inf (clearance)
    inf = math.inf
    for ev in interpret(lines()):
        if isinstance(ev, Comment):
            continue
        if isinstance(ev, ToolChange):
            tool_changes += 1
            waits.append((len(b), tool_change_s))
            continue
        if isinstance(ev, Dwell):
            waits.append((len(b), ev.seconds))
            continue
        if isinstance(ev, Spindle):
            if ev.on and ev.rpm:
                tool_info.setdefault(cur["tool"], {}).setdefault("spindle_rpm", ev.rpm)
            continue
        if isinstance(ev, Stop):
            if ev.code in ("M0", "M1"):
                markers.append(
                    {"type": "pause", "line": ev.line, "segment": len(b), "command": ev.code}
                )
            continue
        if not isinstance(ev, Motion):
            continue
        end = list(ev.end)
        for k in (0, 1):
            if not math.isfinite(end[k]):
                end[k] = disp[k] if disp is not None else math.nan
        if math.isnan(end[2]):
            end[2] = disp[2] if disp is not None else inf
        if math.isnan(end[0]) or math.isnan(end[1]):
            continue
        end_t = (end[0], end[1], end[2])
        if disp is None:
            disp = end
            continue
        start_t = (disp[0], disp[1], disp[2])
        disp = end
        if end_t == start_t:
            continue
        t = index_of(cur["tool"])
        feed = ev.feed or 0.0
        if ev.mode == "rapid":
            kind = RAPID
            feed = 0.0
        elif not ev.is_arc and end_t[:2] == start_t[:2] and end_t[2] < start_t[2]:
            kind = PLUNGE
        else:
            kind = FEED
        if b.state[0] != t or b.state[1] != feed:
            b.state = (t, feed, 0, 0, 0, 0)
            b.dirty = True
        if ev.is_arc and ev.center is not None and start_t == ev.start:
            p0 = start_t
            for p1 in arc_polyline(ev, tolerance, max_arc_segments):
                b.add(p0, p1, kind, ev.line)
                p0 = p1
        else:
            b.add(start_t, end_t, kind, ev.line)

    arrays = b.arrays()
    pts = arrays["points"]
    finite_z = pts[:, 2][np.isfinite(pts[:, 2])] if len(pts) else pts[:, 2]
    top = float(finite_z.max()) if len(finite_z) else 0.0
    clearance = top + CLEARANCE_MARGIN
    if len(pts):
        pts = pts.copy()
        pts[~np.isfinite(pts[:, 2]), 2] = clearance
        arrays["points"] = pts

    if len(tool_index) > 256:
        raise ValueError("too many tools for the binary format")
    tools = []
    for number, _ in sorted(tool_index.items(), key=lambda kv: kv[1]):
        tools.append({"number": number, **tool_info.get(number, {})})
    meta: dict[str, Any] = {
        "kinds": list(CNC_KINDS),
        "tools": tools,
        "markers": markers,
        "clearance_z": round(clearance, 4),
        "source": {"skipped_lines": skipped[:50], "skipped_count": len(skipped)},
        "params": {
            "tolerance": tolerance,
            "max_arc_segments": max_arc_segments,
            "rapid_rates": list(rapid_rates),
            "tool_change_s": tool_change_s,
        },
    }
    if stock:
        meta["stock"] = stock
    tp = _finish("cnc", arrays, meta)
    _cnc_timeline(tp, pts, rapid_rates, waits, tool_changes)
    return tp


def _cnc_timeline(
    tp: Toolpath,
    pts: np.ndarray,
    rapid_rates: tuple[float, float, float],
    waits: list[tuple[int, float]],
    tool_changes: int,
) -> None:
    """Fill `tp.time` (start/end s per segment) and the time stats and marker times.

    Waits (tool changes, dwells) sit between segments: segment i starts `wait[i]` after
    segment i-1 ended. Per-tool `time_s` counts motion only; `stats.time_s` adds the waits.
    """
    n = tp.n_segments
    meta = tp.meta
    delta = np.abs(np.diff(pts, axis=0)) if n else np.zeros((0, 3))
    seg_len = np.linalg.norm(delta, axis=1)
    rapid = tp.seg_kind == RAPID
    cutting = (tp.seg_kind == FEED) | (tp.seg_kind == PLUNGE)
    feed = tp.feed.astype(np.float64)
    dur = np.zeros(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        dur[rapid] = (delta[rapid] / np.asarray(rapid_rates)).max(axis=1) * 60.0
        cut = cutting & (feed > 0)
        # GRBL slows a feed move so no axis exceeds its max rate.
        axis_cap = (delta[cut] / np.asarray(rapid_rates)).max(axis=1)
        dur[cut] = np.maximum(seg_len[cut] / feed[cut], axis_cap) * 60.0
    wait = np.zeros(n + 1)
    if waits:
        idx, sec = zip(*waits, strict=True)
        wait = np.bincount(np.asarray(idx), weights=np.asarray(sec, dtype=float), minlength=n + 1)
    end = np.cumsum(dur + wait[:n])
    total = float(end[-1] if n else 0.0) + float(wait[n])
    tp.time = np.stack([end - dur, end], axis=1).astype(np.float32).reshape(-1, 2)

    for m in meta["markers"]:
        s = m["segment"]
        m["time_s"] = round(float(end[s - 1]) if s > 0 else 0.0, 2)
    stats = meta["stats"]
    for row in stats["tools"]:
        sel = tp.tool == row["tool"]
        row["time_s"] = round(float(dur[sel].sum()), 1)
        row["cut_time_s"] = round(float(dur[sel & cutting].sum()), 1)
        row["rapid_time_s"] = round(float(dur[sel & rapid].sum()), 1)
    stats["motion_s"] = round(float(dur.sum()), 1)
    stats["wait_s"] = round(float(wait.sum()), 1)
    stats["time_s"] = round(total, 1)
    stats["tool_changes"] = tool_changes


def _cnc_comment(
    text: str,
    info: dict[int | None, dict[str, Any]],
    pending: dict[str, Any],
    stock: dict[str, list[float]],
) -> None:
    if m := _FUSION_TOOL_RE.match(text):
        entry = info.setdefault(int(m.group(1)), {})
        entry["diameter"] = float(m.group(2))
        if m.group(3) is not None:
            entry["corner_radius"] = float(m.group(3))
        if m.group(4) is not None:
            entry["zmin"] = float(m.group(4))
        if m.group(5):
            entry["description"] = m.group(5).strip()
    elif m := _CARBIDE_TOOL_RE.match(text):
        pending["diameter"] = float(m.group(1))
        pending["corner_radius"] = float(m.group(2))
    elif m := _STOCK_RE.match(text):
        stock[m.group(1).lower()] = [float(m.group(k)) for k in (2, 3, 4)]


# --- shared -------------------------------------------------------------------------------


def _bounds(pts: np.ndarray) -> dict[str, list[float]] | None:
    if not len(pts):
        return None
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    return {"min": [round(float(v), 4) for v in lo], "max": [round(float(v), 4) for v in hi]}


def _finish(
    kind: Kind,
    arrays: dict[str, np.ndarray],
    meta: dict[str, Any],
) -> Toolpath:
    """Convert to the stored dtypes and add bounds, counts and stats to `meta`."""
    pts64 = arrays.pop("points")
    tp = Toolpath(
        kind=kind,
        points=pts64.astype(np.float32).reshape(-1, 3),
        seg_kind=arrays["seg_kind"],
        tool=arrays["tool"],
        feed=arrays["feed"],
        line=arrays["line"],
        feature=arrays.get("feature"),
        layer=arrays.get("layer"),
        width=arrays.get("width"),
        height=arrays.get("height"),
        meta=meta,
    )
    n = tp.n_segments
    seg_len = np.linalg.norm(np.diff(pts64, axis=0), axis=1) if n else np.zeros(0, dtype=np.float64)
    drawn = tp.seg_kind != JUMP
    rapid = tp.seg_kind == (TRAVEL if kind == "print" else RAPID)
    active = drawn & ~rapid
    feed = tp.feed.astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        seg_time = np.where(drawn & (feed > 0), seg_len / feed * 60.0, 0.0)

    kinds = meta["kinds"]
    per_kind = {
        name: {
            "segments": int(np.count_nonzero(tp.seg_kind == i)),
            "length_mm": round(float(seg_len[tp.seg_kind == i].sum()), 3),
        }
        for i, name in enumerate(kinds)
    }
    per_tool = []
    for i, entry in enumerate(meta["tools"]):
        sel = tp.tool == i
        row: dict[str, Any] = {"tool": i, "number": entry.get("number")}
        row["segments"] = int(np.count_nonzero(sel & drawn))
        row["active_length_mm"] = round(float(seg_len[sel & active].sum()), 3)
        row["rapid_length_mm"] = round(float(seg_len[sel & rapid].sum()), 3)
        row["time_s"] = round(float(seg_time[sel].sum()), 1)
        per_tool.append(row)
    stats: dict[str, Any] = {
        "kinds": per_kind,
        "tools": per_tool,
        "time_s": round(float(seg_time.sum()), 1),
    }
    if tp.feature is not None:
        stats["features"] = {
            name: {
                "segments": int(np.count_nonzero((tp.feature == i) & active)),
                "length_mm": round(float(seg_len[(tp.feature == i) & active].sum()), 3),
            }
            for i, name in enumerate(meta["features"])
        }
    ends = np.zeros(len(pts64), dtype=bool)
    if n:
        ends[:-1] |= active
        ends[1:] |= active
    meta.update(
        {
            "segments": n,
            "bounds": _bounds(pts64),
            "bounds_active": _bounds(pts64[ends]),
            "stats": stats,
        }
    )
    if kind == "print":
        meta["layer_count"] = len(meta["layers"]) - 1
    return tp


def extract(text: str, kind: Kind | Literal["auto"] = "auto", **opts: Any) -> Toolpath:
    """Dispatch on `kind` ('auto' looks at the first 64 kB)."""
    if kind == "auto":
        kind = detect_kind(text[:65536])
    if kind == "print":
        return extract_print(text, **opts)
    if kind == "cnc":
        return extract_cnc(text, **opts)
    raise ValueError(f"unknown toolpath kind {kind!r}")
