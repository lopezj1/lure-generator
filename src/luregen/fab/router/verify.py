"""Static checks on a G-code file before it goes to the machine.

Errors: syntax, forbidden or unsupported codes, over-long lines, rapids below safe Z (pure Z
moves excepted), cutting with the spindle off or without a feed rate, cuts below the stock
bottom or outside the stock (plus the tool radius), an envelope larger than the machine
travel, G53 Z beyond the Z travel, and arcs whose end radius differs from the start radius
(GRBL error 33). The report also carries a runtime estimate.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Literal

from luregen.fab.gcode import (
    PLANE_AXES,
    Comment,
    Dwell,
    GcodeSyntaxError,
    Line,
    Motion,
    Spindle,
    Stop,
    ToolChange,
    arc_points,
    interpret,
    motion_length,
    tokenize_line,
)
from luregen.fab.router.post import code_matches
from luregen.fab.router.post_config import MachineConfig, PostConfig

Vec3 = tuple[float, float, float]
_STOCK = re.compile(r"^\s*stock(Min|Max)\s*:\s*([-\d.]+)\s*mm\s*,\s*([-\d.]+)\s*mm\s*,\s*([-\d.]+)")
TOOL_MILL = re.compile(r"^\s*TOOL/MILL\s*,\s*([\d.]+)\s*,\s*([\d.]+)", re.I)
_EPS = 1e-6


@dataclass(frozen=True)
class Issue:
    code: str
    line: int | None
    message: str
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line else "file"
        return f"{self.severity}: [{self.code}] {where}: {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    runtime_s: float = 0.0
    envelope: tuple[Vec3, Vec3] | None = None
    stock: tuple[Vec3, Vec3] | None = None
    tool_changes: int = 0

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def summary(self) -> str:
        m, s = divmod(round(self.runtime_s), 60)
        head = f"{len(self.errors)} error(s), {len(self.issues) - len(self.errors)} warning(s); "
        head += f"est. runtime {m // 60}:{m % 60:02d}:{s:02d}, {self.tool_changes} tool change(s)"
        return "\n".join([head, *map(str, self.issues)])


def stock_from_header(lines: list[Line]) -> tuple[Vec3, Vec3] | None:
    """Read Carbide-style `(stockMin:…)` / `(stockMax:…)` comments."""
    found: dict[str, Vec3] = {}
    for ln in lines[:40]:
        for c in ln.comments:
            if m := _STOCK.match(c):
                found[m.group(1).lower()] = (float(m[2]), float(m[3]), float(m[4]))
    if "min" in found and "max" in found:
        return found["min"], found["max"]
    return None


def _parse(text: str, report: Report) -> list[Line]:
    lines = []
    for n, raw in enumerate(text.splitlines(), start=1):
        try:
            lines.append(tokenize_line(raw, n))
        except GcodeSyntaxError as e:
            report.issues.append(Issue("syntax", n, str(e)))
    return lines


def _finite(p: Vec3) -> bool:
    return all(map(math.isfinite, p))


def verify(
    text: str,
    post: PostConfig,
    machine: MachineConfig | None = None,
    stock: tuple[Vec3, Vec3] | None = None,
    safe_z: float | None = None,
    below_stock_mm: float = 0.5,
) -> Report:
    """Check `text`. Stock defaults to the header comments; safe Z to stock top + 1 mm."""
    report = Report()
    lines = _parse(text, report)
    _check_words(lines, post, machine, report)
    report.stock = stock or stock_from_header(lines)
    if safe_z is None:
        safe_z = (report.stock[1][2] if report.stock else 0.0) + 1.0
    _check_motion(lines, machine, report, safe_z, below_stock_mm)
    return report


def _check_words(
    lines: list[Line], post: PostConfig, machine: MachineConfig | None, report: Report
) -> None:
    supported = set(machine.controller.supported) if machine else set()
    ended = False
    for ln in lines:
        if len(ln.raw) > post.max_line_length:
            report.issues.append(
                Issue("line_length", ln.number, f"{len(ln.raw)} > {post.max_line_length} chars")
            )
        for w in ln.words:
            if w.letter not in "GM":
                continue
            if any(code_matches(w.code, f) for f in post.forbid):
                report.issues.append(Issue("forbidden", ln.number, f"forbidden code {w.code}"))
            elif supported and w.code not in supported:
                report.issues.append(Issue("unsupported", ln.number, f"unsupported {w.code}"))
            ended |= w.code in ("M2", "M30")
    if not ended:
        report.issues.append(Issue("no_end", None, "no M2/M30 program end", "warning"))


def _check_motion(
    lines: list[Line],
    machine: MachineConfig | None,
    report: Report,
    safe_z: float,
    below_stock_mm: float,
) -> None:
    rates = machine.rapid_mm_min if machine else (5000.0, 5000.0, 2500.0)
    tool_radius, max_radius = 0.0, 0.0
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    stock = report.stock
    try:
        events = list(interpret(lines))
    except GcodeSyntaxError as e:
        report.issues.append(Issue("syntax", None, str(e)))
        return
    for ev in events:
        match ev:
            case Comment(text=t) if m := TOOL_MILL.match(t):
                tool_radius = float(m[1]) / 2
                max_radius = max(max_radius, tool_radius)
            case ToolChange():
                report.tool_changes += 1
                if machine:
                    report.runtime_s += machine.tool_change_s
            case Dwell(seconds=s):
                report.runtime_s += s
            case Motion() as mv:
                if mv.machine:
                    _check_machine_move(mv, lines, machine, report)
                    continue
                pts = arc_points(mv, 0.05) if mv.is_arc and _finite(mv.start) else [mv.end]
                for p in pts:
                    for k in range(3):
                        if math.isfinite(p[k]):
                            lo[k], hi[k] = min(lo[k], p[k]), max(hi[k], p[k])
                _check_one(mv, pts, report, safe_z, stock, below_stock_mm, tool_radius)
                report.runtime_s += _duration(mv, rates)
            case Spindle() | Stop() | Comment():
                pass
    if all(math.isfinite(v) for v in (*lo, *hi)):
        report.envelope = ((lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2]))
        if machine:
            travel = (machine.travel.x, machine.travel.y, machine.travel.z)
            for k, axis in enumerate("XYZ"):
                if hi[k] - lo[k] > travel[k] + _EPS:
                    report.issues.append(
                        Issue("travel", None, f"{axis} span {hi[k] - lo[k]:.1f} > {travel[k]}")
                    )


def _check_machine_move(
    mv: Motion, lines: list[Line], machine: MachineConfig | None, report: Report
) -> None:
    z = lines_by_number(lines, mv.line).axes.get("Z")
    if machine and z is not None and not (-machine.travel.z <= z <= 0):
        report.issues.append(Issue("travel", mv.line, f"G53 Z{z} outside 0..-{machine.travel.z}"))


def lines_by_number(lines: list[Line], number: int) -> Line:
    for ln in lines:
        if ln.number == number:
            return ln
    raise KeyError(number)


def _check_one(mv, pts, report, safe_z, stock, below_stock_mm, tool_radius) -> None:
    add = report.issues.append
    if mv.mode == "rapid":
        xy_moves = any(
            not (math.isfinite(a) and math.isfinite(b) and abs(a - b) < _EPS)
            for a, b in zip(mv.start[:2], mv.end[:2], strict=True)
        )
        low = min(mv.start[2], mv.end[2])
        if xy_moves and low < safe_z - _EPS:
            add(Issue("rapid_low", mv.line, f"XY rapid at Z{low:.3f} below safe Z{safe_z:.3f}"))
    else:
        if not mv.spindle_on:
            add(Issue("spindle_off", mv.line, "cutting move with the spindle off"))
        if not mv.feed:
            add(Issue("no_feed", mv.line, "cutting move without a feed rate"))
        if mv.is_arc and _finite(mv.start):
            a, b, _ = PLANE_AXES[mv.plane]
            c = mv.center
            r0 = math.hypot(mv.start[a] - c[a], mv.start[b] - c[b])
            r1 = math.hypot(mv.end[a] - c[a], mv.end[b] - c[b])
            if abs(r0 - r1) > 0.005 and abs(r0 - r1) > 0.001 * r0:
                add(Issue("arc_radius", mv.line, f"arc radii differ: {r0:.4f} vs {r1:.4f}"))
    if stock is None:
        return
    (x0, y0, z0), (x1, y1, _) = stock
    margin = tool_radius + 0.5
    for p in pts:
        if not _finite(p):
            continue
        if p[2] < z0 - below_stock_mm - _EPS:
            add(Issue("below_stock", mv.line, f"Z{p[2]:.3f} below stock bottom {z0:.3f}"))
            break
        if not (x0 - margin <= p[0] <= x1 + margin and y0 - margin <= p[1] <= y1 + margin):
            add(Issue("outside_stock", mv.line, f"X{p[0]:.3f} Y{p[1]:.3f} outside the stock"))
            break


def _duration(mv: Motion, rates) -> float:
    if not (_finite(mv.start) and _finite(mv.end)):
        return 0.0
    if mv.mode == "rapid":
        return max(abs(e - s) / r for s, e, r in zip(mv.start, mv.end, rates, strict=True)) * 60
    if not mv.feed:
        return 0.0
    # GRBL slows a feed move so no axis exceeds its max rate (the rapid rates, $110-$112).
    capped = max(abs(e - s) / r for s, e, r in zip(mv.start, mv.end, rates, strict=True))
    return max(motion_length(mv) / mv.feed, capped) * 60
