from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError

from .schemas import (
    DocumentNumberIssuersFile,
    InitiatorProfilesFile,
    IssuerAliasesFile,
    PrivateClassificationConfig,
    TitleTemplatesFile,
)


_REQUIRED_FILES = (
    "initiator_profiles.yaml",
    "document_number_issuers.yaml",
    "issuer_aliases.yaml",
    "title_templates.yaml",
)

_PUBLIC_SCHEMA_FIELDS = frozenset(
    {
        "aliases",
        "business_category",
        "canonical_issuer",
        "content_origin",
        "document_type",
        "flow_type",
        "initiators",
        "pattern",
        "role",
        "rules",
        "templates",
    }
)


class PrivateConfigError(ValueError):
    """The local private configuration is missing, unsafe, or invalid."""


class _DuplicateKeyError(ConstructorError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise _DuplicateKeyError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class LoadedPrivateConfig:
    config: PrivateClassificationConfig
    config_sha256: str


def _canonical_hash_payload(config: PrivateClassificationConfig) -> dict[str, Any]:
    """Return semantic hash input without assigning rule priority by file order.

    The current schema defines initiator aliases and both rule collections as
    unordered declarations. Exact repeated patterns are rejected by the schema,
    so sorting here cannot choose between ambiguous outcomes. The parsed config
    itself retains its declaration order for diagnostics and display.
    """

    payload = config.model_dump(mode="json")
    for profile in payload["initiators"].values():
        profile["aliases"] = sorted(profile["aliases"], key=str.casefold)

    def stable_rule(rule: dict[str, Any]) -> str:
        return json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    payload["document_number_issuers"] = sorted(
        payload["document_number_issuers"], key=stable_rule
    )
    payload["title_templates"] = sorted(payload["title_templates"], key=stable_rule)
    return payload


def _read_required_file(root: Path, filename: str) -> str:
    path = root / filename
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PrivateConfigError(f"{filename}: required private configuration file is missing") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PrivateConfigError(f"{filename}: symlinks are forbidden")
    if not stat.S_ISREG(metadata.st_mode):
        raise PrivateConfigError(f"{filename}: required path must be a regular file")

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PrivateConfigError(f"{filename}: resolved path escapes the private root") from exc

    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & ~0o600:
        raise PrivateConfigError(f"{filename}: POSIX permissions must be no broader than 0600")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PrivateConfigError(f"{filename}: private configuration file cannot be opened safely") from exc
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise PrivateConfigError(f"{filename}: opened path must be a regular file")
        if (opened_metadata.st_dev, opened_metadata.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PrivateConfigError(f"{filename}: private configuration file changed while opening")
        if os.name == "posix" and stat.S_IMODE(opened_metadata.st_mode) & ~0o600:
            raise PrivateConfigError(f"{filename}: POSIX permissions must be no broader than 0600")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    except UnicodeDecodeError as exc:
        raise PrivateConfigError(f"{filename}: private configuration must be UTF-8") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_yaml(root: Path, filename: str) -> object:
    text = _read_required_file(root, filename)
    try:
        loaded = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except _DuplicateKeyError as exc:
        raise PrivateConfigError(f"{filename}: duplicate YAML mapping key") from exc
    except yaml.YAMLError as exc:
        raise PrivateConfigError(f"{filename}: invalid YAML") from exc
    if loaded is None:
        raise PrivateConfigError(f"{filename}: configuration file must not be empty")
    return loaded


def _validated(model_type: type[Any], value: object, filename: str) -> Any:
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        safe_fields = sorted(
            {
                component
                for error in exc.errors(include_input=False)
                for component in error["loc"]
                if isinstance(component, str) and component in _PUBLIC_SCHEMA_FIELDS
            }
        )
        category = ", ".join(safe_fields) if safe_fields else "schema"
        raise PrivateConfigError(
            f"{filename}: invalid private configuration ({category})"
        ) from exc


def _load_private_classification_config(root: Path) -> LoadedPrivateConfig:
    configured_root = Path(root).expanduser()
    if configured_root.is_symlink():
        raise PrivateConfigError("private classification root must not be a symlink")
    try:
        resolved_root = configured_root.resolve(strict=True)
    except OSError as exc:
        raise PrivateConfigError("private classification root does not exist") from exc
    if not resolved_root.is_dir():
        raise PrivateConfigError("private classification root must be a directory")

    raw = {filename: _load_yaml(resolved_root, filename) for filename in _REQUIRED_FILES}
    initiators_file = _validated(
        InitiatorProfilesFile,
        raw["initiator_profiles.yaml"],
        "initiator_profiles.yaml",
    )
    document_rules_file = _validated(
        DocumentNumberIssuersFile,
        raw["document_number_issuers.yaml"],
        "document_number_issuers.yaml",
    )
    aliases_file = _validated(
        IssuerAliasesFile,
        raw["issuer_aliases.yaml"],
        "issuer_aliases.yaml",
    )
    title_templates_file = _validated(
        TitleTemplatesFile,
        raw["title_templates.yaml"],
        "title_templates.yaml",
    )
    try:
        config = PrivateClassificationConfig(
            initiators=initiators_file.initiators,
            document_number_issuers=document_rules_file.rules,
            issuer_aliases=aliases_file.aliases,
            title_templates=title_templates_file.templates,
        )
    except ValidationError as exc:
        raise PrivateConfigError("private classification configuration has conflicting aliases") from exc

    canonical = json.dumps(
        _canonical_hash_payload(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return LoadedPrivateConfig(
        config=config,
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def load_private_classification_config(root: Path) -> LoadedPrivateConfig:
    """Validate and load the four local-only classification rule files.

    Public failures expose only fixed filenames and error categories. The
    outer exception is raised after the input-bearing implementation exception
    has left scope, preventing traceback logging from retaining private YAML in
    ``__cause__`` or ``__context__``.
    """

    failure_message: str | None = None
    try:
        return _load_private_classification_config(root)
    except PrivateConfigError as error:
        failure_message = str(error)
    if failure_message is not None:
        raise PrivateConfigError(failure_message)
    raise AssertionError("unreachable private configuration loader state")
