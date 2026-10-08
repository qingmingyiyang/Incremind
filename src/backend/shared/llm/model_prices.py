"""CNY catalog rates and exact arithmetic, independent of transport and storage.

Only the per-model rate declaration idea is borrowed from pi-ai's ModelCost.
Rates: https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ (2026-10-04).
2026 holidays: State Council notice, published by beijing.gov.cn, 2025-11-04.
Unverified historical prices and calendar years deliberately remain unknown.
"""
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from collections.abc import Mapping
import re
from urllib.parse import urlsplit

RATE_FIELDS = ('input_per_million', 'output_per_million', 'cache_read_per_million')
_DECIMAL = re.compile(r'(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,6})?\Z')
_BEIJING = timezone(timedelta(hours=8))
_HOLIDAYS_2026 = ((1, 1, 3), (2, 15, 23), (4, 4, 6), (5, 1, 5),
                  (6, 19, 21), (9, 25, 27), (10, 1, 7))


def validate_rates(value):
    """Null means unconfigured, never a free call; values are CNY per million."""
    if not isinstance(value, Mapping) or set(value) != set(RATE_FIELDS):
        raise ValueError('model_price_invalid')
    result = {}
    for field in RATE_FIELDS:
        rate = value[field]
        if rate is None:
            result[field] = None
            continue
        if type(rate) not in (str, int, float) or not _DECIMAL.fullmatch(str(rate)):
            raise ValueError('model_price_invalid')
        parsed = Decimal(str(rate))
        if not parsed.is_finite() or not 0 <= parsed <= 1_000_000:
            raise ValueError('model_price_invalid')
        result[field] = format(parsed, 'f')
    return result


@dataclass(frozen=True)
class ModelPrices:
    peak: tuple[str, str, str]
    off_peak: tuple[str, str, str]
    source: str = 'deepseek-cny-2026-10-04'

    def at(self, instant: datetime):
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            return None
        local = instant.astimezone(_BEIJING)
        if local.year != 2026 or local.date() < date(2026, 10, 4):
            return None
        holiday = any(local.month == month and first <= local.day <= last
                      for month, first, last in _HOLIDAYS_2026)
        minute = local.hour * 60 + local.minute
        peak = local.weekday() < 5 and not holiday and (540 <= minute < 720 or 840 <= minute < 1080)
        return dict(zip(RATE_FIELDS, self.peak if peak else self.off_peak))


def official_prices(provider, model, base_url):
    try:
        url = urlsplit(base_url)
        official = (provider.strip().lower() in {'openai', 'deepseek'} and url.scheme == 'https'
                    and url.hostname == 'api.deepseek.com' and url.port in (None, 443)
                    and url.path.rstrip('/') in {'', '/v1', '/beta'}
                    and not (url.username or url.password or url.query or url.fragment))
    except (ValueError, AttributeError):
        return None
    if official and model in {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}:
        return ModelPrices(('2', '8', '0.04'), ('1', '4', '0.02'))
    if official and model == 'deepseek-v4-pro':
        return ModelPrices(('9', '27', '0.30'), ('4.5', '13.5', '0.15'))
    return None


def calculate_cost(usage, cache, rates):
    """Require an actual complete input/output and an unambiguous cache split."""
    try:
        rates = validate_rates(rates)
    except ValueError:
        return None
    if (any(value is None for value in rates.values()) or not isinstance(usage, Mapping)
            or usage.get('observed_only') or usage.get('usage_status') == 'partial'):
        return None
    if cache is not None and not isinstance(cache, Mapping):
        return None
    incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
    if any(type(value) is not int or value < 0 for value in (incoming, outgoing)):
        return None
    if 'total_tokens' in usage and (type(usage['total_tokens']) is not int
                                   or usage['total_tokens'] != incoming + outgoing):
        return None
    if incoming == 0:
        cached = 0
        if cache and any(cache.get(key) not in (None, 0) for key in
                         ('cache_read_input_tokens', 'cache_write_input_tokens', 'uncached_input_tokens')):
            return None
    else:
        if not isinstance(cache, Mapping) or cache.get('cache_write_input_tokens') not in (None, 0):
            # This three-rate contract has no cache-write price or provider-specific
            # proof that writes are included in the normalized input count.
            return None
        cached = cache.get('cache_read_input_tokens')
        uncached = cache.get('uncached_input_tokens')
        if cached is None and type(uncached) is int:
            cached = incoming - uncached
        if type(cached) is not int or not 0 <= cached <= incoming:
            return None
        if uncached is not None and (type(uncached) is not int or uncached != incoming - cached):
            return None
    return (Decimal(incoming - cached) * Decimal(rates['input_per_million'])
            + Decimal(cached) * Decimal(rates['cache_read_per_million'])
            + Decimal(outgoing) * Decimal(rates['output_per_million'])) / Decimal(1_000_000)
