from datetime import datetime
from decimal import Decimal

import pytest

from backend.shared.llm.model_capabilities import resolve_model_capabilities


def rates():
    return {'input_per_million': '2', 'output_per_million': '3', 'cache_read_per_million': '0.1'}


def calculate(usage, cache, price=None):
    from backend.shared.llm.model_prices import calculate_cost
    return calculate_cost(usage, cache, rates() if price is None else price)


def test_cache_reads_replace_full_input_price_without_double_counting():
    assert calculate({'input_tokens': 1000, 'output_tokens': 600},
        {'cache_read_input_tokens': 200, 'uncached_input_tokens': 800}) == Decimal('0.00342')


@pytest.mark.parametrize(('usage', 'cache', 'price'), [
    (None, None, rates()),
    ({'total_tokens': 1000}, None, rates()),
    ({'input_tokens': 1000}, {'cache_read_input_tokens': 0}, rates()),
    ({'input_tokens': 1000, 'output_tokens': 1}, None, rates()),
    ({'input_tokens': 1000, 'output_tokens': 1}, {'cache_read_input_tokens': 2000}, rates()),
    ({'input_tokens': 1000, 'output_tokens': 1}, {'cache_read_input_tokens': 200, 'uncached_input_tokens': 900}, rates()),
    ({'input_tokens': 1000, 'output_tokens': 1}, {'cache_read_input_tokens': 0, 'cache_write_input_tokens': 2}, rates()),
    ({'input_tokens': True, 'output_tokens': 1}, {'cache_read_input_tokens': 0}, rates()),
    ({'input_tokens': 1, 'output_tokens': 1, 'observed_only': True}, {'cache_read_input_tokens': 0}, rates()),
    ({'input_tokens': 1, 'output_tokens': 1, 'usage_status': 'partial'}, {'cache_read_input_tokens': 0}, rates()),
    ({'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 3}, {'cache_read_input_tokens': 0}, rates()),
    ({'input_tokens': 0, 'output_tokens': 0}, [], rates()),
    ({'input_tokens': 1, 'output_tokens': 1}, {'cache_read_input_tokens': 0}, {'input_per_million': None}),
])
def test_unknown_partial_or_inconsistent_counters_and_prices_remain_unknown(usage, cache, price):
    assert calculate(usage, cache, price) is None


def test_reported_zero_is_zero_but_unknown_price_is_not_zero():
    assert calculate({'input_tokens': 0, 'output_tokens': 0}, None) == Decimal('0')
    assert calculate({'input_tokens': 0, 'output_tokens': 0}, None, {'input_per_million': None}) is None


@pytest.mark.parametrize(('at', 'expected'), [
    ('2026-10-08T08:59:59+08:00', '1'), ('2026-10-08T09:00:00+08:00', '2'),
    ('2026-10-08T12:00:00+08:00', '1'), ('2026-10-08T14:00:00+08:00', '2'),
    ('2026-10-08T18:00:00+08:00', '1'), ('2026-10-10T10:00:00+08:00', '1'),
    ('2026-10-05T10:00:00+08:00', '1'), ('2026-10-08T01:00:00Z', '2'),
])
def test_official_cny_rates_follow_beijing_hours_holidays_and_weekdays(at, expected):
    price = resolve_model_capabilities('openai', 'deepseek-flash', 'https://api.deepseek.com/v1').prices
    assert price.at(datetime.fromisoformat(at.replace('Z', '+00:00')))['input_per_million'] == expected


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp', 'deepseek-v4-pro'])
def test_only_current_documented_model_names_have_cny_declarations(model):
    price = resolve_model_capabilities('openai', model, 'https://api.deepseek.com').prices
    assert price.at(datetime.fromisoformat('2026-10-08T10:00:00+08:00'))['output_per_million'] == ('27' if model == 'deepseek-v4-pro' else '8')


@pytest.mark.parametrize(('model', 'url'), [
    ('deepseek-chat', 'https://api.deepseek.com'), ('deepseek-reasoner', 'https://api.deepseek.com'),
    ('deepseek-flash', 'https://proxy.invalid/v1'), ('deepseek-flash', 'https://api.deepseek.com.evil.invalid'),
    ('deepseek-flash', 'http://api.deepseek.com'), ('deepseek-flash', 'https://api.deepseek.com:8443'),
    ('deepseek-flash', 'https://api.deepseek.com/proxy'),
])
def test_proxy_unknown_and_old_names_do_not_borrow_official_rates(model, url):
    assert resolve_model_capabilities('openai', model, url).prices is None


@pytest.mark.parametrize('at', ['2026-09-01T10:00:00+08:00', '2027-01-08T10:00:00+08:00'])
def test_unverified_history_and_calendar_year_are_unknown(at):
    price = resolve_model_capabilities('openai', 'deepseek-flash', 'https://api.deepseek.com').prices
    assert price.at(datetime.fromisoformat(at)) is None


@pytest.mark.parametrize('value', ['-1', 'NaN', 'Infinity', True, {}, '1e20', '0.1234567'])
def test_manual_rates_are_bounded_finite_decimals(value):
    from backend.shared.llm.model_prices import validate_rates
    with pytest.raises(ValueError, match='model_price_invalid'):
        validate_rates({**rates(), 'input_per_million': value})
