from copy import deepcopy
from weakref import WeakKeyDictionary

from backend.recognition import RecognitionConflict
from backend.recognition.external_evidence_json import encoded


_ISSUED = WeakKeyDictionary()


class PublishedLearningProof:
    __slots__ = ('__weakref__',)

    def __new__(cls):
        raise RecognitionConflict('steer_learning_publication_proof_not_issued')


def _issue_published_learning_proof(producer, *, reader, scope, recognition, seed):
    from .steer_learning_sources import SteerLearningSources
    if type(producer) is not SteerLearningSources:
        raise RecognitionConflict('steer_learning_publication_issuer_changed')
    view = producer.freshness._reader(reader)
    proof = object.__new__(PublishedLearningProof)
    _ISSUED[proof] = {'producer': producer, 'owner': producer.freshness.records,
        'transaction': view._transaction, 'scope': scope, 'recognition': recognition, 'seed': deepcopy(seed),
        'contexts': frozenset(encoded(context) for context in seed['contexts'])}
    return proof


def require_published_learning_context(proof, *, freshness, reader, context):
    if type(proof) is not PublishedLearningProof or proof not in _ISSUED:
        raise RecognitionConflict('steer_learning_publication_proof_not_issued')
    state = _ISSUED[proof]
    producer = state['producer']
    if producer.freshness is not freshness or state['owner'] is not freshness.records:
        raise RecognitionConflict('steer_learning_publication_consumer_changed')
    view = freshness._reader(reader)
    if view._transaction is not state['transaction'] or not view.connection.in_transaction:
        raise RecognitionConflict('steer_learning_publication_transaction_changed')
    if not isinstance(context, dict) or encoded(context) not in state['contexts']:
        raise RecognitionConflict('steer_learning_publication_context_changed')
    actual = producer._publication_seed(view, state['scope'], state['recognition'])
    if encoded(actual) != encoded(state['seed']):
        raise RecognitionConflict('steer_learning_publication_proof_changed')
