"""Strict structured decisions returned by the local model."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = "curation-schema-v1"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionSource(StrictSchema):
    source_key: str = Field(min_length=1, max_length=180)
    role: Literal["body", "attachment"]


class DocumentDecision(StrictSchema):
    document_kind: Literal["formal", "internal", "project"]
    normalized_title: str = Field(min_length=1, max_length=500)
    issuer: str = Field(default="", max_length=300)
    document_number: str = Field(default="", max_length=120)
    publication_date: str = Field(default="", max_length=20)
    topic: str = Field(default="", max_length=200)
    customer: str = Field(default="", max_length=200)
    project: str = Field(default="", max_length=200)
    stage: str = Field(default="", max_length=120)
    confidence: float = Field(ge=0, le=1)
    sources: list[DecisionSource] = Field(min_length=1)
    evidence_source_keys: list[str] = Field(default_factory=list)


class ModelCurationResponse(StrictSchema):
    documents: list[DocumentDecision] = Field(default_factory=list, max_length=50)


class SourceSemanticMap(StrictSchema):
    source_key: str = Field(min_length=1, max_length=180)
    summary: str = Field(min_length=1, max_length=600)
    document_signals: list[str] = Field(default_factory=list, max_length=12)


class CurationDecision(BaseModel):
    documents: list[DocumentDecision] = Field(default_factory=list)
    needs_review: bool = False
    reason_code: str = "ok"
