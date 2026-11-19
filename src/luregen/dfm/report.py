"""Rendering of the report's `dfm` block (report.md and the CLI summary)."""

from __future__ import annotations

from collections.abc import Callable

ICON = {"error": "ERROR", "warn": "warn"}


def dfm_md(dfm: dict) -> list[str]:
    lines = [
        "## DFM",
        "",
        f"Status: **{dfm['status']}** ({dfm['n_errors']} errors, {dfm['n_warnings']} warnings; "
        f"{len(dfm['checked'])} rules checked)",
    ]
    if dfm["violations"]:
        lines += ["", "| Severity | Rule | Section | Finding |", "|---|---|---|---|"]
        for v in dfm["violations"]:
            lines.append(
                f"| {v['severity']} | `{v['rule']}` | {v['section'] or '—'} | {v['message']} |"
            )
    lines += ["", "Rules checked: " + ", ".join(f"`{r}`" for r in dfm["checked"])]
    return lines


def echo_dfm(dfm: dict, echo: Callable[[str], None]) -> None:
    echo(
        f"DFM: {dfm['status']} ({dfm['n_errors']} errors, {dfm['n_warnings']} warnings, "
        f"{len(dfm['checked'])} rules)"
    )
    for v in dfm["violations"]:
        echo(f"  {ICON.get(v['severity'], v['severity'])} {v['rule']}: {v['message']}")
