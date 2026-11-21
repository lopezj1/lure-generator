"""DFM rule registry: `rule(model, cfg) -> list[Violation]`, tagged by process, body
style and family.

A rule module decorates its check functions with `@rule(...)`; `evaluate` runs every rule
whose tags match the config. A rule's severity is the default for the violations it
returns: `error` fails the build, `warn` goes in the report.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from luregen.config import LureConfig
    from luregen.dfm.model import DfmModel

Severity = Literal["error", "warn"]
RuleFn = Callable[["DfmModel", "LureConfig"], list["Violation"]]


@dataclass(frozen=True)
class Violation:
    """One rule finding. `rule` and `severity` default to the rule's own when left empty."""

    message: str
    section: str | None = None
    value: float | None = None
    limit: float | None = None
    unit: str = ""
    rule: str = ""
    severity: Severity | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("value", "limit"):
            if d[k] is not None:
                d[k] = round(float(d[k]), 4)
        return d


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    summary: str
    fn: RuleFn
    process: tuple[str, ...] = ()  # empty = any
    body_style: tuple[str, ...] = ()
    family: tuple[str, ...] = ()

    def applies(self, cfg: LureConfig) -> bool:
        return (
            (not self.process or cfg.process.method in self.process)
            and (not self.body_style or cfg.process.body_style in self.body_style)
            and (not self.family or cfg.family in self.family)
        )


REGISTRY: dict[str, Rule] = {}


def rule(
    rule_id: str,
    *,
    severity: Severity,
    summary: str,
    process: Iterable[str] = (),
    body_style: Iterable[str] = (),
    family: Iterable[str] = (),
) -> Callable[[RuleFn], RuleFn]:
    """Register a DFM check under `rule_id` (decorator)."""

    def deco(fn: RuleFn) -> RuleFn:
        if rule_id in REGISTRY and REGISTRY[rule_id].fn is not fn:
            raise ValueError(f"duplicate DFM rule id {rule_id!r}")
        REGISTRY[rule_id] = Rule(
            rule_id, severity, summary, fn, tuple(process), tuple(body_style), tuple(family)
        )
        return fn

    return deco


def load_rules() -> dict[str, Rule]:
    """Import the rule modules (they register on import) and return the registry."""
    from luregen.dfm import cnc_rules, fin_rules, print_rules  # noqa: F401

    return REGISTRY


@dataclass
class DfmResult:
    checked: list[str] = field(default_factory=list)  # rule ids evaluated
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warn"]

    @property
    def status(self) -> str:
        return "fail" if self.errors else ("warn" if self.warnings else "pass")

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "n_errors": len(self.errors),
            "n_warnings": len(self.warnings),
            "checked": self.checked,
            "violations": [v.as_dict() for v in self.violations],
        }


def run_rule(r: Rule, model: DfmModel, cfg: LureConfig) -> list[Violation]:
    """Run one rule; a crash in a rule is reported as an error of that rule."""
    try:
        found = r.fn(model, cfg) or []
    except Exception as exc:  # a rule that cannot evaluate must not pass silently
        found = [Violation(f"rule could not be evaluated: {type(exc).__name__}: {exc}")]
    return [replace(v, rule=v.rule or r.id, severity=v.severity or r.severity) for v in found]


def evaluate(
    model: DfmModel,
    cfg: LureConfig,
    only: Iterable[str] | None = None,
    emit: Callable[[float, str], None] | None = None,
) -> DfmResult:
    """Run every registered rule that applies to `cfg` (or just the ids in `only`)."""
    rules = [r for r in load_rules().values() if r.applies(cfg)]
    if only is not None:
        wanted = set(only)
        rules = [r for r in rules if r.id in wanted]
    result = DfmResult()
    for i, r in enumerate(rules):
        if emit:
            emit(i / max(len(rules), 1), r.id)
        result.checked.append(r.id)
        result.violations += run_rule(r, model, cfg)
    return result
