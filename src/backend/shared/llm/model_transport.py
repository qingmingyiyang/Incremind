"""Timeouts on existing synchronous HTTPX responses, without a reader thread."""
from collections.abc import Callable, Mapping
import math
import socket
from threading import Event, RLock, Timer

import httpx


class ModelTransportError(RuntimeError):
    """Sanitized transport phase; no provider payload or credentials."""
    def __init__(self, kind: str):
        if kind not in {'header_timeout', 'stalled', 'malformed_stream', 'content_filter', 'provider_rejected', 'provider_close_failed'}:
            raise ValueError('invalid_model_transport_error')
        self.kind = kind
        super().__init__(kind)


_CLOSED_PROVIDER_SEAL = object()


class _ProviderClosed:
    def __init__(self, seal):
        if seal is not _CLOSED_PROVIDER_SEAL:
            raise ValueError('invalid_provider_close_witness')


def _provider_closed_witness():
    return _ProviderClosed(_CLOSED_PROVIDER_SEAL)


def is_provider_closed_witness(value):
    return type(value) is _ProviderClosed


class ModelInterrupted(RuntimeError):
    """Safe, unvalidated partial text; never a successful model result."""
    def __init__(self, *, partial, interruption, close_witness=None):
        self.partial, self.interruption = partial, interruption
        self.close_witness = close_witness
        super().__init__('model_output_interrupted')


class ModelContinuationFailed(RuntimeError):
    """A resumed model failed; this grants no close witness or execution right."""
    def __init__(self):
        super().__init__('model_continuation_failed')


class ModelTransportTimeout(float):
    """SDK-compatible header timeout carrying an injected live body deadline.

    HTTPX timeout/network_stream extensions and public SDK response objects are
    used; there is no background next(), provider selection or request replay.
    Borrowed HTTP/2 sessions are never aborted; an explicitly owned attempt pool
    can close its own socket without affecting other sessions.
    """
    def __new__(cls, *, remaining: Callable[[], float], limits: Mapping[str, object],
                header_retry: bool = False, owned_client_factory=None):
        header, idle = float(limits['header_timeout']), float(limits['idle_timeout'])
        if any(not math.isfinite(value) or value <= 0 for value in (header, idle)):
            raise ValueError('invalid_model_transport_timeout')
        value = remaining() if header_retry else min(header, remaining())
        instance = super().__new__(cls, value)
        instance.remaining, instance.idle = remaining, idle
        instance.owned_client_factory = owned_client_factory
        return instance

    def header_error(self, error):
        current = error
        seen = set()
        for _ in range(5):
            if isinstance(current, ModelTransportError):
                return current
            if isinstance(current, httpx.ReadTimeout):
                return ModelTransportError('header_timeout')
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return error

    def bind_completion(self, completion):
        if isinstance(completion, _OwnedModelStream):
            return completion
        # OpenAI Stream.response and LiteLLM CustomStreamWrapper.completion_stream
        # are public objects. Unknown SDK wrappers retain their own timeout.
        stream = getattr(completion, 'completion_stream', completion)
        response = getattr(stream, 'response', None)
        if isinstance(response, httpx.Response):
            self.bind_response(response)
            return _ClosingProviderStream(completion, stream)
        return completion

    def bind_response(self, response: httpx.Response, *, owns_deadline=False):
        if isinstance(response.stream, _DeadlineByteStream):
            return
        try:
            remaining = self.remaining()
            # HTTPCore reads this public extension when body iteration begins,
            # after its separate header read has already finished.
            timeouts = response.request.extensions.setdefault('timeout', {})
            timeouts['read'] = min(self.idle, remaining)
            response.stream = _DeadlineByteStream(response.stream, response, self,
                owns_deadline=owns_deadline)
        except BaseException:
            response.close()
            raise


def complete_with_owned_transport(completion, request):
    """Opt-in transport for the existing LiteLLM OpenAI-compatible factory.

    The original SDK retains credentials/protocol conversion. Its cached client
    is borrowed; only a fresh client explicitly returned by its HTTP owner is
    decorated and closed. There is no guessed clone of session/TLS settings.
    """
    timeout = request.get('timeout')
    if not isinstance(timeout, ModelTransportTimeout) or not str(request.get('model', '')).startswith('openai/'):
        return completion(**request)
    import litellm
    from litellm.main import openai_chat_completions
    factory = timeout.owned_client_factory
    if factory is None and litellm.client_session is not None:
        raise ModelTransportError('provider_rejected')
    sdk = openai_chat_completions._get_openai_client(is_async=False,
        api_key=request.get('api_key'), api_base=request.get('api_base'),
        timeout=timeout, max_retries=0, organization=request.get('organization'),
        client=request.get('client'))
    client = (factory or openai_chat_completions._get_sync_http_client)()
    if (not isinstance(client, httpx.Client) or client.is_closed
            or client is litellm.client_session or client is sdk._client):
        raise ModelTransportError('provider_rejected')
    attempt = _OwnedHTTPAttempt(client, timeout)
    try:
        result = completion(**{**request, 'client': sdk.with_options(http_client=client, max_retries=0)})
        attempt.check()
        if request.get('stream'):
            return _OwnedModelStream(result, attempt)
    except BaseException as error:
        attempt.close()
        attempt.check()
        raise timeout.header_error(error) from None
    attempt.close()
    return result


class _OwnedHTTPAttempt:
    """Timers affect only sockets in this attempt's explicitly owned pool."""
    def __init__(self, client, timeout):
        self.client, self.timeout = client, timeout
        self.lock, self.sockets = RLock(), []
        self.failure, self.closed = None, False
        self.header = None
        self.header_deadline = None
        self.total = Timer(timeout.remaining(), lambda: self._expire('total'))
        self.total.daemon = True
        self.total.start()
        client.event_hooks['request'].insert(0, self.request)
        client.event_hooks['response'].insert(0, self.response)

    def _expire(self, kind):
        with self.lock:
            if self.closed:
                return
            self.failure = self.failure or kind
            sockets = list(self.sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check(self):
        if self.failure == 'header':
            raise ModelTransportError('header_timeout')
        if self.failure == 'total':
            raise TimeoutError('model request deadline exceeded')
        self.timeout.remaining()

    def request(self, request):
        from time import monotonic
        self.check()
        if self.header_deadline is None:
            self.header_deadline = monotonic() + float(self.timeout)
        self.header = Timer(max(0, self.header_deadline - monotonic()), lambda: self._expire('header'))
        self.header.daemon = True
        self.header.start()
        original = request.extensions.get('trace')

        def trace(name, info):
            if original is not None:
                original(name, info)
            if name.endswith(('connect_tcp.complete', 'connect_unix_socket.complete', 'start_tls.complete')):
                stream = info['return_value']
                sock = stream.get_extra_info('socket')
                if isinstance(sock, socket.socket):
                    with self.lock:
                        self.sockets.append(sock)
                    if self.failure is not None:
                        self._expire(self.failure)
                self.check()

        request.extensions['trace'] = trace

    def response(self, response):
        self._stop(self.header)
        self.check()
        self.timeout.bind_response(response, owns_deadline=True)

    @staticmethod
    def _stop(timer):
        if timer is not None:
            timer.cancel()
            timer.join()

    def close(self):
        if self.closed:
            return
        self._stop(self.header)
        self._stop(self.total)
        try:
            self.client.close()
        except Exception:
            raise ModelTransportError('provider_close_failed') from None
        self.closed = True


class _OwnedModelStream:
    def __init__(self, stream, attempt):
        self.stream, self.iterator, self.attempt = stream, iter(stream), attempt
        self.closed = False

    @property
    def completion_stream(self):
        return getattr(self.stream, 'completion_stream', self.stream)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except BaseException as error:
            self.close()
            self.attempt.check()
            raise self.attempt.timeout.header_error(error) from None

    def close(self):
        if self.closed:
            return
        provider = getattr(self.stream, 'completion_stream', self.stream)
        close = getattr(provider, 'close', None)
        try:
            if callable(close):
                close()
        except Exception:
            self.attempt.close()
            raise ModelTransportError('provider_close_failed') from None
        self.attempt.close()
        self.closed = True


class _ClosingProviderStream:
    """Give the existing SDK wrapper its actual public provider close operation."""
    def __init__(self, outer, provider):
        self.iterator, self.provider = iter(outer), provider

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except Exception as error:
            current, seen = error, set()
            while current is not None and id(current) not in seen:
                if isinstance(current, ModelTransportError):
                    raise current from None
                seen.add(id(current))
                current = current.__cause__ or current.__context__
            raise

    def close(self):
        self.provider.close()


class _DeadlineByteStream(httpx.SyncByteStream):
    def __init__(self, inner, response, timeout, *, owns_deadline=False):
        self.inner, self.response, self.timeout = inner, response, timeout
        self.expired, self.closed, self.close_failed = Event(), False, False
        self.timer = None
        network = response.extensions.get('network_stream')
        self.socket = network.get_extra_info('socket') if network is not None else None
        # Socket shutdown is safe only for a non-multiplexed HTTP/1 response.
        if (not owns_deadline and response.extensions.get('http_version') in {b'HTTP/1.0', b'HTTP/1.1'}
                and isinstance(self.socket, socket.socket)):
            self.timer = Timer(timeout.remaining(), self._expire)
            self.timer.daemon = True
            self.timer.start()

    def _expire(self):
        self.expired.set()
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        # Foreground iteration unwinds and closes the response before any retry.

    def __iter__(self):
        try:
            for data in self.inner:
                self.timeout.remaining()
                if self.expired.is_set():
                    raise TimeoutError('model request deadline exceeded')
                yield data
            self.timeout.remaining()
        except httpx.ReadTimeout:
            if self.expired.is_set():
                raise TimeoutError('model request deadline exceeded') from None
            raise ModelTransportError('stalled') from None
        except (httpx.ReadError, httpx.RemoteProtocolError):
            if self.expired.is_set():
                raise TimeoutError('model request deadline exceeded') from None
            raise
        finally:
            self.close()

    def close(self):
        if self.close_failed:
            raise ModelTransportError('provider_close_failed')
        if self.closed:
            return
        if self.timer is not None:
            self.timer.cancel()
            self.timer.join()
        try:
            self.inner.close()
        except Exception:
            self.close_failed = True
            raise ModelTransportError('provider_close_failed') from None
        self.closed = True
