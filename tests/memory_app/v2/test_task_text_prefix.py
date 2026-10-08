import pytest
from backend.memory_app.kernel.policy_runtime import retry_policy_for
from backend.shared.llm.json_mode import PartialJSONField


@pytest.mark.parametrize(('raw', 'expected'), [
    ('{"type":"complete","summary":"完整段落。\\n\\n半截', True),
    ('{"type":"complete', False),
    ('{"summary":"完整段落。\\n\\n半截', False),
    ('{"type":"complete","summary":"done","capabil', False),
    ('{"type":"complete","summary":"done","type":"tool"}', False),
    ('{"type":"complete","summary":"one","summary":"two', False),
    ('{"type":"complete","summary":"done","arguments":{}}', False),
    ('{"type":"complete","summary":"done","unknown":1}', False),
    ('{"type":"complete","summary":"done","evidence_refs":[],"payload_ref":null}', True),
    ('{"type":"complete","summary":"quote: \\" fragment', True),
    ('{"type":"complete","summary":"bad\\u00', False),
    ('{"type":"complete","summary":"done"}broken', False),
])
def test_pure_text_worker_requires_closed_type_and_entire_valid_current_prefix(raw, expected):
    assert retry_policy_for()({'kind': 'pure_text_prefix', 'raw': raw}) is expected


def test_decoder_observes_every_current_prefix_even_after_parse_error():
    observed = []
    decoder = PartialJSONField('summary', observe=lambda value: observed.append(value))
    first = '{"type":"complete","summary":"完整段落。\\n\\n半截'
    assert decoder.feed(first) == '完整段落。\n\n半截'
    assert observed[-1]['raw'] == first
    decoder.feed('","capabil')
    assert observed[-1]['raw'] == first + '","capabil'
    assert retry_policy_for()({'kind': 'pure_text_prefix', 'raw': observed[-1]['raw']}) is False
    decoder.feed('\x00')
    assert observed[-1]['raw'].endswith('\x00')
    assert retry_policy_for()({'kind': 'pure_text_prefix', 'raw': observed[-1]['raw']}) is False
    original = PartialJSONField('summary')
    assert original.feed(first) == '完整段落。\n\n半截'
