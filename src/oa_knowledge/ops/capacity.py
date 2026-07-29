from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import shutil
import sqlite3


@dataclass(frozen=True)
class CapacityReport:
    current_items: int
    verified_files: int
    verified_bytes: int
    average_bytes_per_item: int
    target_items: int
    projected_incremental_bytes: int
    safety_factor: float
    required_bytes_with_safety: int
    disk_free_bytes: int
    allowed: bool

    def as_dict(self) -> dict[str, int | float | bool]:
        return asdict(self)


def capacity_report(database: Path, data_root: Path, target_items: int, safety_factor: float = 1.5) -> CapacityReport:
    if target_items < 1:
        raise ValueError("target_items must be positive")
    with sqlite3.connect(database) as connection:
        current_items = connection.execute("SELECT COUNT(*) FROM oa_items").fetchone()[0]
        verified_files, verified_bytes = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM files WHERE download_status = 'verified'"
        ).fetchone()
    average = int(verified_bytes / current_items) if current_items else 10 * 1024 * 1024
    incremental = max(0, target_items - current_items) * average
    required = int(incremental * safety_factor)
    free = shutil.disk_usage(data_root).free
    return CapacityReport(
        current_items=current_items, verified_files=verified_files, verified_bytes=verified_bytes,
        average_bytes_per_item=average, target_items=target_items,
        projected_incremental_bytes=incremental, safety_factor=safety_factor,
        required_bytes_with_safety=required, disk_free_bytes=free, allowed=free >= required,
    )


@dataclass
class ScaleReport:
    """Capacity report tailored for Scale-500 gate."""
    target_items: int
    current_items: int
    new_items: int
    average_bytes_per_item: int
    projected_new_files: int
    projected_bytes: int
    safety_factor: float
    required_with_safety: int
    disk_free_bytes: int
    disk_usage_percent: float
    allowed: bool
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def scale_capacity_report(
    database: Path,
    data_root: Path,
    target_items: int = 500,
    safety_factor: float = 1.5,
    min_free_percent: float = 10.0,
) -> ScaleReport:
    """Validate disk capacity before running a Scale-500 batch.

    Checks:
    1. Sufficient free disk space (bytes with safety factor).
    2. Free disk percentage above threshold.
    3. Current item count is below target (otherwise nothing to do).
    """
    with sqlite3.connect(database) as connection:
        current_items = connection.execute("SELECT COUNT(*) FROM oa_items").fetchone()[0]
        verified_files, verified_bytes = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM files WHERE download_status = 'verified'"
        ).fetchone()

    average = int(verified_bytes / current_items) if current_items else 10 * 1024 * 1024
    new_items = max(0, target_items - current_items)
    projected_bytes = new_items * average
    required = int(projected_bytes * safety_factor)

    usage = shutil.disk_usage(data_root)
    free = usage.free
    total = usage.total
    percent_free = (free / total * 100) if total > 0 else 0

    warnings: list[str] = []
    allowed = True

    if free < required:
        allowed = False
        warnings.append(f"insufficient_disk_space: need {required:,} bytes, have {free:,}")

    if percent_free < min_free_percent:
        allowed = False
        warnings.append(f"low_disk_percentage: {percent_free:.1f}% free, minimum {min_free_percent}%")

    if new_items == 0:
        warnings.append("already_at_target: current items meet or exceed target")

    return ScaleReport(
        target_items=target_items,
        current_items=current_items,
        new_items=new_items,
        average_bytes_per_item=average,
        projected_new_files=projected_bytes // max(average, 1),
        projected_bytes=projected_bytes,
        safety_factor=safety_factor,
        required_with_safety=required,
        disk_free_bytes=free,
        disk_usage_percent=round(100 - percent_free, 1),
        allowed=allowed,
        warnings=warnings,
    )
