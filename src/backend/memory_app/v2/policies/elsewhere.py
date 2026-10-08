"""Local navigation decisions; scores are supplied by the existing matcher."""
from math import isfinite
from .enough import coverage_terms, weighted_coverage

# Calibrated by the separate calibration partition in elsewhere.json.
THRESHOLD = .10
MARGIN = .07


def v1(*, question='', project_id='', coverage=0, has_hits=False, tagged=False,
       candidates=(), score=None, operation=None, calibration=(),
       coverage_questions=(), evidence='', terms_for=None):
    if operation == 'coverage':
        return weighted_coverage(coverage_terms(coverage_questions or [question], terms_for), evidence.casefold())
    if operation == 'calibrate':
        trials = []
        for threshold in (.05, .10, .15, .20, .35):
            for margin in (.03, .05, .07, .10, .15):
                correct = sum(_decide(**case['input'], candidates=candidates, score=score,
                    threshold=threshold, margin=margin) == case['expected'] for case in calibration)
                trials.append((correct, threshold, margin))
        correct, threshold, margin = max(trials)
        return {'total': len(calibration), 'correct': correct, 'threshold': threshold,
                'margin': margin, 'production_matches': (threshold, margin) == (THRESHOLD, MARGIN)}
    return _decide(question=question, project_id=project_id, coverage=coverage,
        has_hits=has_hits, tagged=tagged, candidates=candidates, score=score,
        operation=operation, threshold=THRESHOLD, margin=MARGIN)


def _decide(*, question, project_id, coverage, has_hits, tagged, candidates, score,
            threshold, margin, operation=None):
    if (type(coverage) not in (int, float) or not isfinite(coverage)
            or type(has_hits) is not bool or type(tagged) is not bool
            or not isinstance(question, str) or not question.strip()):
        return None
    eligible = not tagged and not (has_hits and coverage >= .6)
    if operation == 'eligible':
        return eligible
    if not eligible:
        return None
    ranked = []
    current = 0.0
    for candidate in candidates:
        value = max((score(question, text) for text in candidate['texts'] if text), default=0.0)
        if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1:
            continue
        identity = candidate['project_id']
        if identity == project_id:
            current = max(current, value)
        elif identity not in {'me', 'inbox'}:
            ranked.append((value, identity, candidate['scene']))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2] or ''))
    if not ranked or ranked[0][0] < threshold or ranked[0][0] - current < margin:
        return None
    best = ranked[0]
    # Two equally plausible destinations are not a clear navigation hint.
    competing = next((row for row in ranked[1:] if row[1] != best[1]), None)
    if competing and best[0] - competing[0] < margin:
        return None
    return {'project_id': best[1], 'scene': best[2]}
