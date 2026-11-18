"""G-code tokenizer and a small modal interpreter, shared by the post, verifier and analyzer.

The dialect is the GRBL 1.1 subset that hobby routers accept: one block per line, letter +
number words (with or without spaces between them), `( … )` and `; …` comments, `%` program
delimiters. `interpret()` walks the blocks and yields events (motions in work coordinates,
dwells, tool and spindle changes) in the order GRBL would execute them within a block.
"""

import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")


class GcodeSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class Word:
    letter: str
    value: float
    text: str  # the number exactly as written

    @property
    def decimals(self) -> int:
        return len(self.text.split(".", 1)[1]) if "." in self.text else 0

    @property
    def code(self) -> str:
        """Canonical form for G/M words: `G1`, `M3`, `G43.1` (leading zeros dropped)."""
        return f"{self.letter}{format_code_number(self.value)}"


def format_code_number(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


@dataclass
class Line:
    number: int  # 1-based
    raw: str
    words: list[Word] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    separated: bool | None = None  # whitespace between words? None when < 2 words

    def codes(self, letter: str) -> list[str]:
        return [w.code for w in self.words if w.letter == letter]

    def has_code(self, code: str) -> bool:
        return any(w.code == code for w in self.words if w.letter in "GM")

    def get(self, letter: str) -> Word | None:
        return next((w for w in self.words if w.letter == letter), None)

    @property
    def axes(self) -> dict[str, float]:
        return {w.letter: w.value for w in self.words if w.letter in "XYZ"}


def tokenize_line(text: str, number: int = 1) -> Line:
    """Split one line into words and comments; raise `GcodeSyntaxError` on stray text."""
    line = Line(number=number, raw=text.rstrip("\r\n"))
    s = line.raw
    i, gaps = 0, []
    pending_gap = False
    while i < len(s):
        ch = s[i]
        if ch in " \t":
            pending_gap = True
            i += 1
        elif ch == "(":
            end = s.find(")", i)
            if end < 0:
                raise GcodeSyntaxError(f"line {number}: unclosed comment")
            line.comments.append(s[i + 1 : end])
            i = end + 1
        elif ch == ";":
            line.comments.append(s[i + 1 :].strip())
            break
        elif ch == "%" and not line.words:
            i += 1
        elif ch.isalpha():
            j = i + 1
            while j < len(s) and s[j] in " \t":
                j += 1
            m = _NUMBER.match(s, j)
            if not m:
                raise GcodeSyntaxError(f"line {number}: word {ch!r} has no number")
            if line.words:
                gaps.append(pending_gap)
            line.words.append(Word(ch.upper(), float(m.group()), m.group()))
            pending_gap = False
            i = m.end()
        else:
            raise GcodeSyntaxError(f"line {number}: unexpected {ch!r}")
    if gaps:
        line.separated = all(gaps)
    return line


def parse(text: str) -> list[Line]:
    return [tokenize_line(raw, n) for n, raw in enumerate(text.splitlines(), start=1)]


# --- interpreter -------------------------------------------------------------------------

Vec3 = tuple[float, float, float]
Plane = Literal["xy", "zx", "yz"]
# In-plane axis indices (first, second) and the normal axis, per plane.
PLANE_AXES: dict[str, tuple[int, int, int]] = {"xy": (0, 1, 2), "zx": (2, 0, 1), "yz": (1, 2, 0)}
_OFFSET_LETTERS = "IJK"
UNKNOWN_Z = math.inf  # Z after a machine-coordinate retract: above everything
UNKNOWN_XY = math.nan


@dataclass(frozen=True)
class Motion:
    line: int
    mode: Literal["rapid", "feed", "cw", "ccw"]
    start: Vec3
    end: Vec3
    feed: float | None = None
    plane: Plane = "xy"
    center: Vec3 | None = None
    spindle_on: bool = False
    tool: int | None = None
    machine: bool = False  # G53 / G28: coordinates are machine, not work

    @property
    def is_arc(self) -> bool:
        return self.mode in ("cw", "ccw")

    @property
    def cutting(self) -> bool:
        return self.mode != "rapid"


@dataclass(frozen=True)
class Dwell:
    line: int
    seconds: float


@dataclass(frozen=True)
class ToolChange:
    line: int
    tool: int | None


@dataclass(frozen=True)
class Spindle:
    line: int
    on: bool
    rpm: float | None
    code: str


@dataclass(frozen=True)
class Stop:
    line: int
    code: str


@dataclass(frozen=True)
class Comment:
    line: int
    text: str


Event = Motion | Dwell | ToolChange | Spindle | Stop | Comment


@dataclass
class _State:
    pos: list[float] = field(default_factory=lambda: [UNKNOWN_XY, UNKNOWN_XY, UNKNOWN_Z])
    motion: str | None = None
    absolute: bool = True
    scale: float = 1.0  # mm per program unit
    plane: Plane = "xy"
    feed: float | None = None
    rpm: float | None = None
    spindle_on: bool = False
    tool: int | None = None
    next_tool: int | None = None


_MOTION_CODES = {"G0": "rapid", "G1": "feed", "G2": "cw", "G3": "ccw"}
_PLANE_CODES: dict[str, Plane] = {"G17": "xy", "G18": "zx", "G19": "yz"}
# Codes whose axis words are not a motion (offsets, canned cycles): skipped, not modeled.
_AXES_NOT_MOTION = {"G10", "G92", "G43", "G43.1", *(f"G{n}" for n in range(73, 90))}


def interpret(lines: Iterable[Line]) -> Iterator[Event]:
    """Yield the events of a program in execution order. Positions are in mm."""
    st = _State()
    for ln in lines:
        for text in ln.comments:
            yield Comment(ln.number, text)
        gm = {w.code for w in ln.words if w.letter in "GM"}
        if "G20" in gm:
            st.scale = 25.4
        if "G21" in gm:
            st.scale = 1.0
        if (f := ln.get("F")) is not None:
            st.feed = f.value * st.scale
        if (s := ln.get("S")) is not None:
            st.rpm = s.value
        if (t := ln.get("T")) is not None:
            st.next_tool = int(t.value)
        if "M6" in gm:
            st.tool = st.next_tool
            st.spindle_on = False
            yield ToolChange(ln.number, st.tool)
        for code in ("M3", "M4", "M5"):
            if code in gm:
                st.spindle_on = code != "M5"
                yield Spindle(ln.number, st.spindle_on, st.rpm, code)
        if "G4" in gm:
            p = ln.get("P")
            yield Dwell(ln.number, p.value if p else 0.0)
        for code, plane in _PLANE_CODES.items():
            if code in gm:
                st.plane = plane
        if "G90" in gm:
            st.absolute = True
        if "G91" in gm:
            st.absolute = False
        for code, mode in _MOTION_CODES.items():
            if code in gm:
                st.motion = mode
        axes = ln.axes
        if "G28" in gm:
            start = tuple(st.pos)
            st.pos = [UNKNOWN_XY, UNKNOWN_XY, UNKNOWN_Z]
            yield Motion(ln.number, "rapid", start, tuple(st.pos), machine=True)
        elif axes and "G4" not in gm and not gm & _AXES_NOT_MOTION:
            yield from _motion(ln, st, axes, machine="G53" in gm)
        for code in ("M0", "M1", "M2", "M30"):
            if code in gm:
                yield Stop(ln.number, code)


def _motion(ln: Line, st: _State, axes: dict[str, float], machine: bool) -> Iterator[Motion]:
    start: Vec3 = (st.pos[0], st.pos[1], st.pos[2])
    mode = "rapid" if machine else st.motion
    if mode is None:
        raise GcodeSyntaxError(f"line {ln.number}: axis words without a motion mode")
    end = list(start)
    for k, letter in enumerate("XYZ"):
        if letter not in axes:
            continue
        v = axes[letter] * st.scale
        if machine:
            end[k] = UNKNOWN_Z if letter == "Z" else UNKNOWN_XY
        else:
            end[k] = v if st.absolute else end[k] + v
    center = None
    if mode in ("cw", "ccw"):
        center = _arc_center(ln, st, start, tuple(end))
    st.pos = end
    yield Motion(
        ln.number,
        mode,
        start,
        (end[0], end[1], end[2]),
        feed=st.feed,
        plane=st.plane,
        center=center,
        spindle_on=st.spindle_on,
        tool=st.tool,
        machine=machine,
    )


def _arc_center(ln: Line, st: _State, start: Vec3, end: Vec3) -> Vec3:
    a, b, _ = PLANE_AXES[st.plane]
    c = list(start)
    r = ln.get("R")
    if r is None:
        for k in (a, b):
            off = ln.get(_OFFSET_LETTERS[k])
            c[k] = start[k] + (off.value * st.scale if off else 0.0)
        return (c[0], c[1], c[2])
    # R form: center on the chord's perpendicular bisector; R < 0 selects the long arc.
    radius = r.value * st.scale
    dx, dy = end[a] - start[a], end[b] - start[b]
    d = math.hypot(dx, dy)
    if d == 0 or abs(radius) < d / 2 - 1e-9:
        raise GcodeSyntaxError(f"line {ln.number}: R arc cannot reach its end point")
    h = math.sqrt(max(radius * radius - d * d / 4, 0.0))
    sign = 1.0 if (st.motion == "ccw") == (radius > 0) else -1.0
    c[a] = start[a] + dx / 2 - sign * h * dy / d
    c[b] = start[b] + dy / 2 + sign * h * dx / d
    return (c[0], c[1], c[2])


def arc_sweep(m: Motion) -> float:
    """Signed-free sweep of an arc motion in radians (0, 2π]; a closed arc is 2π."""
    a, b, _ = PLANE_AXES[m.plane]
    assert m.center is not None
    t0 = math.atan2(m.start[b] - m.center[b], m.start[a] - m.center[a])
    t1 = math.atan2(m.end[b] - m.center[b], m.end[a] - m.center[a])
    sweep = (t0 - t1) if m.mode == "cw" else (t1 - t0)
    sweep %= 2 * math.pi
    return sweep if sweep > 1e-9 else 2 * math.pi


def arc_points(m: Motion, tol: float = 0.01) -> list[Vec3]:
    """Points along an arc (excluding the start) with chord sagitta ≤ `tol`."""
    a, b, n = PLANE_AXES[m.plane]
    assert m.center is not None
    r = math.hypot(m.start[a] - m.center[a], m.start[b] - m.center[b])
    sweep = arc_sweep(m)
    step = 2 * math.acos(max(-1.0, 1 - tol / r)) if r > tol else math.pi / 4
    count = max(1, math.ceil(sweep / max(step, 1e-6)))
    t0 = math.atan2(m.start[b] - m.center[b], m.start[a] - m.center[a])
    sign = -1.0 if m.mode == "cw" else 1.0
    pts: list[Vec3] = []
    for k in range(1, count + 1):
        t = t0 + sign * sweep * k / count
        p = [0.0, 0.0, 0.0]
        p[a] = m.center[a] + r * math.cos(t)
        p[b] = m.center[b] + r * math.sin(t)
        p[n] = m.start[n] + (m.end[n] - m.start[n]) * k / count
        pts.append((p[0], p[1], p[2]))
    pts[-1] = m.end
    return pts


def motion_length(m: Motion) -> float:
    """Path length in mm (0 for motions touching unknown coordinates)."""
    if m.machine or not all(map(math.isfinite, (*m.start, *m.end))):
        return 0.0
    if not m.is_arc:
        return math.dist(m.start, m.end)
    a, b, n = PLANE_AXES[m.plane]
    assert m.center is not None
    r = math.hypot(m.start[a] - m.center[a], m.start[b] - m.center[b])
    return math.hypot(r * arc_sweep(m), m.end[n] - m.start[n])
