"""统一 data_root 路径解析的安全边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from oa_knowledge.storage_paths import relative_data_path, resolve_data_path


PENDING_PREFIXES = ("raw/pending", "archive/raw/oa/pending")


@pytest.mark.parametrize(
    "relpath",
    (
        "raw/pending/item-1/a.bin",
        "archive/raw/oa/pending/item-1/a.bin",
    ),
)
def test_resolve_data_path_accepts_supported_pending_layouts(tmp_path: Path, relpath: str) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = resolve_data_path(data_root, relpath, allowed_prefixes=PENDING_PREFIXES)

    assert result == (data_root / relpath).resolve()


@pytest.mark.parametrize(
    "relpath",
    (
        "/tmp/outside.bin",
        "../outside.bin",
        "raw/pending/../done/original.bin",
        "raw//pending/item.bin",
        "./raw/pending/item.bin",
        "raw/pending/",
    ),
)
def test_resolve_data_path_rejects_unsafe_or_noncanonical_paths(
    tmp_path: Path, relpath: str,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    with pytest.raises(ValueError):
        resolve_data_path(data_root, relpath, allowed_prefixes=PENDING_PREFIXES)


def test_resolve_data_path_rejects_disallowed_prefix(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    with pytest.raises(ValueError, match="allowed prefix"):
        resolve_data_path(
            data_root,
            "archive/raw/oa/done/original.bin",
            allowed_prefixes=PENDING_PREFIXES,
        )


def test_resolve_data_path_rejects_symlink_escape(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    (data_root / "raw/pending").mkdir(parents=True)
    outside.mkdir()
    (data_root / "raw/pending/link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes data_root"):
        resolve_data_path(
            data_root,
            "raw/pending/link/secret.bin",
            allowed_prefixes=PENDING_PREFIXES,
        )


def test_relative_data_path_returns_posix_path_and_rejects_outside(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    inside = data_root / "archive/raw/oa/pending/a.bin"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")

    assert relative_data_path(data_root, inside) == "archive/raw/oa/pending/a.bin"

    with pytest.raises(ValueError, match="outside data_root"):
        relative_data_path(data_root, tmp_path / "outside.bin")
