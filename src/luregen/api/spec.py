"""spec/ endpoints: catalogs, variants (read, validate, save), the form schema, git status.

Every path value the API sends or receives (profile.image, …) is relative to the data root;
`load_layered` makes them absolute, so they are relativized before they leave the server, and
written back relative to the variant file.
"""

import copy
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Response
from pydantic import ValidationError

from luregen.api.formschema import extends_chain, postprocess_schema, provenance, restore
from luregen.api.models import (
    DesignSummary,
    FieldError,
    FormSchema,
    Layer,
    MachineSummary,
    Material,
    SaveRequest,
    SaveResult,
    SpecChange,
    SpecStatus,
    VariantDetail,
    VariantSummary,
)
from luregen.api.safety import Name, inside
from luregen.api.tomldiff import rewrite_variant
from luregen.config import (
    PATH_KEYS,
    ConfigError,
    LureConfig,
    absolutize_paths,
    load_config,
    load_layered,
    relative_path,
    relativize_paths,
    resolve,
)
from luregen.config.resolve import load_materials

_CONFLICT = {"description": "The file changed since it was read (etag mismatch)"}
_INVALID = {"description": "Config errors: `detail` is a list of {path, message}"}


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _rel(root: Path, value: str | Path) -> str:
    return relative_path(value, root)


def _paths_to_root(data: dict[str, Any], root: Path) -> dict[str, Any]:
    """Absolute path values → relative to the data root (never leak home paths)."""
    return relativize_paths(data, root)


def _label(index: int, file: Path) -> str:
    if index == 0:
        return "variant"
    return {"family.toml": "family", "_common.toml": "common"}.get(file.name, file.stem)


def _field_errors(exc: ValidationError) -> list[FieldError]:
    out = []
    for err in exc.errors():
        loc = [p for p in err["loc"] if not (isinstance(p, str) and p.startswith("function-"))]
        out.append(FieldError(path=loc, message=err["msg"].removeprefix("Value error, ")))
    return out


def spec_router(root: Path) -> APIRouter:
    router = APIRouter(tags=["spec"])
    spec = root / "spec"
    designs = spec / "designs"
    save_lock = threading.Lock()

    def variant_file(family: str, name: str) -> Path:
        path = inside(spec, designs / family / "variants" / f"{name}.toml")
        if not path.is_file():
            raise HTTPException(404, f"no variant {family}/{name}")
        return path

    def resolved_of(data: dict[str, Any]) -> dict[str, Any]:
        return resolve(LureConfig.model_validate(data), spec).model_dump(mode="json")

    def check(config: dict[str, Any]) -> tuple[dict[str, Any], list[FieldError]]:
        """Form output → (absolute-path data, errors)."""
        data = absolutize_paths(copy.deepcopy(restore(config)), root)
        data.pop("extends", None)  # layering is the file's business, not the form's
        errors = [
            FieldError(path=[section, key], message="path must stay inside the data root")
            for section, key in PATH_KEYS
            if isinstance(table := data.get(section), dict)
            and isinstance(v := table.get(key), str)
            and not Path(v).is_relative_to(root)
        ]
        try:
            resolve(LureConfig.model_validate(data), spec)
        except ValidationError as exc:
            errors += _field_errors(exc)
        except (ConfigError, KeyError) as exc:
            errors.append(FieldError(path=[], message=str(exc)))
        return data, errors

    def detail(family: str, name: str, path: Path) -> VariantDetail:
        raw = path.read_bytes()
        own = absolutize_paths(tomllib.loads(raw.decode()), path.parent)
        try:
            layered = load_layered(path)
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        resolved, errors = None, []
        try:
            resolved = restore(load_config(path).model_dump(mode="json"))
        except ConfigError as exc:
            if isinstance(exc.__cause__, ValidationError):
                errors = _field_errors(exc.__cause__)
            else:
                errors = [FieldError(path=[], message=str(exc))]
        chain = extends_chain(path)
        inherited = load_layered(chain[1]) if len(chain) > 1 else {}
        return VariantDetail(
            family=family,
            name=name,
            path=_rel(root, path),
            etag=_etag(raw),
            own=_paths_to_root(own, root),
            layered=_paths_to_root(layered, root),
            inherited=_paths_to_root(inherited, root),
            resolved=_paths_to_root(resolved, root) if resolved else None,
            errors=errors,
            layers=[Layer(label=_label(i, f), path=_rel(root, f)) for i, f in enumerate(chain)],
            provenance=provenance(chain),
        )

    @router.get("/designs")
    def list_designs() -> list[DesignSummary]:
        out = []
        for family in sorted(designs.glob("*/family.toml")):
            if family.parent.name.startswith((".", "_")):
                continue
            variants = sorted(
                p.stem
                for p in (family.parent / "variants").glob("*.toml")
                if not p.name.startswith(".")
            )
            out.append(
                DesignSummary(family=family.parent.name, path=_rel(root, family), variants=variants)
            )
        return out

    @router.get("/designs/{family}/variants")
    def list_variants(family: Name) -> list[VariantSummary]:
        folder = inside(designs, designs / family)
        if not (folder / "family.toml").is_file():
            raise HTTPException(404, f"no design family {family!r}")
        out = []
        for path in sorted((folder / "variants").glob("*.toml")):
            if path.name.startswith("."):
                continue
            try:
                data = load_layered(path)
            except (ConfigError, tomllib.TOMLDecodeError):
                data = {}
            process = data.get("process") if isinstance(data.get("process"), dict) else {}
            out.append(
                VariantSummary(
                    name=path.stem,
                    path=_rel(root, path),
                    method=process.get("method"),
                    material=process.get("material"),
                )
            )
        return out

    @router.get("/designs/{family}/presets")
    def list_presets(family: Name) -> list[str]:
        """Station-table CSVs under the family's profiles/ folder (relative to the data root)."""
        folder = inside(designs, designs / family)
        if not (folder / "family.toml").is_file():
            raise HTTPException(404, f"no design family {family!r}")
        return sorted(_rel(root, p) for p in (folder / "profiles").rglob("*.csv"))

    @router.get("/machines")
    def list_machines() -> list[MachineSummary]:
        out = []
        for kind, folder in (("printer", "printers"), ("router", "routers")):
            for toml in sorted((spec / folder).glob("*/machine.toml")):
                data = tomllib.loads(toml.read_text())
                out.append(
                    MachineSummary(
                        id=toml.parent.name,
                        type=kind,
                        name=str(data.get("name", toml.parent.name)),
                        path=_rel(root, toml.parent),
                    )
                )
        return out

    @router.get("/materials")
    def list_materials() -> list[Material]:
        return [
            Material(
                name=name,
                density=m["density"],
                category=m.get("category"),
                range=m.get("range"),
            )
            for name, m in load_materials(spec).items()
        ]

    @router.get("/schema/lure-config")
    def lure_config_schema() -> FormSchema:
        """Draft-07 form schema for @sjsf/form (rules R1–R9; see luregen.api.formschema)."""
        schema, ui = postprocess_schema(LureConfig.model_json_schema(), load_materials(spec))
        return FormSchema(json_schema=schema, ui_schema=ui)

    @router.get("/variants/{family}/{name}")
    def get_variant(family: Name, name: Name, response: Response) -> VariantDetail:
        out = detail(family, name, variant_file(family, name))
        response.headers["ETag"] = f'"{out.etag}"'
        return out

    @router.post("/variants/{family}/{name}/validate")
    def validate_variant(
        family: Name, name: Name, config: Annotated[dict[str, Any], Body()]
    ) -> list[FieldError]:
        """Dry-run validation of form output; [] means valid."""
        variant_file(family, name)
        return check(config)[1]

    @router.put(
        "/variants/{family}/{name}",
        responses={409: _CONFLICT, 422: _INVALID, 428: {"description": "No etag given"}},
    )
    def save_variant(
        family: Name,
        name: Name,
        body: SaveRequest,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> SaveResult:
        """Validate, then write only what differs from the parent layers (comments kept)."""
        path = variant_file(family, name)
        expected = (if_match or "").removeprefix("W/").strip('" ') or body.etag
        if not expected:
            raise HTTPException(428, "send the variant's etag as If-Match or body.etag")
        with save_lock:
            raw = path.read_bytes()
            if _etag(raw) != expected:
                raise HTTPException(409, {"message": "changed on disk", "etag": _etag(raw)})
            data, errors = check(body.config)
            if errors:
                raise HTTPException(422, [e.model_dump() for e in errors])
            try:
                text = rewrite_variant(path, data, resolved_of)
            except ConfigError as exc:
                raise HTTPException(422, [{"path": [], "message": str(exc)}]) from exc
            changed = text != raw.decode()
            own_name = tomllib.loads(raw.decode()).get("name", data.get("name"))
            expected = resolved_of({**data, "name": own_name})
            backup = path.with_name(path.name + ".bak") if changed else None
            _write_checked(path, text if changed else None, expected, raw, backup)
        out = detail(family, name, path)
        response.headers["ETag"] = f'"{out.etag}"'
        return SaveResult(
            changed=changed, backup=_rel(root, backup) if backup else None, variant=out
        )

    @router.get("/spec/status")
    def spec_status() -> SpecStatus:
        """Uncommitted changes under spec/ (for the UI badge; commits stay manual)."""
        files = _git_changes(root)
        if files is None:
            return SpecStatus(git=False, dirty=False, files=[])
        return SpecStatus(git=True, dirty=bool(files), files=files)

    return router


_UNEXPRESSIBLE = (
    "the variant file can't express this config "
    "(an inherited value can't be removed here; edit the parent layer)"
)


def _reload(path: Path) -> dict[str, Any]:
    try:
        return load_config(path).model_dump(mode="json")
    except ConfigError as exc:
        raise HTTPException(422, [{"path": [], "message": str(exc)}]) from exc


def _write_checked(
    path: Path, text: str | None, expected: dict[str, Any], old: bytes, backup: Path | None
) -> None:
    """Check that `text` (None: the file as it is) reloads to `expected`, then replace `path`
    atomically, keeping `old` in `backup`."""
    if text is None:
        if _reload(path) != expected:
            raise HTTPException(422, [{"path": [], "message": _UNEXPRESSIBLE}])
        return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".toml", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if _reload(tmp) != expected:
            raise HTTPException(422, [{"path": [], "message": _UNEXPRESSIBLE}])
        shutil.copymode(path, tmp)
        if backup is not None:
            backup.write_bytes(old)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _git_changes(root: Path) -> list[SpecChange] | None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=10
        ).stdout

    try:
        top = Path(git("rev-parse", "--show-toplevel").strip())
        out = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "spec")
    except (OSError, subprocess.SubprocessError):
        return None
    files, entries = [], iter(out.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            next(entries, None)  # the rename source
        files.append(SpecChange(path=_rel(root, top / rel), status=status))
    return files
