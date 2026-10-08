"""Bounded user review selection; no storage or model dependencies."""
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ReviewPolicy:
    window_days: int = 30
    minimum_sent: int = 5
    maximum_items: int = 5
    expiry_days: int = 14
    evidence_questions: int = 3
    feedback_answer_chars: int = 300
    feedback_history_turns: int = 64

    def __call__(self, items, *, now, dismissed=(), enabled=True):
        if not enabled:
            return []
        eligible = [item for item in items if item['review_key'] not in dismissed
                    and datetime.fromisoformat(item['created_at']) <= now
                    < datetime.fromisoformat(item['created_at']) + timedelta(days=self.expiry_days)]
        return sorted(eligible, key=lambda item: (-item['strength'], item.get('event_at', item['created_at']), item['id']))[:self.maximum_items]

    def feedback(self, kind, questions, answers):
        before = questions[0] + '\n' + answers[0][:self.feedback_answer_chars]
        after = questions[1] + '\n' + answers[1][:self.feedback_answer_chars] if kind == 'reask' else ''
        return before, after


v1 = ReviewPolicy()
