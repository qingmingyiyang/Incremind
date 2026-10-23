"""Answer diagnostics keep fixed codes and real runtime-log correlation only."""
import logging

import pytest

from backend.memory_app.answer_diagnostics import log_answer_validation_failure
from backend.shared.runtime_logging import RuntimeLogHandler, runtime_log_scope


@pytest.mark.parametrize(('message', 'compose', 'expected_code', 'expected_compose'), [
    ('invalid_first_fact_requires_correction', '@34', 'invalid_first_fact_requires_correction', '@34'),
    ('grounded_calendar_provenance_invalid', '@35', 'grounded_calendar_provenance_invalid', '@35'),
    ('grounded_calendar_provenance_invalid', '@36', 'grounded_calendar_provenance_invalid', '@36'),
    ('grounded_calendar_provenance_invalid private question and quote', '@35 private model input',
     'answer_validation_failed', 'other'),
])
def test_answer_diagnostics_preserve_safe_code_and_disk_correlation(
        tmp_path, message, compose, expected_code, expected_compose):
    root = tmp_path / 'logs'
    root.mkdir()
    handler = RuntimeLogHandler(root)
    logger = logging.getLogger('backend.memory_app.answer_diagnostics')
    logger.addHandler(handler)
    try:
        with runtime_log_scope(root, request_id='request-synthetic', turn_id='turn-synthetic'):
            try:
                raise ValueError(message)
            except ValueError as error:
                assert log_answer_validation_failure(error, compose_version=compose) == expected_code
    finally:
        logger.removeHandler(handler)
        handler.close()
    [path] = root.glob('app-*.log')
    text = path.read_text(encoding='utf8')
    assert f'ask_preview_failed code={expected_code} stage=answer_validation compose={expected_compose}' in text
    assert 'request_id=request-synthetic turn_id=turn-synthetic' in text
    assert 'private question and quote' not in text and 'private model input' not in text
    assert 'logged_exception' not in text and 'Traceback' not in text
