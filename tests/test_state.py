import pytest

from oa_knowledge.constants import BatchStatus, PipelineStatus
from oa_knowledge.state import BATCH_TRANSITIONS, PIPELINE_TRANSITIONS, reconcile_batch, validate_transition


def test_valid_and_invalid_transitions() -> None:
    validate_transition(PipelineStatus.DISCOVERED, PipelineStatus.RAW_SAVED, PIPELINE_TRANSITIONS)
    validate_transition(BatchStatus.RUNNING, BatchStatus.PAUSED, BATCH_TRANSITIONS)
    with pytest.raises(ValueError):
        validate_transition(BatchStatus.PLANNED, BatchStatus.COMPLETED, BATCH_TRANSITIONS)


def test_batch_reconciliation() -> None:
    assert reconcile_batch(20, 18, 2, 0)
    assert not reconcile_batch(20, 18, 1, 1)
    assert reconcile_batch(20, 18, 1, 0, reviewed=1)
    with pytest.raises(ValueError):
        reconcile_batch(-1, 0, 0, 0)
