from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
InitiatorRole = Literal["internal", "external", "mixed", "system", "unknown"]
ContentOrigin = Literal["internal", "external"]


def _normalize_alias_mapping(value: object) -> object:
    if not isinstance(value, dict):
        return value
    normalized: dict[str, str] = {}
    canonical_by_alias: dict[str, str] = {}
    for alias, canonical_issuer in value.items():
        if not isinstance(alias, str) or not isinstance(canonical_issuer, str):
            raise ValueError("issuer aliases and canonical issuers must be text")
        clean_alias = alias.strip()
        clean_canonical = canonical_issuer.strip()
        collision_key = clean_alias.casefold()
        prior = canonical_by_alias.get(collision_key)
        if prior is not None:
            if prior.casefold() != clean_canonical.casefold():
                raise ValueError("issuer alias conflict maps one alias to multiple canonical issuers")
            raise ValueError("issuer alias configuration contains a duplicate alias")
        canonical_by_alias[collision_key] = clean_canonical
        normalized[clean_alias] = clean_canonical
    return normalized


class StrictClassificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InitiatorProfile(StrictClassificationModel):
    role: InitiatorRole
    aliases: list[NonEmptyText] = Field(default_factory=list)

    @field_validator("aliases")
    @classmethod
    def unique_aliases(cls, aliases: list[str]) -> list[str]:
        seen: set[str] = set()
        for alias in aliases:
            normalized = alias.casefold()
            if normalized in seen:
                raise ValueError("initiator profile contains a duplicate alias")
            seen.add(normalized)
        return aliases


class DocumentNumberIssuerRule(StrictClassificationModel):
    pattern: NonEmptyText
    canonical_issuer: NonEmptyText
    document_type: NonEmptyText | None = None

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, pattern: str) -> str:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("document-number pattern must be a valid regular expression") from exc
        return pattern


class TitleTemplateRule(StrictClassificationModel):
    pattern: NonEmptyText
    content_origin: ContentOrigin
    flow_type: NonEmptyText
    business_category: NonEmptyText | None = None
    canonical_issuer: NonEmptyText | None = None

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, pattern: str) -> str:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("title-template pattern must be a valid regular expression") from exc
        return pattern


class InitiatorProfilesFile(StrictClassificationModel):
    initiators: dict[NonEmptyText, InitiatorProfile] = Field(min_length=1)


class DocumentNumberIssuersFile(StrictClassificationModel):
    rules: list[DocumentNumberIssuerRule] = Field(min_length=1)


class IssuerAliasesFile(StrictClassificationModel):
    aliases: dict[NonEmptyText, NonEmptyText] = Field(min_length=1)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_issuer_aliases(cls, value: object) -> object:
        return _normalize_alias_mapping(value)


class TitleTemplatesFile(StrictClassificationModel):
    templates: list[TitleTemplateRule] = Field(min_length=1)


class PrivateClassificationConfig(StrictClassificationModel):
    initiators: dict[NonEmptyText, InitiatorProfile] = Field(min_length=1)
    document_number_issuers: list[DocumentNumberIssuerRule] = Field(min_length=1)
    issuer_aliases: dict[NonEmptyText, NonEmptyText] = Field(min_length=1)
    title_templates: list[TitleTemplateRule] = Field(min_length=1)

    @field_validator("initiators", mode="before")
    @classmethod
    def normalize_initiator_identifiers(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        seen: set[str] = set()
        for identifier, profile in value.items():
            if not isinstance(identifier, str):
                raise ValueError("initiator identifiers must be text")
            clean_identifier = identifier.strip()
            collision_key = clean_identifier.casefold()
            if collision_key in seen:
                raise ValueError("initiator configuration contains a duplicate identifier")
            seen.add(collision_key)
            normalized[clean_identifier] = profile
        return normalized

    @field_validator("issuer_aliases", mode="before")
    @classmethod
    def normalize_issuer_aliases(cls, value: object) -> object:
        return _normalize_alias_mapping(value)

    @model_validator(mode="after")
    def unique_initiator_aliases(self) -> "PrivateClassificationConfig":
        owners: dict[str, str] = {}
        for identifier, profile in self.initiators.items():
            for candidate in (identifier, *profile.aliases):
                normalized = candidate.casefold()
                prior = owners.get(normalized)
                if prior is not None and prior != identifier:
                    raise ValueError("initiator alias is assigned to multiple profiles")
                owners[normalized] = identifier
        return self
