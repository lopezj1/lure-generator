"""Design-for-manufacture rules (F5): a registry of `rule(model, cfg) -> list[Violation]`
checks tagged by process, body style and family. Errors fail `lure build`; warnings go in
the report's `dfm` block. Thresholds live in `[dfm]` of the config (spec/designs/_common.toml).
"""

from luregen.dfm.registry import REGISTRY, DfmResult, Rule, Violation, evaluate, load_rules, rule

__all__ = ["REGISTRY", "DfmResult", "Rule", "Violation", "evaluate", "load_rules", "rule"]
