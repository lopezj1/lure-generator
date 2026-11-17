# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Create epic/feature/task issues from a YAML manifest and link them as sub-issues.

Usage:
    uv run tools/seed_issues.py MANIFEST.yaml [--repo owner/name] [--dry-run]

Each entry has: key, title, labels, body, and optional parent (a key that appears earlier).
`{{key}}` in a body is replaced with the issue number of an earlier entry.
Progress is recorded in MANIFEST.state.json (key -> {number, id}), so re-running
skips issues that already exist and only adds missing sub-issue links.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


def gh_api(*args: str, stdin: str | None = None) -> dict | list:
    out = subprocess.run(
        ["gh", "api", *args], input=stdin, capture_output=True, text=True
    )
    if out.returncode != 0:
        sys.exit(f"gh api {' '.join(args)} failed:\n{out.stderr}")
    return json.loads(out.stdout) if out.stdout.strip() else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--repo", default="lopezj1/lure-generator")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    entries = yaml.safe_load(a.manifest.read_text())
    state_path = a.manifest.with_suffix(".state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    keys = [e["key"] for e in entries]
    for e in entries:
        p = e.get("parent")
        if p and (p not in keys or keys.index(p) > keys.index(e["key"])):
            sys.exit(f"{e['key']}: parent {p!r} must appear earlier in the manifest")

    for e in entries:
        key = e["key"]
        if key not in state:
            if a.dry_run:
                print(f"would create  {key:6} {e['title']}")
                continue
            body = re.sub(r"\{\{(\w+)\}\}", lambda m: f"#{state[m.group(1)]['number']}", e["body"])
            payload = {"title": e["title"], "body": body, "labels": e["labels"]}
            issue = gh_api(f"repos/{a.repo}/issues", "--input", "-", stdin=json.dumps(payload))
            state[key] = {"number": issue["number"], "id": issue["id"], "linked": False}
            state_path.write_text(json.dumps(state, indent=2))
            print(f"created #{issue['number']:<4} {key:6} {e['title']}")

        parent = e.get("parent")
        if parent and not a.dry_run and not state[key]["linked"]:
            gh_api(
                f"repos/{a.repo}/issues/{state[parent]['number']}/sub_issues",
                "-F", f"sub_issue_id={state[key]['id']}",
            )
            state[key]["linked"] = True
            state_path.write_text(json.dumps(state, indent=2))
            print(f"linked  #{state[key]['number']} under #{state[parent]['number']}")


if __name__ == "__main__":
    main()
