import pytest

from oa_knowledge.classification.agnes_client import AgnesPublicClient


def test_agnes_client_accepts_only_approved_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("AGNES_API_KEY", "synthetic-secret")
    client = AgnesPublicClient(
        "https://apihub.agnes-ai.com/v1", api_key_env="AGNES_API_KEY", model="agnes-2.0-flash"
    )

    assert client.model == "agnes-2.0-flash"
    with pytest.raises(ValueError, match="approved"):
        AgnesPublicClient("https://example.invalid/v1", api_key_env="AGNES_API_KEY", model="agnes-2.0-flash")


def test_agnes_client_uses_openai_chat_endpoint_without_logging_payload(monkeypatch) -> None:
    monkeypatch.setenv("AGNES_API_KEY", "synthetic-secret")
    observed: dict[str, object] = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"model": "agnes-2.0-flash", "choices": [{"message": {"content": "{}"}}]}

    class _HttpClient:
        def __init__(self, **kwargs) -> None:
            observed["init"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, path: str, json: dict):
            observed["path"] = path
            observed["payload"] = json
            return _Response()

    monkeypatch.setattr("oa_knowledge.classification.agnes_client.httpx.Client", _HttpClient)
    result = AgnesPublicClient(
        "https://apihub.agnes-ai.com/v1", api_key_env="AGNES_API_KEY", model="agnes-2.0-flash"
    ).chat("system", "public body", json_schema={"type": "object"})

    assert result["content"] == "{}"
    assert observed["path"] == "/chat/completions"
    assert observed["init"]["follow_redirects"] is False
    assert observed["init"]["headers"] == {"Authorization": "Bearer synthetic-secret"}
