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
    files: list[BuildFile]
