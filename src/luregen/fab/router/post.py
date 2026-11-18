"""Post-processor: toolpath IR + `post.toml` → G-code text for GRBL-style controllers.

What it handles beyond plain formatting:
- Modal suppression of motion words, unchanged axes and feed.
- Arcs: start and center are rounded to the output grid, each end point is snapped to the
  grid point closest to the circle (GRBL rejects arcs whose start and end radii differ,
  error 33). Arcs sweep at most `max_sweep_deg` per block, and tiny ones are linearized.
- Peck drilling expanded into G0/G1 moves (no canned cycles on Carbide Motion).
- Tool changes, spindle start with a spin-up dwell, sanitized and length-limited comments.
- A final guard that rejects forbidden codes and over-long lines.
"""

import math
import re

from luregen.fab.gcode import format_code_number, tokenize_line
from luregen.fab.router.ir import (
    Arc,
    Comment,
    Drill,
    Dwell,
    Feed,
    Operation,
    Pause,
    Program,
    Rapid,
    Tool,
)
from luregen.fab.router.post_config import MachineConfig, PostConfig

AXES = ("x", "y", "z")
_OFFSET_LETTER = {"x": "I", "y": "J", "z": "K"}
_PLANE_CODE = {"xy": "G17", "zx": "G18", "yz": "G19"}
_ARC_RADIUS_TOL = 0.01  # mm; an IR arc whose end is further off its circle is a CAM bug


class PostError(ValueError):
    pass


def format_number(value: float, decimals: int, trim: bool = False) -> str:
    text = f"{value:.{decimals}f}"
    if trim and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.lstrip("-") if float(text) == 0 else text


def sanitize_comment(text: str, cfg: PostConfig) -> str:
    """Return `(text)` with disallowed characters replaced and the line length capped."""
    c = cfg.comments
    clean = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in c.allowed) else c.replacement for ch in text
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    return f"({clean[: c.max_length - 2].rstrip()})"


def code_matches(code: str, pattern: str) -> bool:
    """`G43` matches `G43` and `G43.1`; `G1` does not match `G10`."""
    return code == pattern or code.startswith(pattern + ".")


class _Writer:
    def __init__(self, cfg: PostConfig, program: Program, machine: MachineConfig | None):
        self.cfg = cfg
        self.program = program
        self.machine = machine
        self.lines: list[str] = []
        self.pos: dict[str, float | None] = dict.fromkeys(AXES)
        self.motion: str | None = None
        self.feed: str | None = None
        self.plane = "xy"
        self.q = 10.0**-cfg.decimals.xyz  # output grid step

    # --- formatting ---------------------------------------------------------------------

    def num(self, letter: str, value: float) -> str:
        d = self.cfg.decimals
        return format_number(value, d.for_letter(letter), d.trim_zeros)

    def grid(self, value: float) -> float:
        return float(format_number(value, self.cfg.decimals.xyz))

    def emit(self, words: list[str]) -> None:
        if words:
            self.lines.append(self.cfg.word_separator.join(words))

    def comment(self, text: str) -> None:
        self.lines.append(sanitize_comment(text, self.cfg))

    def template(self, lines: list[str], tokens: dict[str, str]) -> None:
        """Render template lines: substitute tokens, re-emit words, track modal state."""
        for raw in lines:
            line = tokenize_line(raw.format_map(tokens))
            for text in line.comments:
                self.comment(text)
            words: list[str] = []
            machine = any(w.code == "G53" for w in line.words if w.letter == "G")
            for w in line.words:
                if w.letter in "GMN":
                    words.append(w.letter + w.text)
                    if w.code in ("G0", "G1", "G2", "G3"):
                        self.motion = w.code
                    if w.code in ("G17", "G18", "G19"):
                        self.plane = {"G17": "xy", "G18": "zx", "G19": "yz"}[w.code]
                elif w.letter == "T":
                    words.append(f"T{int(w.value)}")
                else:
                    words.append(w.letter + self.num(w.letter, w.value))
                    axis = w.letter.lower()
                    if axis in self.pos:
                        self.pos[axis] = None if machine else self.grid(w.value)
                    if w.letter == "F":
                        self.feed = self.num("F", w.value)
            self.emit(words)

    # --- moves --------------------------------------------------------------------------

    def _motion_word(self, code: str) -> list[str]:
        if self.cfg.modal.motion and self.motion == code:
            return []
        self.motion = code
        return [code]

    def _feed_word(self, f: float) -> list[str]:
        text = self.num("F", f)
        if self.cfg.modal.feed and self.feed == text:
            return []
        self.feed = text
        return ["F" + text]

    def _axis_words(self, target: dict[str, float | None]) -> list[str]:
        words = []
        for axis in AXES:
            v = target.get(axis)
            if v is None:
                continue
            v = self.grid(v)
            if self.cfg.modal.axes and self.pos[axis] == v:
                continue
            self.pos[axis] = v
            words.append(axis.upper() + self.num("X", v))
        return words

    def linear(self, code: str, target: dict[str, float | None], f: float | None = None) -> None:
        axes = self._axis_words(target)
        if not axes:
            return
        words = self._motion_word(code) + axes
        if code != "G0":
            assert f is not None
            words += self._feed_word(f)
        self.emit(words)

    def arc(self, mv: Arc) -> None:
        a, b = mv.plane_coords()
        (n,) = set(AXES) - {a, b}
        sa, sb = self.pos[a], self.pos[b]
        if sa is None or sb is None:
            raise PostError("arc starts from an unknown position; add a rapid first")
        ca, cb = self.grid(getattr(mv, "c" + a)), self.grid(getattr(mv, "c" + b))
        ea, eb = getattr(mv, a), getattr(mv, b)
        r = math.hypot(sa - ca, sb - cb)
        r_end = math.hypot(ea - ca, eb - cb)
        if abs(r_end - r) > max(_ARC_RADIUS_TOL, 1e-3 * r):
            raise PostError(f"arc end is {abs(r_end - r):.4f} mm off its circle (r={r:.3f})")
        t0 = math.atan2(sb - cb, sa - ca)
        t1 = math.atan2(eb - cb, ea - ca)
        sweep = (t0 - t1) if mv.cw else (t1 - t0)
        sweep %= 2 * math.pi
        if sweep < 1e-9:
            sweep = 2 * math.pi if math.hypot(ea - sa, eb - sb) < self.q else 0.0
        if sweep == 0.0:
            return
        sn = self.pos[n]
        en = getattr(mv, n)
        arcs = self.cfg.arcs
        sign = -1.0 if mv.cw else 1.0
        pieces = max(1, math.ceil(sweep / math.radians(arcs.max_sweep_deg) - 1e-9))
        for k in range(1, pieces + 1):
            frac = k / pieces
            t = t0 + sign * sweep * frac
            pa, pb = self._snap(ca + r * math.cos(t), cb + r * math.sin(t), ca, cb, r)
            pn = None if en is None else (en if sn is None else sn + (en - sn) * frac)
            start = (self.pos[a], self.pos[b])
            chord = math.hypot(pa - start[0], pb - start[1])
            ok = arcs.enabled and mv.plane in arcs.planes
            if not ok or r < arcs.min_radius or chord < arcs.min_chord:
                t_prev = t0 + sign * sweep * (k - 1) / pieces
                self._linearize((a, b, n), (ca, cb), r, (t_prev, t), pn, (pa, pb), mv.f)
                continue
            self._arc_block(mv, a, b, n, (ca, cb), (pa, pb), pn)

    def _snap(self, x: float, y: float, ca: float, cb: float, r: float) -> tuple[float, float]:
        """Grid point near (x, y) whose distance to the center best matches `r`."""
        if not self.cfg.arcs.snap_endpoint:
            return self.grid(x), self.grid(y)
        gx, gy = self.grid(x), self.grid(y)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                px, py = self.grid(gx + dx * self.q), self.grid(gy + dy * self.q)
                key = (round(abs(math.hypot(px - ca, py - cb) - r), 12), math.hypot(px - x, py - y))
                if best is None or key < best[0]:
                    best = (key, px, py)
        assert best is not None
        return best[1], best[2]

    def _arc_block(self, mv: Arc, a: str, b: str, n: str, c: tuple, end: tuple, pn) -> None:
        if self.plane != mv.plane:
            self.emit([_PLANE_CODE[mv.plane]])
            self.plane = mv.plane
        start = {a: self.pos[a], b: self.pos[b]}
        code = "G2" if mv.cw else "G3"
        words = [code] if (not self.cfg.modal.motion or self.motion != code) else []
        self.motion = code
        target = {a: end[0], b: end[1], n: pn}
        # In-plane end words are always written: an arc block without them is a full circle.
        for axis in AXES:
            v = target[axis]
            if v is None:
                continue
            if axis == n and self.cfg.modal.axes and self.pos[n] == self.grid(v):
                continue
            words.append(axis.upper() + self.num("X", v))
            self.pos[axis] = self.grid(v)
        for axis, cv in ((a, c[0]), (b, c[1])):
            words.append(_OFFSET_LETTER[axis] + self.num("I", cv - start[axis]))
        words += self._feed_word(mv.f)
        self.emit(words)

    def _linearize(self, axes, c, r, angles, pn, end, f) -> None:
        """G1 chords along the arc from angle t_start to t_end, ending exactly at `end`."""
        a, b, n = axes
        t_start, dt = angles[0], angles[1] - angles[0]
        tol = self.cfg.arcs.linearize_tol
        step = 2 * math.acos(max(-1.0, 1 - tol / r)) if r > tol else math.pi / 2
        count = max(1, math.ceil(abs(dt) / step))
        sn = self.pos[n]
        for k in range(1, count + 1):
            frac = k / count
            if k == count:
                pa, pb = end
            else:
                t = t_start + dt * frac
                pa, pb = c[0] + r * math.cos(t), c[1] + r * math.sin(t)
            zn = None if pn is None else (pn if sn is None else sn + (pn - sn) * frac)
            self.linear("G1", {a: pa, b: pb, n: zn}, f)

    def drill(self, mv: Drill, safe_z: float) -> None:
        z = self.pos["z"]
        if z is not None and z < safe_z - 1e-9:
            self.linear("G0", {"z": safe_z})
        self.linear("G0", {"x": mv.x, "y": mv.y})
        self.linear("G0", {"z": mv.r_z})
        depth = mv.r_z
        peck = mv.peck or (mv.r_z - mv.bottom_z)
        while depth > mv.bottom_z + 1e-9:
            target = max(depth - peck, mv.bottom_z)
            if depth < mv.r_z:
                self.linear("G0", {"z": depth + self.cfg.drill.clearance})
            self.linear("G1", {"z": target}, mv.f)
            if target <= mv.bottom_z + 1e-9 and mv.dwell_s > 0:
                self.dwell(mv.dwell_s)
            self.linear("G0", {"z": mv.r_z})
            depth = target

    def dwell(self, seconds: float) -> None:
        self.emit(["G4", "P" + self.num("P", seconds)])


def _stock_tokens(program: Program) -> dict[str, str]:
    s = program.setup
    lo, hi = s.stock_min, s.stock_max
    size = [h - low for low, h in zip(lo, hi, strict=True)]
    return {
        "design": program.design,
        "setup": s.name,
        "zero": s.zero,
        "safe_z": format_number(s.safe_z, 3),
        "stock_min": ", ".join(f"{v:.2f}mm" for v in lo),
        "stock_max": ", ".join(f"{v:.2f}mm" for v in hi),
        "stock_block": ", ".join(format_number(v, 2) for v in (*size, *(-v for v in lo))),
    }


def _dial(rpm: float, machine: MachineConfig | None) -> str:
    table = machine.spindle.dial_rpm if machine else []
    if not table:
        return "?"
    best = min(range(len(table)), key=lambda i: abs(table[i] - rpm))
    return str(best + 1)


def _tool_tokens(base: dict[str, str], tool: Tool, op: Operation, cfg: PostConfig) -> dict:
    return base | {
        "tool_number": str(tool.number),
        "tool_id": tool.id,
        "tool_name": tool.name or tool.id,
        "dia": format_number(tool.diameter, 2),
        "corner_r": format_number(tool.corner, 2),
        "op": op.name,
        "rpm": format_number(op.rpm, cfg.decimals.rpm),
    }


def post_program(program: Program, cfg: PostConfig, machine: MachineConfig | None = None) -> str:
    """Render `program` as G-code text (newline-terminated)."""
    w = _Writer(cfg, program, machine)
    base = _stock_tokens(program)
    w.template(cfg.header, base)
    tool: Tool | None = None
    rpm: float | None = None
    safe_z = program.setup.safe_z
    for op in program.operations:
        w.comment(op.name)
        op_tool = program.tools[op.tool]
        tokens = _tool_tokens(base, op_tool, op, cfg)
        if op_tool != tool:
            w.template(cfg.tool_change, tokens)
            tool, rpm = op_tool, None
        if op.rpm != rpm:
            w.template(cfg.spindle.on, tokens | {"dial": _dial(op.rpm, machine)})
            if cfg.spindle.dwell_s > 0:
                w.dwell(cfg.spindle.dwell_s)
            rpm = op.rpm
        w.motion = None  # every operation starts with an explicit motion word
        for mv in op.moves:
            match mv:
                case Rapid():
                    w.linear("G0", {"x": mv.x, "y": mv.y, "z": mv.z})
                case Feed():
                    w.linear("G1", {"x": mv.x, "y": mv.y, "z": mv.z}, mv.f)
                case Arc():
                    w.arc(mv)
                case Drill():
                    w.drill(mv, safe_z)
                case Dwell():
                    w.dwell(mv.seconds)
                case Comment():
                    w.comment(mv.text)
                case Pause():
                    if mv.message:
                        w.comment(mv.message)
                    w.emit(["M0"])
    w.template(cfg.footer, base)
    text = "\n".join(w.lines) + "\n"
    check_output(text, cfg)
    return text


def check_output(text: str, cfg: PostConfig) -> None:
    """Raise `PostError` on forbidden codes or lines longer than allowed."""
    problems = []
    for n, raw in enumerate(text.splitlines(), start=1):
        if len(raw) > cfg.max_line_length:
            problems.append(f"line {n}: {len(raw)} characters > {cfg.max_line_length}")
        for word in tokenize_line(raw, n).words:
            if word.letter not in "GM":
                continue
            code = word.letter + format_code_number(word.value)
            if any(code_matches(code, f) for f in cfg.forbid):
                problems.append(f"line {n}: forbidden code {code}")
    if problems:
        raise PostError("; ".join(problems))
