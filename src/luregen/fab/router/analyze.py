"""Infer post-processor settings from a reference G-code file (`lure analyze-gcode`).

The result is a proposed `post.toml`: the current config (if any) with every setting the
sample shows overridden. Header comments that carry job data (design file, stock) become
`{tokens}`; anything that looks like a local path is dropped.
"""

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import tomli_w

from luregen.fab.gcode import PLANE_AXES, Line, Motion, interpret, parse
from luregen.fab.router.post_config import PostConfig

# Known Carbide-style header comments → token templates.
_HEADER_COMMENTS = [
    (re.compile(r"^\s*Design File\s*:", re.I), "Design File: {design}"),
    (re.compile(r"^\s*stockMin\s*:", re.I), "stockMin:{stock_min}"),
    (re.compile(r"^\s*stockMax\s*:", re.I), "stockMax:{stock_max}"),
    (re.compile(r"^\s*STOCK/BLOCK\s*,", re.I), "STOCK/BLOCK,{stock_block}"),
]
_TOOL_LIST_COMMENT = re.compile(r"^\s*T\d+\s+D\s*=", re.I)
_PATHLIKE = re.compile(r"[A-Za-z]:[\\/]|\\|/(home|Users)/", re.I)
_TOOL_MILL = re.compile(r"^(\s*TOOL/MILL\s*,)\s*[\d.]+(\s*,\s*)[\d.]+(.*)$", re.I)


@dataclass
class Analysis:
    units: str | None = None
    word_separator: str = ""
    decimals: dict[str, int] = field(default_factory=dict)  # per letter, max seen
    trim_zeros: bool = False
    header: list[str] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)
    arcs: int = 0
    arc_planes: list[str] = field(default_factory=list)
    arc_format: str | None = None
    min_arc_radius: float | None = None
    spindle_on: list[str] = field(default_factory=list)
    spindle_dwell_s: float = 0.0
    tool_change: list[str] = field(default_factory=list)
    tool_change_style: str = "none"  # "M6" | "T only" | "none"
    retract: str | None = None
    retract_style: str = "unknown"  # "G53" | "G28" | "unknown"
    end_code: str | None = None
    comment_style: str = "paren"
    max_comment_length: int = 0
    line_endings: str = "lf"
    notes: list[str] = field(default_factory=list)


def _template(line: Line) -> str:
    """Re-emit a code line with single spaces between words, numbers as written."""
    return " ".join(w.letter + w.text for w in line.words)


def _is_cut_or_move(line: Line) -> bool:
    machine = line.has_code("G53") or line.has_code("G28")
    return bool(line.axes) and not machine


def _header_comment(text: str, notes: list[str]) -> str | None:
    for pattern, template in _HEADER_COMMENTS:
        if pattern.match(text):
            return f"({template})"
    if _TOOL_LIST_COMMENT.match(text):
        notes.append("header tool-list comment omitted (tool data goes in tool_change)")
        return None
    if _PATHLIKE.search(text):
        notes.append("header comment with a file path omitted")
        return None
    return f"({text})"


def _body_start(lines: list[Line]) -> int:
    for i, ln in enumerate(lines):
        gm = {w.code for w in ln.words if w.letter in "GM"}
        if gm & {"M3", "M4", "M5", "M6"} or ln.get("T") or _is_cut_or_move(ln):
            return i
    return len(lines)


def analyze(text: str) -> Analysis:
    a = Analysis()
    a.line_endings = "crlf" if "\r\n" in text else "lf"
    lines = parse(text)
    code_lines = [ln for ln in lines if ln.words]

    gm = Counter(w.code for ln in code_lines for w in ln.words if w.letter in "GM")
    a.units = "mm" if gm["G21"] else ("inch" if gm["G20"] else None)
    if a.units == "inch":
        a.notes.append("sample is in inches; the post only writes mm")

    seps = Counter(ln.separated for ln in code_lines if ln.separated is not None)
    a.word_separator = " " if seps[True] > seps[False] else ""

    per_letter: dict[str, Counter] = {}
    trailing_zero = False
    for ln in code_lines:
        for w in ln.words:
            if w.letter in "XYZIJKFSPR":
                per_letter.setdefault(w.letter, Counter())[w.decimals] += 1
                if w.letter in "XYZIJK" and "." in w.text and w.text.endswith("0"):
                    trailing_zero = True
    a.decimals = {k: max(v) for k, v in sorted(per_letter.items())}
    a.trim_zeros = not trailing_zero and any(a.decimals.get(k, 0) > 0 for k in "XYZ")

    # Header: everything before the first tool/spindle/cutting line, minus trailing comments.
    start = _body_start(lines)
    head = lines[:start]
    while head and not head[-1].words:
        head.pop()
    for ln in head:
        for c in ln.comments:
            if (t := _header_comment(c, a.notes)) is not None:
                a.header.append(t)
        if ln.words:
            a.header.append(_template(ln))

    # Footer: code lines after the last non-machine motion.
    last = max((i for i, ln in enumerate(lines) if _is_cut_or_move(ln)), default=len(lines) - 1)
    a.footer = [_template(ln) for ln in lines[last + 1 :] if ln.words]
    ends = [c for c in ("M30", "M2") if gm[c]]
    a.end_code = ends[0] if ends else None

    _analyze_motion(lines, a)
    _analyze_tooling(lines, a)

    retracts = [ln for ln in code_lines if ln.has_code("G53") and "Z" in ln.axes]
    homes = [ln for ln in code_lines if ln.has_code("G28") and "Z" in ln.axes]
    if retracts:
        a.retract, a.retract_style = _template(retracts[0]), "G53"
    elif homes:
        a.retract, a.retract_style = _template(homes[0]), "G28"
        a.notes.append("retracts use G28 (home); the post prefers G53 G0 Z")

    comments = [c for ln in lines for c in ln.comments]
    a.comment_style = "semicolon" if any(";" in ln.raw for ln in lines) else "paren"
    a.max_comment_length = max((len(c) + 2 for c in comments), default=0)
    return a


def _analyze_motion(lines: list[Line], a: Analysis) -> None:
    arcs = [e for e in interpret(lines) if isinstance(e, Motion) and e.is_arc]
    a.arcs = len(arcs)
    if not arcs:
        return
    a.arc_planes = sorted({m.plane for m in arcs}, key=["xy", "zx", "yz"].index)
    uses_r = any(ln.get("R") for ln in lines if ln.has_code("G2") or ln.has_code("G3"))
    a.arc_format = "r" if uses_r else "ij"
    radii = []
    for m in arcs:
        i, j, _ = PLANE_AXES[m.plane]
        assert m.center is not None
        radii.append(((m.start[i] - m.center[i]) ** 2 + (m.start[j] - m.center[j]) ** 2) ** 0.5)
    a.min_arc_radius = min(radii)


def _analyze_tooling(lines: list[Line], a: Analysis) -> None:
    for i, ln in enumerate(lines):
        if ln.has_code("M3") or ln.has_code("M4"):
            a.spindle_on = [
                " ".join("S{rpm}" if w.letter == "S" else w.letter + w.text for w in ln.words)
            ]
            nxt = next((n for n in lines[i + 1 :] if n.words), None)
            if nxt is not None and nxt.has_code("G4") and nxt.get("P"):
                a.spindle_dwell_s = nxt.get("P").value  # type: ignore[union-attr]
            break
    m6 = next((i for i, ln in enumerate(lines) if ln.has_code("M6")), None)
    if m6 is not None:
        a.tool_change_style = "M6"
        first = m6
        for k in range(m6 - 1, max(m6 - 5, -1), -1):
            prev = lines[k]
            if prev.words and not (prev.has_code("M5") or prev.has_code("G53")):
                break
            first = k
            if prev.has_code("M5"):
                break
        block = []
        for ln in lines[first : m6 + 1]:
            for c in ln.comments:
                block.append("(" + _TOOL_MILL.sub(r"\1{dia}\2{corner_r}\3", c) + ")")
            if ln.words:
                words = [
                    "T{tool_number}" if w.letter == "T" else w.letter + w.text for w in ln.words
                ]
                block.append(" ".join(words))
        a.tool_change = block
    elif any(ln.get("T") for ln in lines):
        a.tool_change_style = "T only"
        a.tool_change = ["T{tool_number}"]
        a.notes.append("tool select without M6: no tool-change prompt and no BitSetter probe")


def propose(a: Analysis, current: PostConfig | None = None) -> PostConfig:
    """Overlay what the sample shows onto `current` (or the defaults)."""
    data: dict[str, Any] = (current or PostConfig()).model_dump()
    data["word_separator"] = a.word_separator
    dec = data["decimals"]

    def pick(letters: str, key: str) -> None:
        seen = [a.decimals[c] for c in letters if c in a.decimals]
        if seen:
            dec[key] = max(seen)

    pick("XYZ", "xyz")
    pick("IJK", "ijk")
    pick("F", "feed")
    pick("S", "rpm")
    pick("P", "dwell")
    dec["trim_zeros"] = a.trim_zeros
    if a.header:
        data["header"] = a.header
    if a.footer:
        data["footer"] = a.footer
    if a.retract:
        data["safe_retract"] = a.retract
    if a.tool_change:
        data["tool_change"] = a.tool_change
    if a.spindle_on:
        data["spindle"]["on"] = a.spindle_on
        data["spindle"]["dwell_s"] = a.spindle_dwell_s
    if a.arcs:
        data["arcs"]["enabled"] = True
        data["arcs"]["planes"] = a.arc_planes
        if a.min_arc_radius is not None and a.min_arc_radius < data["arcs"]["min_radius"]:
            a.notes.append(
                f"sample has arcs down to r={a.min_arc_radius:.3f} mm, "
                f"below min_radius={data['arcs']['min_radius']}"
            )
    else:
        a.notes.append("sample has no arcs; arc settings kept from the current config")
    if a.max_comment_length > data["comments"]["max_length"]:
        a.notes.append(f"sample has comments up to {a.max_comment_length} characters")
    used = {w for line in a.header + a.footer + a.tool_change for w in line.split()}
    clash = sorted(c for c in data["forbid"] if c in used)
    if clash:
        a.notes.append(f"sample templates use forbidden codes {clash}")
    return PostConfig.model_validate(data)


def to_toml(cfg: PostConfig) -> str:
    return tomli_w.dumps(cfg.model_dump(exclude_none=True))


def diff(current: PostConfig | None, proposed: PostConfig, label: str = "post.toml") -> str:
    old = to_toml(current).splitlines(keepends=True) if current else []
    new = to_toml(proposed).splitlines(keepends=True)
    return "".join(difflib.unified_diff(old, new, f"current/{label}", f"proposed/{label}"))


def summary(a: Analysis) -> str:
    sep = "none" if a.word_separator == "" else "space"
    dec = ", ".join(f"{k}={v}" for k, v in a.decimals.items())
    rows = [
        ("units", a.units or "not set"),
        ("word separator", sep),
        ("decimals (max)", dec + (" (trailing zeros trimmed)" if a.trim_zeros else "")),
        ("arcs", f"{a.arcs} ({a.arc_format or '-'}, planes {','.join(a.arc_planes) or '-'})"),
        ("spindle", " / ".join(a.spindle_on) or "-"),
        ("tool change", f"{a.tool_change_style}: {' | '.join(a.tool_change) or '-'}"),
        ("retract", f"{a.retract_style}: {a.retract or '-'}"),
        ("end code", a.end_code or "-"),
        ("comments", f"{a.comment_style}, up to {a.max_comment_length} chars"),
        ("line endings", a.line_endings),
    ]
    out = [f"{k:>15}: {v}" for k, v in rows]
    out += [f"           note: {n}" for n in dict.fromkeys(a.notes)]
    return "\n".join(out)
