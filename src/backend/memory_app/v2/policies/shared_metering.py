from collections.abc import Mapping
from decimal import Decimal
from urllib.parse import urlsplit


VERSION = '@1'
_FIELDS = {
    'input_tokens': {'input_per_million'},
    'search_tokens_calls': {'input_per_million', 'output_per_million',
                            'cache_read_per_million', 'search_per_thousand'},
    'audio_seconds': {'audio_per_second'},
    'search_requests': {'request_per_thousand'},
}


def validate_price(value):
    if not isinstance(value, Mapping) or value.get('unit') not in _FIELDS:
        raise ValueError('server_model_metering_price_invalid')
    fields = _FIELDS[value['unit']]
    if set(value) != {'unit', 'rates'} or not isinstance(value['rates'], Mapping) or set(value['rates']) != fields:
        raise ValueError('server_model_metering_price_invalid')
    rates = {}
    for field, raw in value['rates'].items():
        if not isinstance(raw, str) or len(raw) > 24:
            raise ValueError('server_model_metering_price_invalid')
        try:
            number = Decimal(raw)
        except ArithmeticError:
            raise ValueError('server_model_metering_price_invalid') from None
        if not number.is_finite() or not 0 <= number <= 1_000_000 or number.as_tuple().exponent < -6:
            raise ValueError('server_model_metering_price_invalid')
        rates[field] = format(number, 'f')
    return {'unit': value['unit'], 'rates': rates}


def _official(base, host, paths):
    address = urlsplit(base)
    return (address.scheme == 'https' and address.hostname == host and address.port in {None, 443}
        and address.path.rstrip('/') in paths
        and not (address.username or address.password or address.query or address.fragment))


def _integer(value):
    return type(value) is int and value >= 0


class MeteringV1:
    version = VERSION

    def bound(self, *, purpose, provider, base_url, model, request):
        if not isinstance(request, Mapping) or not isinstance(request.get('payload'), Mapping):
            raise ValueError('server_model_balance_bound_unavailable')
        payload = request['payload']
        if payload.get('model') != model:
            raise ValueError('server_model_balance_bound_unavailable')
        if (purpose == 'embedding' and provider == 'openai'
                and model in {'text-embedding-3-small', 'text-embedding-3-large'}
                and _official(base_url, 'api.openai.com', {'', '/v1'})
                and _official(request.get('endpoint', ''), 'api.openai.com', {'/v1/embeddings'})):
            inputs = payload.get('input')
            if (not isinstance(inputs, list) or not 1 <= len(inputs) <= 2048
                    or any(not isinstance(text, str) or not text for text in inputs)):
                raise ValueError('server_model_balance_payload_invalid')
            return {'version': VERSION, 'protocol': 'openai_embeddings', 'unit': 'input_tokens',
                'limits': {'input_tokens': 300000},
                'source': 'https://developers.openai.com/api/reference/resources/embeddings/methods/create'}
        if (purpose == 'rerank' and provider == 'cohere'
                and model in {'rerank-v5.0-pro', 'rerank-v5.0-fast'}
                and _official(base_url, 'api.cohere.com', {'', '/v2'})
                and _official(request.get('endpoint', ''), 'api.cohere.com', {'/v2/rerank'})):
            documents = payload.get('documents')
            if (not isinstance(documents, list) or not 1 <= len(documents) <= 10000
                    or any(not isinstance(text, str) or not text for text in documents)
                    or not isinstance(payload.get('query'), str) or not payload['query']
                    or payload.get('max_tokens_per_doc') != 4096):
                raise ValueError('server_model_balance_payload_invalid')
            return {'version': VERSION, 'protocol': 'cohere_rerank_v5', 'unit': 'input_tokens',
                'limits': {'input_tokens': len(documents) * 32768},
                'source': 'https://docs.cohere.com/docs/reranking-best-practices'}
        raise ValueError('server_model_balance_bound_unavailable')

    def reserve(self, bound, price):
        price = validate_price(price)
        if price['unit'] != bound['unit']:
            raise ValueError('server_model_metering_price_invalid')
        if bound['unit'] == 'input_tokens':
            return Decimal(bound['limits']['input_tokens']) * Decimal(price['rates']['input_per_million']) / 1000000
        raise ValueError('server_model_balance_bound_unavailable')

    def observe(self, bound, response):
        if not isinstance(response, Mapping):
            return None
        if bound['protocol'] == 'openai_embeddings':
            usage = response.get('usage')
            if isinstance(usage, Mapping):
                incoming, total = usage.get('prompt_tokens'), usage.get('total_tokens')
                if _integer(incoming) and _integer(total) and incoming == total:
                    return {'input_tokens': incoming}
        elif bound['protocol'] == 'cohere_rerank_v5':
            meta = response.get('meta')
            billed = meta.get('billed_units') if isinstance(meta, Mapping) else None
            if isinstance(billed, Mapping) and _integer(billed.get('input_tokens')):
                return {'input_tokens': billed['input_tokens']}
        return None

    def charge(self, bound, price, observation):
        if not isinstance(observation, Mapping) or set(observation) != set(bound['limits']):
            return None, 'usage_unavailable'
        if any(not _integer(value) for value in observation.values()):
            return None, 'usage_invalid'
        if any(value > bound['limits'][key] for key, value in observation.items()):
            return None, 'usage_exceeds_bound'
        if bound['unit'] == 'input_tokens':
            return (Decimal(observation['input_tokens']) * Decimal(price['rates']['input_per_million']) / 1000000), None
        return None, 'usage_unavailable'


class MeteringV2(MeteringV1):
    version = '@2'

    def bound(self, *, purpose, provider, base_url, model, request):
        if purpose != 'search' or provider != 'brave':
            return super().bound(purpose=purpose, provider=provider, base_url=base_url, model=model, request=request)
        if (model != 'brave-web-search' or not _official(base_url, 'api.search.brave.com', {'/res/v1'})
                or not isinstance(request, Mapping) or request.get('method') != 'POST'
                or not _official(request.get('endpoint', ''), 'api.search.brave.com', {'/res/v1/web/search'})):
            raise ValueError('server_model_balance_bound_unavailable')
        payload = request.get('payload')
        if (not isinstance(payload, Mapping) or set(payload) != {'q', 'count', 'offset', 'result_filter'}
                or not isinstance(payload['q'], str) or not 1 <= len(payload['q']) <= 600
                or len(payload['q'].split()) > 75 or not payload['q'].strip()
                or type(payload['count']) is not int or not 1 <= payload['count'] <= 20
                or type(payload['offset']) is not int or payload['offset'] != 0
                or payload['result_filter'] != ['web']):
            raise ValueError('server_model_balance_payload_invalid')
        return {'version': '@2', 'protocol': 'brave_web_search', 'unit': 'search_requests',
            'limits': {'requests': 1}, 'source': 'https://api-dashboard.search.brave.com/app/plans?tab=normal'}

    def reserve(self, bound, price):
        if bound['unit'] != 'search_requests':
            return super().reserve(bound, price)
        price = validate_price(price)
        if price['unit'] != bound['unit']:
            raise ValueError('server_model_metering_price_invalid')
        return Decimal(price['rates']['request_per_thousand']) / 1000

    def observe(self, bound, response):
        if bound['protocol'] != 'brave_web_search':
            return super().observe(bound, response)
        # 该观察由已完成且通过响应合同校验的单次 HTTP 边界提供。
        if (isinstance(response, Mapping) and response == {'completed_requests': 1}
                and type(response['completed_requests']) is int):
            return {'requests': 1}
        return None

    def charge(self, bound, price, observation):
        if bound['unit'] != 'search_requests':
            return super().charge(bound, price, observation)
        if observation != {'requests': 1} or type(observation.get('requests')) is not int:
            return None, 'usage_unavailable'
        return Decimal(price['rates']['request_per_thousand']) / 1000, None


class MeteringV3(MeteringV2):
    version = '@3'

    def asr_configuration(self, *, provider, base_url, model):
        return (provider == 'tokenhub-asr' and model == 'hy-asr-3.0-preview'
            and _official(base_url, 'tokenhub.tencentmaas.com', {'/v1', '/v1/wand/asrproxy/sync_transcribe'}))

    def bound(self, *, purpose, provider, base_url, model, request):
        if purpose != 'asr':
            return super().bound(purpose=purpose, provider=provider, base_url=base_url, model=model, request=request)
        host = 'tokenhub.tencentmaas.com'
        path = '/v1/wand/asrproxy/sync_transcribe'
        if (not self.asr_configuration(provider=provider, base_url=base_url, model=model)
                or not isinstance(request, Mapping) or request.get('method') != 'POST'
                or not _official(request.get('endpoint', ''), host, {path})):
            raise ValueError('server_model_balance_bound_unavailable')
        payload = request.get('payload')
        if (not isinstance(payload, Mapping) or set(payload) != {'model', 'data', 'voice_encode_format'}
                or payload['model'] != model or payload['voice_encode_format'] != 'wav'
                or not isinstance(payload['data'], str) or not payload['data']):
            raise ValueError('server_model_balance_payload_invalid')
        return {'version': '@3', 'protocol': 'tokenhub_sync_asr', 'unit': 'audio_seconds',
            'limits': {'duration_ms': 1800000},
            'source': 'https://cloud.tencent.com/document/product/1823/135791'}

    def reserve(self, bound, price):
        if bound['unit'] != 'audio_seconds':
            return super().reserve(bound, price)
        price = validate_price(price)
        if price['unit'] != bound['unit']:
            raise ValueError('server_model_metering_price_invalid')
        return Decimal(bound['limits']['duration_ms']) * Decimal(price['rates']['audio_per_second']) / 1000

    def observe(self, bound, response):
        if bound['protocol'] != 'tokenhub_sync_asr':
            return super().observe(bound, response)
        if not isinstance(response, Mapping) or response.get('status') != 'completed':
            return None
        output, usage = response.get('output'), response.get('usage')
        if not isinstance(output, Mapping):
            return None
        tokens = usage.get('total_token') if isinstance(usage, Mapping) else None
        duration = output.get('duration_ms')
        sentences = output.get('sentences')
        timestamps = (isinstance(sentences, list) and all(isinstance(sentence, Mapping)
            and _integer(sentence.get('begin_ms')) and _integer(sentence.get('end_ms'))
            and sentence['begin_ms'] <= sentence['end_ms'] for sentence in sentences))
        return {'duration_ms': duration if _integer(duration) else None,
            'usage_total_token': tokens if _integer(tokens) else None,
            'sentence_end_ms': max((sentence['end_ms'] for sentence in sentences), default=0) if timestamps else None}

    def charge(self, bound, price, observation):
        if bound['unit'] != 'audio_seconds':
            return super().charge(bound, price, observation)
        if not isinstance(observation, Mapping) or not _integer(observation.get('duration_ms')):
            return None, 'usage_unavailable'
        duration = observation['duration_ms']
        if duration > bound['limits']['duration_ms']:
            return None, 'usage_exceeds_bound'
        if (duration <= 0 or not _integer(observation.get('sentence_end_ms'))
                or observation['sentence_end_ms'] > duration):
            return None, 'usage_invalid'
        return Decimal(duration) * Decimal(price['rates']['audio_per_second']) / 1000, None


def get(*, version):
    if version == '@1':
        return MeteringV1()
    if version == '@2':
        return MeteringV2()
    if version == '@3':
        return MeteringV3()
    raise ValueError('unknown_shared_metering_version')
