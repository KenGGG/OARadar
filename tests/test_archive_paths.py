"""Tests for centralized archive path construction and recognition."""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

import pytest

from oa_knowledge.archive_paths import (
    ARCHIVE_PREFIX,
    done_archive_directory,
    is_current_archive_path,
    is_legacy_archive_path,
    markdown_tail_from_archive_path,
    pending_archive_directory,
    replace_archive_prefix,
)


def test_done_archive_directory_uses_initiation_date_and_title_directory() -> None:
    rel = done_archive_directory("事项", "42", datetime(2022, 4, 22, 9, 0))
    assert rel.parts[:1] == ARCHIVE_PREFIX.parts
    assert rel.parts[1:3] == ("2022", "04")
    assert rel.as_posix() == "originals/2022/04/2022-04-22_事项"


def test_done_archive_directory_falls_back_to_unknown_period() -> None:
    assert done_archive_directory("事项", "42", None).as_posix() == "originals/unknown/unknown_事项"


def test_pending_archive_directory_uses_unified_prefix() -> None:
    rel = pending_archive_directory(7, 99)
    assert rel.as_posix() == "originals/pending/7/99"


@pytest.mark.parametrize(
    "rel,expected",
    [
        (PurePosixPath("raw/done/2022/04/x"), True),
        (PurePosixPath("raw/pending/7/99"), True),
        (PurePosixPath("archive/raw/oa/done/2022/04/x"), False),
        (PurePosixPath("archive/raw/oa/pending/7/99"), False),
        (PurePosixPath("data/done/x"), False),
    ],
)
def test_is_legacy_archive_path(rel: PurePosixPath, expected: bool) -> None:
    assert is_legacy_archive_path(rel) is expected


@pytest.mark.parametrize(
    "rel,expected",
    [
        (PurePosixPath("originals/done/2022/04/x"), True),
        (PurePosixPath("originals/pending/7/99"), True),
        (PurePosixPath("originals/2022/04/2022-04-22_事项/source.pdf"), True),
        (PurePosixPath("archive/raw/done/x"), False),
        (PurePosixPath("data/other/x"), False),
        (PurePosixPath("raw/done/2022/04/x"), False),
    ],
)
def test_is_current_archive_path(rel: PurePosixPath, expected: bool) -> None:
    assert is_current_archive_path(rel) is expected


def test_replace_archive_prefix() -> None:
    assert replace_archive_prefix("a/b/c", "a/b", "x/y") == "x/y/c"
    assert replace_archive_prefix("a/b", "a/b", "x/y") == "x/y"
    assert replace_archive_prefix("z/b/c", "a/b", "x/y") == "z/b/c"
    assert replace_archive_prefix(None, "a/b", "x/y") is None


@pytest.mark.parametrize(
    "rel,expected",
    [
        (PurePosixPath("originals/2022/04/2022-04-22_x/y.pdf"), PurePosixPath("2022/04/2022-04-22_x/y.pdf")),
        (PurePosixPath("archive/raw/oa/done/2022/04/x/y.pdf"), PurePosixPath("done/2022/04/x/y.pdf")),
        (PurePosixPath("raw/pending/7/99/body.html"), PurePosixPath("pending/7/99/body.html")),
    ],
)
def test_markdown_tail_from_archive_path(rel: PurePosixPath, expected: PurePosixPath) -> None:
    assert markdown_tail_from_archive_path(rel) == expected


def test_markdown_tail_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError):
        markdown_tail_from_archive_path(PurePosixPath("data/other/x"))


@pytest.mark.parametrize(
    "rel",
    [
        PurePosixPath("originals/../escape"),
        PurePosixPath("/raw/done/2022/04/x"),
    ],
)
def test_markdown_tail_rejects_absolute_or_traversal_path(rel: PurePosixPath) -> None:
    with pytest.raises(ValueError, match="safe relative"):
        markdown_tail_from_archive_path(rel)
