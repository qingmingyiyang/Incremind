from tools.method_eval import evaluate
from backend.memory_app.v2.policies import override


def test_real_offline_method_category_has_six_questions_with_life_cases():
    with override(retrieve='@1', compose='@1'):
        before = evaluate()
    with override(retrieve='@2', compose='@2'):
        after = evaluate()
    assert len(after['questions']) == 6
    assert sum(row['id'].startswith('method-life-') for row in after['questions']) >= 2
    assert before['hit_rate'] == 0
    assert after['hit_rate'] == 1
    assert after['over_supplement_rate'] == 0
    assert before['model_attempts'] == after['model_attempts'] == 0
