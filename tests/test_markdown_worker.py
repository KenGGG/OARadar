from sqlalchemy.exc import OperationalError

from oa_knowledge.markdown_worker import MarkdownWorker


def test_database_lock_during_claim_is_transient(monkeypatch) -> None:
    class FakeSession:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("oa_knowledge.markdown_worker.Session", FakeSession)
    monkeypatch.setattr(
        "oa_knowledge.markdown_worker.claim",
        lambda *_args: (_ for _ in ()).throw(
            OperationalError("claim", {}, Exception("database is locked"))
        ),
    )
    worker = object.__new__(MarkdownWorker)
    worker.engine = object()
    worker.owner = "synthetic-worker"

    assert worker.run_once() is False
