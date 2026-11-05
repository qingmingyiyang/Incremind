from copy import deepcopy
from types import SimpleNamespace

from backend.recognition import RecognitionConflict
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _now
from .links import InsightLinks
from .policies import get
from .privacy import is_private_project

EXAMPLES = 'v2_route_examples'


class RouteExamples:
    def __init__(self, records, models, *, origin_for, source_allowed=None):
        self.records, self.models, self.origin_for = records, models, origin_for
        self.source_allowed = source_allowed

    def stage(self, tx, turn_id, *, parts, corrected=False, source_turn_id=None):
        from .route_corrections import source_parts, CORRECTIONS
        row = tx.read('v2_turns', turn_id)
        if row is None:
            raise ValueError('route_example_source_missing')
        canonical = source_parts(self.records, row, reader=TransactionRecords(tx))
        actual = [{key: part[key] for key in ('intent', 'span', 'depends_on', 'instruction')} for part in canonical]
        if parts != actual:
            raise RecognitionConflict('route_example_parts_changed')
        for part in canonical:
            if part['turn_id'] is None:
                raise RecognitionConflict('route_example_source_not_completed')
            child = tx.read('v2_turns', part['turn_id'])
            value = child.payload
            if (value['intent'] in {'remember', 'do'} and value['receipt'][value['intent']]['state'] not in {'done', 'partial'}
                    or value['intent'] == 'ask' and value['receipt']['ask'].get('error')):
                raise RecognitionConflict('route_example_source_not_completed')
        if corrected:
            marker = tx.read(CORRECTIONS, source_turn_id)
            if marker is None or marker.payload['replacement_turn_id'] != turn_id:
                raise RecognitionConflict('route_example_correction_missing')
        origin = self.origin_for(turn_id, reader=TransactionRecords(tx))
        if (not isinstance(origin, dict) or origin.get('kind') not in {'personal', 'shared'}
                or not isinstance(origin.get('namespace'), str) or not origin['namespace']):
            raise RecognitionConflict('route_example_origin_missing')
        payload = {'project_id': row.payload['project_id'],
            'text': row.payload['user_text'], 'parts': deepcopy(parts), 'corrected': corrected,
            'source_turn_id': source_turn_id or turn_id, 'result_turn_id': turn_id,
            'source_revision': row.revision, 'origin': deepcopy(origin), 'deleted': not bool(parts),
            'at': _now()}
        current = tx.read(EXAMPLES, turn_id)
        if current is not None:
            if current.payload.get('deleted'):
                return
            stable = lambda value: {key: item for key, item in value.items() if key not in {'source_revision', 'at'}}
            if stable(current.payload) != stable(payload):
                raise RecognitionConflict('route_example_source_changed')
            if current.payload['source_revision'] == row.revision:
                return
            payload['at'] = current.payload['at']
        tx.put(EXAMPLES, turn_id, payload, expected_revision=current.revision if current else 0)

    def complete(self, turn_id):
        from .route_corrections import source_parts
        row = self.records.read('v2_turns', turn_id)
        if row is None:
            raise ValueError('route_example_source_missing')
        if row.payload.get('receipt', {}).get('reminder') is not None:
            return False
        if row.payload.get('intent') == 'multi' and any(
                self.records.read('v2_turns', identity) is None for identity in row.payload['part_turn_ids']):
            return False
        try:
            parts = source_parts(self.records, row)
        except RecognitionConflict as error:
            if str(error) == 'route_correction_source_unfinished':
                return False
            raise
        for part in parts:
            child = self.records.read('v2_turns', part['turn_id'])
            if child.payload['intent'] == 'ask' and child.payload['receipt']['ask'].get('error'):
                return
            if child.payload['intent'] in {'remember', 'do'} and child.payload['receipt'][child.payload['intent']]['state'] not in {'done', 'partial'}:
                return
        with self.records.begin() as tx:
            source = tx.read('v2_turns', turn_id)
            if source.revision != row.revision:
                raise RecognitionConflict('route_example_source_changed')
            self.stage(tx, turn_id, parts=[{key: part[key] for key in ('intent', 'span', 'depends_on', 'instruction')} for part in parts])
            tx.commit()

    def remove(self, turn_id, *, project_id, expected_revision, operation_id):
        from .route_corrections import OPERATIONS, validate_operation_id
        validate_operation_id(operation_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('route_example_revision_invalid')
        with self.records.begin() as tx:
            if tx.read(OPERATIONS, operation_id) is not None:
                raise RecognitionConflict('route_correction_operation_exists')
            row = tx.read(EXAMPLES, turn_id)
            if row is None or row.payload['project_id'] != project_id:
                raise ValueError('route_example_not_found')
            if row.revision != expected_revision:
                raise RecognitionConflict('route_example_revision_changed')
            if self.origin_for(row.payload['result_turn_id'], reader=TransactionRecords(tx)) != row.payload['origin']:
                raise RecognitionConflict('route_example_source_changed')
            tx.put(EXAMPLES, turn_id, {**row.payload, 'deleted': True}, expected_revision=row.revision)
            tx.put(OPERATIONS, operation_id, {'status': 'completed', 'action': 'delete_example',
                'source_turn_id': turn_id, 'project_id': project_id, 'at': _now()}, expected_revision=0)
            tx.commit()

    def validate(self, row):
        if self.source_allowed is not None and not self.source_allowed(row):
            raise RecognitionConflict('route_example_source_changed')
        current = self.records.read(EXAMPLES, row.object_id)
        source = self.records.read('v2_turns', row.payload['result_turn_id'])
        if (current is None or current.revision != row.revision or current.payload.get('deleted')
                or source is None or source.revision != row.payload['source_revision']
                or source.payload['project_id'] != row.payload['project_id']
                or source.payload['user_text'] != row.payload['text']
                or self.origin_for(source.object_id) != row.payload['origin']
                or self.source_allowed is None and is_private_project(self.records, row.payload['project_id'])):
            raise RecognitionConflict('route_example_source_changed')
        self.origin_for.validate_egress(source.object_id)

    def similar(self, project_id, text):
        self.origin_for.current(project_id)
        candidates = []
        for row in self.records.list(EXAMPLES):
            if row.payload.get('deleted') or not row.payload.get('parts'):
                continue
            # 失去当前来源资格的样例不参加选择；已冻结样例失效则由validate_frozen拒绝发送。
            try:
                self.validate(row)
            except RecognitionConflict:
                continue
            candidates.append(row)
        if not candidates:
            return []
        def validate():
            for row in candidates:
                self.validate(row)
        entries = [SimpleNamespace(object_id='route-example-' + row.object_id, revision=row.revision,
            payload={'project_id': row.payload['project_id'], 'content': row.payload['text']}) for row in candidates]
        anchor = SimpleNamespace(object_id='route-example-query', revision=1,
            payload={'project_id': project_id, 'content': text})
        policy = get('route_correction', version='@1')
        scores, threshold = InsightLinks(self.records, None, self.models)._rank(project_id, anchor, entries,
            source_refs=lambda row: [], validate_current=validate, limit=len(entries), keyword_threshold=policy.keyword_threshold)
        validate()
        values = {row.object_id: score for row, score in scores}
        ranked = policy.rank_examples([{'id': row.object_id, 'revision': row.revision,
            'same_project': row.payload['project_id'] == project_id, 'corrected': row.payload['corrected'],
            'score': values.get('route-example-' + row.object_id, 0)} for row in candidates
            if values.get('route-example-' + row.object_id, 0) >= threshold])
        by_id = {row.object_id: row for row in candidates}
        return [{'id': value['id'], 'revision': value['revision'], **deepcopy(by_id[value['id']].payload)} for value in ranked]

    def current(self, project_id):
        return self.origin_for.current(project_id)

    def validate_frozen(self, examples):
        for example in examples:
            row = self.records.read(EXAMPLES, example['id'])
            if row is None or {'id': row.object_id, 'revision': row.revision, **row.payload} != example:
                raise RecognitionConflict('route_example_source_changed')
            self.validate(row)
