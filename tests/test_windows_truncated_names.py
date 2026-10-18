from oa_knowledge.archive.naming import safe_filename
from oa_knowledge.detail_archive import _attachment_relname
from types import SimpleNamespace
import hashlib


def test_truncation_never_exposes_windows_trailing_space_or_dot():
    assert safe_filename("synthetic directory long name", 10) == "synthetic"
    assert not safe_filename("synthetic. longer name", 10).endswith((" ", "."))
    assert not safe_filename("合成目录 空格边界更多内容", 13).endswith((" ", "."))


def test_long_attachment_names_preserve_distinct_identities():
    names = []
    for key in ("synthetic-attachment-1", "synthetic-attachment-2"):
        name = _attachment_relname(SimpleNamespace(filename="合成附件名称" * 40 + ".pdf", attachment_key=key))
        assert name.endswith("_" + hashlib.sha256(key.encode()).hexdigest()[:12] + ".pdf")
        assert len(name.encode("utf-8")) <= 160
        names.append(name)
    assert names[0] != names[1]
