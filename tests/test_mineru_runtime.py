from types import SimpleNamespace

import pytest

from oa_knowledge.config import Settings
from oa_knowledge.parsers import mineru_runtime as runtime
from oa_knowledge.parsers import mineru_parser as parser


@pytest.mark.parametrize('failure', [False, True])
def test_owned_service_stops_after_parse_or_error(tmp_path, monkeypatch, failure):
    settings = Settings(mineru={'enabled': True, 'on_demand': True})
    monkeypatch.setattr(runtime, 'python_path', lambda: tmp_path / 'python')
    (tmp_path / 'python').touch()
    health = iter([False, True])
    def probe(_):
        if not next(health):
            raise parser.MineruResponseError('offline')
        return {'version': 'test'}
    monkeypatch.setattr(parser, '_health_payload', probe)
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **kw: process)
    stopped = []
    monkeypatch.setattr(runtime, '_stop', stopped.append)
    monkeypatch.setattr(runtime, 'lock_exclusive', lambda _: None)
    local = SimpleNamespace(mineru=settings.mineru, runtime_root=tmp_path)
    try:
        with runtime.runtime(local):
            if failure:
                raise ValueError('synthetic parse failure')
    except ValueError:
        assert failure
    assert stopped == [process]


def test_availability_does_not_start_service(tmp_path, monkeypatch):
    executable = tmp_path / 'python'
    executable.touch()
    monkeypatch.setattr(runtime, 'python_path', lambda: executable)
    monkeypatch.setattr(parser, '_health_payload', lambda *_: pytest.fail('loaded service'))
    assert parser.mineru_available(Settings(mineru={'enabled': True, 'on_demand': True}))
