"""Bounded answer-validation diagnostics without exception or answer text."""
import logging


_LOGGER = logging.getLogger(__name__)
_KNOWN_CODES = frozenset({
    'invalid_first_fact_requires_correction',
    'grounded_calendar_provenance_invalid',
})
_COMPOSE_VERSIONS = frozenset(f'@{version}' for version in range(1, 36))


def log_answer_validation_failure(error: BaseException, *, compose_version: object) -> str:
    """Log only exact product-owned codes and return the safe diagnostic code."""
    code = 'answer_validation_failed'
    if (type(error) is ValueError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in _KNOWN_CODES):
        code = error.args[0]
    compose = compose_version if type(compose_version) is str and compose_version in _COMPOSE_VERSIONS else 'other'
    _LOGGER.warning('ask_preview_failed code=%s stage=answer_validation compose=%s', code, compose)
    return code
