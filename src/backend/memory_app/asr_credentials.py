from contextlib import contextmanager
from pathlib import Path
import os

from backend.shared.server_resources import RESOURCE_POOL
from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence


class AsrCredentialError(ValueError):
    pass


@contextmanager
def asr_credential_authority(container, *, validate, independent=True):
    root = Path(container.root_dir).resolve()
    with model_dispatch_authority_fence(root):
        validate()
        resources = RESOURCE_POOL.get()
        factory = getattr(resources, 'server_model_access_for', None)
        if factory is None and os.environ.get('CHRIPTMAS_DEPLOY') == 'server':
            raise AsrCredentialError('asr_key_identity_unavailable')
        access = factory(root) if callable(factory) else None
        if independent and access is not None and access.public_selection('asr')['mode'] != 'independent':
            raise AsrCredentialError('server_model_configuration_readonly')
        yield


def store_asr_credential(container, value, *, validate=lambda: None):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 16 * 1024 or any(char in value for char in '\r\n\x00'):
        raise AsrCredentialError('asr_key_invalid')
    with asr_credential_authority(container, validate=validate):
        validate()
        # 与原 Hy-ASR 共用 container 的 SecretStore；密钥只交给原加密 owner。
        container.secret_store.set(TOKENHUB_ASR_SECRET_REF, value)
        return container.secret_store.get_generation(TOKENHUB_ASR_SECRET_REF)
