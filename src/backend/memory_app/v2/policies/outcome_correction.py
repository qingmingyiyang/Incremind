"""Pure edit-window and excerpt bounds required by T12.15."""
from math import isfinite


def v1(request=None, *, operation, gap_seconds=None):
    if operation == 'limits':
        return {'edit_side_chars': 600, 'outcome_side_chars': 300}
    if operation == 'window':
        known = (type(gap_seconds) in (int, float) and isfinite(gap_seconds)
                 and gap_seconds >= 0)
        return {'merge': not known or gap_seconds <= 600,
                'mature': known and gap_seconds > 600}
    raise ValueError('invalid_outcome_correction_operation')
