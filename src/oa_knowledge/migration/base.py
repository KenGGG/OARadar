from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ImportPreview:
    source: Path
    items: int
    files: int
    warnings: tuple[str, ...] = ()


class LegacyImporter(Protocol):
    def preview(self, source: Path) -> ImportPreview: ...

    def execute(self, source: Path) -> ImportPreview: ...
