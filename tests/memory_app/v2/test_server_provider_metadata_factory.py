"""服务器供应商元数据缺少原留痕工厂时拒绝构造。"""
import pytest

from backend.providers import ProviderRegistry
from backend.shared.server_resources import SharedResources, resource_context


def test_provider_registry_refuses_active_server_context_without_attribution_factory(tmp_path):
    root = tmp_path / 'users' / 'user-target'
    with resource_context(SharedResources(tmp_path)):
        with pytest.raises(ValueError, match=r'^server_provider_attribution_factory_unconfigured$'):
            ProviderRegistry(root)
    assert not (root / 'library/global/providers/providers.json').exists()
