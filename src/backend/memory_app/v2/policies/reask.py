"""Local lexical comparison for adjacent questions; never calls a model."""
import re
import unicodedata

WINDOW_SECONDS = 600
# Calibration corpus: lowest reask .888889, highest non-numeric new-topic .555556.
# Midpoint rounded conservatively; exact repeats precede follow-up cues.
OVERLAP_THRESHOLD = .722223
FOLLOWUP = re.compile(r'^(那|那么|然后|还有|另外|接着|具体|详细|为什么|比如|举例|其中|如果|what about|and |why|how about)', re.I)

def _normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).lower() if c.isalnum())

def _terms(text):
    text = _normalized(text)
    return {text[i:i+2] for i in range(len(text)-1)} or ({text} if text else set())

def v1(first, second, *, elapsed_seconds=0):
    left, right = _terms(first), _terms(second)
    score = len(left & right) / min(len(left), len(right)) if left and right else 0
    numbers_changed = set(re.findall(r'\d+', first)) != set(re.findall(r'\d+', second))
    kind = ('new_topic' if not 0 <= elapsed_seconds <= WINDOW_SECONDS or numbers_changed else
            'reask' if _normalized(first) and _normalized(first) == _normalized(second) else
            'followup' if FOLLOWUP.match(second.strip()) else
            'reask' if score >= OVERLAP_THRESHOLD else 'new_topic')
    return {'kind': kind, 'score': round(score, 6)}
