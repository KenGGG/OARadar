from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .naming import validate_relative_path


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileManifest(ManifestModel):
    attachment_key: str
    original_name: str
    local_relpath: str | None = None
    file_role: Literal["metadata_snapshot", "body_snapshot", "workflow_snapshot", "direct_attachment", "official_body", "official_attachment", "associated_document", "opinion_attachment"]
    source_container_key: str
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    download_status: str = "discovered"

    @field_validator("local_relpath")
    @classmethod
    def relative_path(cls, value: str | None) -> str | None:
        if value is not None:
            validate_relative_path(value)
        return value


class ContainerManifest(ManifestModel):
    container_key: str
    parent_container_key: str | None = None
    page_family: str
    depth: int = Field(ge=1, le=10)
    direct_file_count: int = Field(default=0, ge=0)
    child_container_count: int = Field(default=0, ge=0)
    has_unvisited_children: bool = False
    files: list[FileManifest] = Field(default_factory=list)

    @model_validator(mode="after")
    def depth_limit_is_explicit(self) -> "ContainerManifest":
        if self.depth == 10 and self.has_unvisited_children:
            return self
        if self.depth < 10 and self.has_unvisited_children:
            raise ValueError("unvisited children are only accepted as a depth-limit condition")
        return self


class ItemManifest(ManifestModel):
    oa_item_key: str
    workitem_id_text: str
    title: str
    captured_at: datetime
    containers: list[ContainerManifest] = Field(default_factory=list)

    @model_validator(mode="after")
    def container_tree_reconciles(self) -> "ItemManifest":
        keys = [container.container_key for container in self.containers]
        if len(keys) != len(set(keys)):
            raise ValueError("container keys must be unique")
        by_key = {container.container_key: container for container in self.containers}
        for container in self.containers:
            if container.direct_file_count != len(container.files):
                raise ValueError(f"direct file count mismatch: {container.container_key}")
            actual_children = sum(child.parent_container_key == container.container_key for child in self.containers)
            if container.child_container_count != actual_children:
                raise ValueError(f"child container count mismatch: {container.container_key}")
            if container.parent_container_key:
                parent = by_key.get(container.parent_container_key)
                if parent is None or container.depth != parent.depth + 1:
                    raise ValueError(f"invalid container parent/depth: {container.container_key}")
        return self

    @property
    def depth_limit_reached(self) -> bool:
        return any(container.depth == 10 and container.has_unvisited_children for container in self.containers)
