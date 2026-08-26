from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
InitiatorRole = Literal["internal", "external", "mixed", "system", "unknown"]
ContentOrigin = Literal["internal", "external"]


class _PatternRule(Protocol):
    pattern: str


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


def _validate_terminal_aliases(aliases: dict[str, str]) -> None:
    targets_by_alias = {alias.casefold(): canonical for alias, canonical in aliases.items()}
    canonical_spelling: dict[str, str] = {}
    for canonical in aliases.values():
        canonical_key = canonical.casefold()
        prior_spelling = canonical_spelling.get(canonical_key)
        if prior_spelling is not None and prior_spelling != canonical:
            raise ValueError("each canonical issuer must have one exact output string")
        canonical_spelling[canonical_key] = canonical
        next_target = targets_by_alias.get(canonical_key)
        if next_target is not None and next_target != canonical:
            raise ValueError("canonical issuer aliases must resolve directly to terminal targets")


def _validate_declared_canonical_issuers(
    aliases: dict[str, str], canonical_issuers: Sequence[str]
) -> None:
    targets_by_alias = {alias.casefold(): canonical for alias, canonical in aliases.items()}
    for canonical in canonical_issuers:
        alias_target = targets_by_alias.get(canonical.casefold())
        if alias_target is not None and alias_target != canonical:
            raise ValueError(
                "rule canonical issuers must be absent from aliases or exact self-maps"
            )


def _validate_unique_rule_patterns(rules: Sequence[_PatternRule]) -> None:
    seen: set[str] = set()
    for rule in rules:
        pattern = rule.pattern
        if pattern in seen:
            raise ValueError("rule patterns must be unique and order-independent")
        seen.add(pattern)


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

    @model_validator(mode="after")
    def unique_rule_patterns(self) -> "DocumentNumberIssuersFile":
        _validate_unique_rule_patterns(self.rules)
        return self


class IssuerAliasesFile(StrictClassificationModel):
    aliases: dict[NonEmptyText, NonEmptyText] = Field(min_length=1)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_issuer_aliases(cls, value: object) -> object:
        return _normalize_alias_mapping(value)

    @model_validator(mode="after")
    def terminal_canonical_targets(self) -> "IssuerAliasesFile":
        _validate_terminal_aliases(self.aliases)
        return self


class TitleTemplatesFile(StrictClassificationModel):
    templates: list[TitleTemplateRule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_rule_patterns(self) -> "TitleTemplatesFile":
        _validate_unique_rule_patterns(self.templates)
        return self


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

    @model_validator(mode="after")
    def order_independent_rule_and_issuer_alias_integrity(
        self,
    ) -> "PrivateClassificationConfig":
        _validate_terminal_aliases(self.issuer_aliases)
        _validate_declared_canonical_issuers(
            self.issuer_aliases,
            [
                *(rule.canonical_issuer for rule in self.document_number_issuers),
                *(
                    template.canonical_issuer
                    for template in self.title_templates
                    if template.canonical_issuer is not None
                ),
            ],
        )
        _validate_unique_rule_patterns(self.document_number_issuers)
        _validate_unique_rule_patterns(self.title_templates)
        return self
