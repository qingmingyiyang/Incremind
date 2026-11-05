import httpx

from backend.memory_app.chatgpt_subscription import ChatGPTSubscriptions
from backend.security.secrets import build_model_secret_store
from core.storage_provider import SQLiteStructuredRecordStore


def test_default_client_uses_environment_and_is_closed_by_its_owner(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = ChatGPTSubscriptions(records, build_model_secret_store(tmp_path))
    client = service.client
    try:
        assert client.trust_env is True
        assert client.timeout == httpx.Timeout(15)
        assert client.follow_redirects is False
        assert not client.is_closed
    finally:
        service.close()
    assert client.is_closed
    service.close()


def test_injected_real_client_keeps_its_configuration_and_close_responsibility(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=7) as client:
        service = ChatGPTSubscriptions(records, build_model_secret_store(tmp_path), client=client)
        assert service.client is client
        assert not service.client.trust_env
        assert service.client.follow_redirects
        assert service.client.timeout == httpx.Timeout(7)
        service.close()
        assert not client.is_closed
    assert client.is_closed
