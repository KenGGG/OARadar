"""Wikilink resolver — maps entity names to canonical Obsidian links."""

from __future__ import annotations

import yaml
from pathlib import Path


class WikilinkResolver:
    """Resolves entity aliases and file relationships to wikilinks."""

    def __init__(self, entities_path: Path | None = None) -> None:
        self._aliases: dict[str, str] = {}
        self._relationships: dict[str, list[str]] = {}
        if entities_path and entities_path.is_file():
            self.load_entities(entities_path)

    def load_entities(self, path: Path) -> None:
        """Load entity aliases from a YAML file."""
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entity in data.get("entities", []):
            name = entity.get("name", "")
            aliases = entity.get("aliases", [])
            if name:
                self._aliases[name] = name  # canonical
                for alias in aliases:
                    self._aliases[alias] = name

    def resolve(self, name: str) -> str:
        """Resolve an entity name to a wikilink."""
        canonical = self._aliases.get(name, name)
        return f"[[{canonical}]]"

    def add_relationship(self, source: str, targets: list[str]) -> None:
        """Register a relationship between entities."""
        self._relationships[source] = targets

    def get_related(self, source: str) -> list[str]:
        """Get related entity names for wikilinking."""
        return self._relationships.get(source, [])
