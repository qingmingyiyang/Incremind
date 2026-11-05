import base64
import math
from time import monotonic
from urllib.parse import urlsplit

import httpx

from .model_transport import ModelTransportTimeout, _OwnedHTTPAttempt
from .litellm_gateway import _is_consumer_cancellation


_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def is_oxr_endpoint(base_url, model):
    if model != 'OXR-1.0':
        return False
    url = urlsplit(base_url)
    return (not url.username and not url.password and not url.query and not url.fragment
        and ((url.scheme == 'https' and url.hostname in
              {'free-oxr.somark.cn', 'free-oxr.somark.ai'} and url.path.rstrip('/') == '/api/v1')
             or (url.scheme in {'http', 'https'} and url.hostname in
                 _LOOPBACK_HOSTS and url.path.rstrip('/') == '/v1')))


class OxrImageGateway:
    def __init__(self, *, base_url, api_key_provider, egress_guard, **options):
        self.url = base_url.rstrip('/') + '/parse/sync'
        endpoint = urlsplit(base_url)
        self._trust_env = endpoint.hostname not in _LOOPBACK_HOSTS
        self._loopback_tls = not self._trust_env and endpoint.scheme == 'https'
        self.key = api_key_provider
        self.guard = egress_guard

    def complete_structured_with_usage(self, messages, *, response_model, timeout,
                                      wire_attempt_sink, retry_control=None, **options):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('oxr_timeout_invalid')
        deadline = monotonic() + timeout
        if retry_control is not None:
            retry_control.start(timeout)

        def remaining():
            left = deadline - monotonic()
            if left <= 0:
                raise TimeoutError('oxr_deadline_exceeded')
            if retry_control is not None:
                retry_control.checkpoint()
                left = min(left, retry_control.remaining())
            return left

        images = []
        for message in messages:
            for part in message.get('content', []):
                if not isinstance(part, dict) or part.get('type') != 'image_url':
                    continue
                uri = part['image_url']['url']
                header, encoded = uri.split(',', 1)
                mime = header.removeprefix('data:').removesuffix(';base64')
                if header != f'data:{mime};base64' or mime not in {
                        'image/png', 'image/jpeg', 'image/webp', 'image/gif', 'application/pdf'}:
                    raise ValueError('oxr_image_invalid')
                images.append((base64.b64decode(encoded, validate=True), mime))
        if not images:
            raise ValueError('oxr_images_required')
        outputs = []
        for data, mime in images:
            remaining()
            lease = self.guard('media_image_read', ('instructions', 'image'), len(data))
            handle = wire_attempt_sink.begin_model_wire_attempt()

            def wire():
                try:
                    self.guard('media_image_read', ('instructions', 'image'), len(data)).finish('succeeded')
                    # 每个 POST 独占连接和期限；禁止重定向及自动重发原件。
                    limits = (retry_control.limits if retry_control is not None else
                              {'header_timeout': timeout, 'idle_timeout': timeout})
                    transport_timeout = ModelTransportTimeout(remaining=remaining, limits=limits)
                    # 同机识别不经过环境代理，HTTPS 仍使用原有证书信任设置。
                    verify = httpx.create_ssl_context() if self._loopback_tls else True
                    client = httpx.Client(follow_redirects=False, timeout=float(transport_timeout),
                        trust_env=self._trust_env, verify=verify)
                    attempt = _OwnedHTTPAttempt(client, transport_timeout)
                    try:
                        response = client.post(self.url, data={'api_key': self.key(),
                            'output_formats': 'markdown',
                            'element_formats': '{"image":"none","formula":"latex","table":"markdown"}'},
                            files={'file': ('document.pdf' if mime == 'application/pdf' else
                                'image.' + mime.split('/')[1], data, mime)})
                        attempt.check()
                        response.raise_for_status()
                    finally:
                        attempt.close()
                    # 服务没有报告 token，用量保持未知。
                    handle.succeeded(usage={}, cache_observation=None)
                    return response
                except BaseException as error:
                    if (_is_consumer_cancellation(error)
                            or getattr(getattr(handle, 'control', None), 'cancel_requested', False)):
                        handle.consumer_cancelled()
                    else:
                        handle.failed_transport(error_code='oxr_request_failed')
                    raise

            try:
                response = handle.invoke_wire(wire)
                self.guard('media_image_read', ('instructions', 'image'), 0).finish('succeeded')
                payload = response.json()
                if (type(payload.get('code')) is not int or payload['code'] != 0
                        or payload.get('data', {}).get('error') is not None):
                    raise ValueError('oxr_provider_rejected')
                text = payload['data']['result']['outputs']['markdown']
                if not isinstance(text, str):
                    raise ValueError('oxr_output_invalid')
                outputs.append({'text': text, 'description': ''})
            except BaseException:
                lease.finish('failed', error_code='oxr_request_failed')
                raise
            lease.finish('succeeded')
        return response_model.model_validate({'images': outputs}), {}, None
