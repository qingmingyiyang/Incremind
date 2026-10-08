from __future__ import annotations

import tempfile
from threading import Event, Thread
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests import _path_setup  # noqa: F401

from backend.api.app import create_app
from backend.video_summary.infrastructure.settings_service import ProviderSettings
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence
from core.storage_provider.connection_scope import connection_scope


class ProviderSettingsApiTests(unittest.TestCase):
    def test_loopback_provider_test_is_forwarded_as_keyless_anonymous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, connection_scope():
            service = RecordingSettingsService()
            container = FakeContainer(Path(temp_dir), settings_service=service)
            with TestClient(create_app(container)) as client:
                created = client.post("/api/providers", json={
                    "provider_id": "local-provider", "name": "Local",
                    "llm_provider": "openai", "base_url": "http://127.0.0.1:8317",
                    "api_path": "/chat/completions", "model": "local-model",
                    "models": ["local-model"], "enabled": True,
                })
                self.assertEqual(created.status_code, 201)
                container.secret_store.calls.clear()
                response = client.post("/api/providers/local-provider/test", json={
                    "llm_provider": "openai", "openai_base_url": "http://127.0.0.1:8317",
                    "openai_model": "local-model", "openai_api_key": "must-not-pass",
                    "hf_endpoint": "",
                })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(container.secret_store.calls, [])
        self.assertTrue(service.test_calls["anonymous"])
        self.assertEqual(service.test_calls["openai_api_key"], "")
    def test_updating_provider_settings_invalidates_cached_agent_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = FakeContainer(Path(temp_dir))
            client = TestClient(create_app(container))

            response = client.put(
                "/api/provider-settings",
                json={
                    "llm_provider": "openai",
                    "openai_base_url": "http://127.0.0.1:8317",
                    "openai_model": "gpt-5.4",
                    "openai_api_key": "sk-test",
                    "hf_endpoint": "https://hf-mirror.com",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(container.invalidate_agent_graph_service_calls, 1)

    def test_provider_settings_test_returns_model_connection_error_without_agent_stage_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = FakeContainer(
                Path(temp_dir),
                settings_service=FailingSettingsService(
                    RuntimeError("APIConnectionError: Connection refused")
                ),
            )
            client = TestClient(create_app(container))

            response = client.post(
                "/api/provider-settings/test",
                json={
                    "llm_provider": "deepseek",
                    "openai_base_url": "http://127.0.0.1:8317",
                    "openai_model": "gpt-5.4",
                    "openai_api_key": "sk-test",
                    "hf_endpoint": "https://hf-mirror.com",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "无法连接模型服务，请检查网络和接口地址。")
        self.assertNotIn("理解问题", response.json()["detail"])
        self.assertNotIn("Connection refused", response.json()["detail"])

    def test_legacy_provider_writer_waits_for_model_dispatch_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            container = FakeContainer(root)
            client = TestClient(create_app(container))
            finished = Event()
            responses = []

            def update() -> None:
                responses.append(client.put(
                    "/api/provider-settings",
                    json={
                        "llm_provider": "openai",
                        "openai_base_url": "http://127.0.0.1:8317",
                        "openai_model": "gpt-5.4",
                        "openai_api_key": "sk-test",
                        "hf_endpoint": "https://hf-mirror.com",
                    },
                ))
                finished.set()

            with model_dispatch_authority_fence(root):
                worker = Thread(target=update)
                worker.start()
                self.assertFalse(finished.wait(0.1))
            worker.join(2)

        self.assertTrue(finished.is_set())
        self.assertEqual(responses[0].status_code, 200)


class FakeContainer:
    def __init__(self, root_dir: Path, *, settings_service=None) -> None:
        self.root_dir = root_dir
        self.config_path = root_dir / "config" / "settings.toml"
        self.settings_service = settings_service or FakeSettingsService()
        self.secret_store = RecordingSecretStore()
        self.invalidate_agent_graph_service_calls = 0

    def invalidate_agent_graph_service(self) -> None:
        self.invalidate_agent_graph_service_calls += 1


class FakeSettingsService:
    def update_provider_settings(self, **kwargs) -> ProviderSettings:
        return ProviderSettings(
            llm_provider=kwargs["llm_provider"],
            openai_base_url=kwargs["openai_base_url"],
            openai_model=kwargs["openai_model"],
            has_openai_api_key=bool(kwargs["openai_api_key"]),
            openai_api_key_masked="sk-****test",
            hf_endpoint=kwargs["hf_endpoint"],
        )


class RecordingSettingsService(FakeSettingsService):
    def __init__(self) -> None:
        self.test_calls = {}

    def test_provider_settings(self, **kwargs) -> str:
        self.test_calls = kwargs
        return "ok"


class RecordingSecretStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, key: str) -> str:
        self.calls.append(key)
        return "stored-secret"

    def has_secret(self, key: str) -> bool:
        self.calls.append(key)
        return True


class FailingSettingsService(FakeSettingsService):
    def __init__(self, error: Exception) -> None:
        self._error = error

    def test_provider_settings(self, **kwargs) -> str:
        del kwargs
        raise self._error


if __name__ == "__main__":
    unittest.main()
