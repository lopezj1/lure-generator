"""Fetch and flatten OrcaSlicer system profiles (#31).

Orca system profiles form `inherits` chains (e.g. a process inherits a printer-family common
process, which inherits `fdm_process_common`). Each vendor folder has an index
`resources/profiles/<Vendor>.json` mapping profile names to files. A parent is looked up in
the vendor the child came from first, then in the other source (the vendor or the
`OrcaFilamentLibrary`). The chain is merged root-first and `inherits` dropped, so the result
loads in a CLI that does not resolve inheritance.
"""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from luregen.fab.printer.machine import PROFILE_KINDS, PrinterMachine, SlicerApp

# Fetches a path relative to `resources/profiles/` at the pinned tag and returns its bytes.
Fetcher = Callable[[str], bytes]

RAW_BASE = "https://raw.githubusercontent.com"

# Key Orca writes the preset name into, per profile kind. Filament's is a per-extruder list.
SETTINGS_ID_KEY = {
    "machine": "printer_settings_id",
    "process": "print_settings_id",
    "filament": "filament_settings_id",
}


class ProfileError(RuntimeError):
    pass


def github_fetcher(repo: str, tag: str, timeout: float = 30.0) -> Fetcher:
    """Fetch files from `resources/profiles/` of `repo` at `tag` via raw.githubusercontent.com."""

    def fetch(rel: str) -> bytes:
        url = f"{RAW_BASE}/{repo}/{tag}/resources/profiles/{urllib.parse.quote(rel)}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except OSError as exc:  # URLError and HTTPError are OSErrors
            raise ProfileError(f"fetch failed: {url}: {exc}") from exc

    return fetch


def dir_fetcher(root: Path) -> Fetcher:
    """Read files from a local copy of `resources/profiles/` (tests, offline use)."""

    def fetch(rel: str) -> bytes:
        path = root / rel
        if not path.is_file():
            raise ProfileError(f"not found: {path}")
        return path.read_bytes()

    return fetch


def parse_json(raw: bytes, where: str) -> dict[str, Any]:
    """Decode profile JSON, tolerating the UTF-8 BOM some profile files start with."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"bad JSON in {where}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{where}: expected a JSON object")
    return data


class ProfileSource:
    """Name → file lookup across a vendor folder and the shared filament library."""

    def __init__(self, fetch: Fetcher, vendors: list[str]) -> None:
        self.fetch = fetch
        self.vendors = vendors
        self._index: dict[str, dict[tuple[str, str], str]] = {}

    def index(self, vendor: str) -> dict[tuple[str, str], str]:
        """(kind, name) → sub_path for one vendor; empty if the vendor has no index."""
        if vendor not in self._index:
            try:
                data = parse_json(self.fetch(f"{vendor}.json"), f"{vendor}.json")
            except ProfileError:
                data = {}
            self._index[vendor] = {
                (kind, entry["name"]): entry["sub_path"]
                for kind in PROFILE_KINDS
                for entry in data.get(f"{kind}_list", [])
            }
        return self._index[vendor]

    def find(self, kind: str, name: str, prefer: str) -> tuple[str, str, dict[str, Any]]:
        """Locate profile `name`, trying vendor `prefer` first. Returns (vendor, path, data)."""
        order = [prefer, *[v for v in self.vendors if v != prefer]]
        for vendor in order:
            sub = self.index(vendor).get((kind, name))
            if sub is not None:
                rel = f"{vendor}/{sub}"
                return vendor, rel, parse_json(self.fetch(rel), rel)
        # No index entry: try the conventional file name before giving up.
        for vendor in order:
            rel = f"{vendor}/{kind}/{name}.json"
            try:
                return vendor, rel, parse_json(self.fetch(rel), rel)
            except ProfileError:
                continue
        raise ProfileError(f"{kind} profile {name!r} not found in {order}")


@dataclass(frozen=True)
class FlatProfile:
    kind: str
    name: str
    data: dict[str, Any]
    chain: list[str]  # file paths, leaf first


def resolve_chain(
    source: ProfileSource, kind: str, name: str, vendor: str
) -> list[tuple[str, dict[str, Any]]]:
    """Return [(path, data), …] from the named profile up to its root, leaf first."""
    chain: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    current: str | None = name
    while current:
        if current in seen:
            raise ProfileError(f"inherits cycle at {current!r}")
        seen.add(current)
        vendor, rel, data = source.find(kind, current, vendor)
        chain.append((rel, data))
        current = data.get("inherits") or None
    return chain


def flatten(
    kind: str, chain: list[dict[str, Any]], name: str, drop: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Merge a leaf-first chain root-first; drop `inherits`; set a clean name and settings id.

    `from` stays `system`: the Snapmaker fork's CLI matches `compatible_printers` against the
    printer's own name only for system presets (otherwise against its now-empty `inherits`).
    """
    merged: dict[str, Any] = {}
    for data in reversed(chain):
        merged.update(data)
    for key in ("inherits", *drop):
        merged.pop(key, None)
    merged["type"] = kind
    merged["name"] = name
    id_key = SETTINGS_ID_KEY[kind]
    merged[id_key] = [name] if isinstance(merged.get(id_key), list) else name
    return merged


def flatten_profile(
    source: ProfileSource, kind: str, name: str, vendor: str, drop: tuple[str, ...] = ()
) -> FlatProfile:
    chain = resolve_chain(source, kind, name, vendor)
    data = flatten(kind, [d for _, d in chain], name, drop)
    return FlatProfile(kind=kind, name=data["name"], data=data, chain=[rel for rel, _ in chain])


def flatten_app(app: SlicerApp, fetch: Fetcher) -> dict[str, FlatProfile]:
    """Flatten the machine, process and filament chains of one slicer app."""
    sets = app.profiles
    source = ProfileSource(fetch, [sets.vendor, sets.library])
    return {
        kind: flatten_profile(source, kind, sets.name(kind), sets.vendor, app.drop_keys)
        for kind in PROFILE_KINDS
    }


def dump_profile(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=4, ensure_ascii=False) + "\n"


def write_profiles(
    machine: PrinterMachine, app_key: str, profiles: dict[str, FlatProfile]
) -> list[Path]:
    """Write `<printer>/profiles/<app>/{machine,process,filament}.json` plus `source.json`."""
    app = machine.slicer(app_key)
    out = machine.profile_dir(app_key)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for kind, path in machine.profile_paths(app_key).items():
        path.write_text(dump_profile(profiles[kind].data))
        written.append(path)
    source = {
        "app": app.key,
        "label": app.label,
        "repo": app.repo,
        "tag": app.tag,
        "dropped_keys": list(app.drop_keys),
        "generated_by": "lure sync-profiles",
        "chains": {kind: p.chain for kind, p in profiles.items()},
    }
    (out / "source.json").write_text(dump_profile(source))
    written.append(out / "source.json")
    return written


def sync_profiles(
    machine: PrinterMachine, app_key: str | None = None, fetch: Fetcher | None = None
) -> list[Path]:
    """Fetch, flatten and write one app's profiles. `fetch` defaults to GitHub at the tag."""
    app = machine.slicer(app_key)
    fetch = fetch or github_fetcher(app.repo, app.tag)
    return write_profiles(machine, app.key, flatten_app(app, fetch))
