import io
import stat
import zipfile

from oa_knowledge.archive.safety import ArchiveLimits, extract_zip_to_staging, inspect_zip_bytes


def _zip(entries: list[tuple[str, bytes]], *, symlink: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
    return output.getvalue()


def test_safe_zip_is_inspected_member_by_member_without_extraction() -> None:
    result = inspect_zip_bytes(_zip([("folder/a.pdf", b"synthetic"), ("表格.xlsx", b"sheet")]))

    assert result.status == "passed"
    assert [member.original_path for member in result.members] == ["folder/a.pdf", "表格.xlsx"]
    assert all(member.status == "accepted" for member in result.members)
    assert result.total_uncompressed_bytes == 14


def test_zip_rejects_path_traversal_windows_paths_and_links() -> None:
    for name in ("../escape.pdf", "/absolute.pdf", "C:/drive.pdf", "folder\\..\\escape.pdf"):
        result = inspect_zip_bytes(_zip([(name, b"x")]))
        assert result.status == "rejected"
        assert result.error_code == "ARCHIVE_PATH_TRAVERSAL"

    linked = inspect_zip_bytes(_zip([], symlink="linked.pdf"))
    assert linked.status == "rejected"
    assert linked.error_code == "ARCHIVE_LINK_REJECTED"


def test_zip_enforces_member_size_count_and_ratio_limits() -> None:
    count = inspect_zip_bytes(_zip([("a", b"1"), ("b", b"2")]), ArchiveLimits(max_members=1))
    assert count.error_code == "ARCHIVE_MEMBER_LIMIT_EXCEEDED"

    size = inspect_zip_bytes(_zip([("large", b"12345")]), ArchiveLimits(max_member_bytes=4))
    assert size.error_code == "ARCHIVE_SIZE_LIMIT_EXCEEDED"

    ratio = inspect_zip_bytes(_zip([("compressed", b"0" * 10_000)]), ArchiveLimits(max_ratio=2))
    assert ratio.error_code == "ARCHIVE_RATIO_LIMIT_EXCEEDED"


def test_invalid_and_empty_zip_have_stable_error_codes() -> None:
    assert inspect_zip_bytes(b"not a zip").error_code == "ARCHIVE_CORRUPTED"
    assert inspect_zip_bytes(_zip([])).error_code == "ARCHIVE_EMPTY"


def test_safe_zip_extracts_only_after_full_inspection(tmp_path) -> None:
    staging = tmp_path / "staging"
    result = extract_zip_to_staging(_zip([("folder/a.txt", b"alpha"), ("b.txt", b"beta")]), staging)

    assert result.status == "passed"
    assert (staging / "folder/a.txt").read_bytes() == b"alpha"
    assert (staging / "b.txt").read_bytes() == b"beta"


def test_rejected_zip_writes_nothing_to_staging(tmp_path) -> None:
    staging = tmp_path / "staging"
    result = extract_zip_to_staging(_zip([("safe.txt", b"safe"), ("../escape.txt", b"bad")]), staging)

    assert result.error_code == "ARCHIVE_PATH_TRAVERSAL"
    assert not staging.exists()
    assert not (tmp_path / "escape.txt").exists()
