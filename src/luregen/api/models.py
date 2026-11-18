"""Response and request models for the JSON API (they drive the generated TypeScript types)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DesignSummary(BaseModel):
    family: str
    path: str = Field(description="family.toml, relative to the data root")
    variants: list[str]


class VariantSummary(BaseModel):
    name: str
    path: str = Field(description="Variant TOML, relative to the data root")
    method: str | None = None
    material: str | None = None


class MachineSummary(BaseModel):
    id: str = Field(description="Folder name under spec/printers or spec/routers")
    type: Literal["printer", "router"]
    name: str
    path: str


class Material(BaseModel):
    name: str
    density: float = Field(description="g/cm³")
    category: str | None = None
    range: list[float] | None = None


class FieldError(BaseModel):
    path: list[str | int] = Field(description="Config key path (pydantic loc); [] = whole")
    message: str


class Layer(BaseModel):
    label: str = Field(description='"variant", "family", "common" or the file stem')
    path: str


class VariantDetail(BaseModel):
    """A variant for the editor. Path values (profile.image, …) are relative to the data root."""

    family: str
    name: str
    path: str
    etag: str = Field(description="Send back as If-Match (or body `etag`) when saving")
    own: dict[str, Any] = Field(description="The variant file itself")
    layered: dict[str, Any] = Field(description="Merged `extends` chain, before validation")
    inherited: dict[str, Any] = Field(
        {},
        description="What the parent layers alone give (merged, before validation): the "
        "value a 'reset to inherited' restores",
    )
    resolved: dict[str, Any] | None = Field(
        description="Validated and catalog-resolved config (None when invalid)"
    )
    errors: list[FieldError] = []
    layers: list[Layer] = Field(description="The `extends` chain, variant first")
    provenance: dict[str, int] = Field(
        description="JSON pointer → index into `layers` of the layer that set it"
    )


class SaveRequest(BaseModel):
    config: dict[str, Any] = Field(description="Edited config (form output)")
    etag: str | None = Field(None, description="Precondition, if no If-Match header")


class SaveResult(BaseModel):
    changed: bool
    backup: str | None = Field(None, description="The .bak written, relative to the data root")
    variant: VariantDetail


class FormSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    json_schema: dict[str, Any] = Field(alias="schema")
    ui_schema: dict[str, Any] = Field(alias="uiSchema")


class SpecChange(BaseModel):
    path: str = Field(description="Relative to the data root")
    status: str = Field(description="git porcelain XY code, e.g. ' M', '??'")


class SpecStatus(BaseModel):
    git: bool = Field(description="False when git or the repository is unavailable")
    dirty: bool
    files: list[SpecChange]


class Upload(BaseModel):
    id: str = Field(description="sha256 of the content")
    kind: Literal["photo", "csv", "gcode"]
    size: int
    path: str = Field(description="Relative to the data root; usable as a config path value")


class BuildFile(BaseModel):
    name: str = Field(description="Relative to the build folder")
    size: int


class SectionSummary(BaseModel):
    mass_g: float | None = None
    density_g_cm3: float | None = None


class Verdict(BaseModel):
    status: str = Field(description='"pass" or "fail"')
    label: str = ""
    failed: list[str] = []


class ReportSummary(BaseModel):
    """Key numbers of report.json (hardware band mid). Absent fields are None."""

    mass_g: float | None = None
    mass_oz: float | None = None
    density_g_cm3: float | None = None
    cg_mm: list[float] | None = Field(None, description="Centre of gravity (x, y, z)")
    cb_mm: list[float] | None = Field(None, description="Centre of buoyancy (x, y, z)")
    cg_minus_cb_mm: list[float] | None = Field(None, description="CG − CB (x, y, z)")
    cb_above_cg_mm: float | None = Field(None, description="CB z − CG z (> 0: self-righting)")
    cb_above_cg_target_mm: float | None = None
    envelope_volume_cm3: float | None = None
    verdict: Verdict | None = None
    sections: dict[str, SectionSummary] = {}
    error: str | None = Field(None, description="The build's error, when status is error")


class Download(BaseModel):
    label: str = Field(description="e.g. 'model/head.step', 'model/head.glb', 'cnc files'")
    format: str = Field(description="STEP, STL, 3MF, GLB, G-code, NC, ZIP, …")
    href: str = Field(description="API URL (same origin) that downloads it")
    size: int | None = Field(None, description="Bytes, when known before the download")


class BuildSummary(BaseModel):
    family: str
    variant: str
    stamp: str
    path: str
    latest: bool
    created: str | None = None
    status: str | None = None
    git_sha: str | None = None
    git_dirty: bool | None = None
    warnings: list[str] = []
    report: ReportSummary | None = Field(None, description="None without a report.json")
    files: list[BuildFile]
    downloads: list[Download] = Field(
        [], description="Exports: model and fab files, converted GLBs and zip archives"
    )


class Delta(BaseModel):
    key: str
    label: str
    unit: str
    a: float | None
    b: float | None
    delta: float | None = Field(description="b − a (None if either is missing)")


class ConfigChange(BaseModel):
    key: str = Field(description="Dotted key in resolved.toml (list items by index)")
    a: Any = None
    b: Any = None


class BuildComparison(BaseModel):
    a: BuildSummary
    b: BuildSummary
    deltas: list[Delta]
    config_changes: list[ConfigChange] = Field(
        description="resolved.toml keys that differ (name excluded), at most 200"
    )
    config_changes_truncated: bool = False
