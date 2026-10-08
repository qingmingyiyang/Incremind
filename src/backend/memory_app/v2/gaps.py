"""从原 ASK 完成事实推导未解答的问题。"""
from datetime import datetime, timezone
import re

from fastapi import APIRouter, HTTPException, Request, Response
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..workspace_contracts import _json, _project
from ..kernel.receipt_projection import kernel_call_groups
from .intent import parse_scope_tag
from .policies import get, version
from .signals import SignalService, _fact_time

GAPS = 'v2_gaps'
DISMISSALS = 'v2_gap_dismissals'

_LAYERS = {'L3': 'insight', 'L2': 'summary', 'L1': 'note', 'L0': 'source'}
_HISTORY = '\n\n对话历史（仅帮助理解，不是证据）：\n'
_QUESTION = '\n\n问题：'


def _frozen_coverage(group, answer, question, project, policy):
    """从原冻结模型输入中恢复边界唯一且映射完整的最终证据。"""
    from core.search_and_recall.evidence_windows import query_terms
    from .budget import _source_texts

    try:
        wire = group['answer_input']
        messages = wire['messages']
        if (wire.get('purpose') != 'primary' or not isinstance(messages, list)
                or len(messages) not in (2, 3) or messages[-1].get('role') != 'user'
                or any(message.get('role') != 'system' for message in messages[:-1])):
            return None
        content = messages[-1]['content']
        suffix = _QUESTION + question
        if (not isinstance(content, str) or not content.startswith('资料：\n')
                or not content.endswith(suffix) or content.count(_QUESTION) != 1
                or len(re.findall(r'^资料：$', content, re.MULTILINE)) != 1
                or content.count(_HISTORY) > 1):
            return None
        sources = content[len('资料：\n'):-len(suffix)]
        trace = answer['trace'][0]
        history = [part for part in answer['context']['parts'] if part.get('key') == 'history']
        if (len(history) != 1 or type(history[0].get('count')) is not int
                or history[0]['count'] < 0 or not isinstance(trace.get('history_turn_ids'), list)
                or history[0]['count'] != len(trace['history_turn_ids'])
                or bool(history[0]['count']) != (_HISTORY in sources)):
            return None
        sources = sources.split(_HISTORY)[0]
        chosen = group['answer']['chosen']
        entries = answer['context']['entries']
        if (not isinstance(chosen, list) or not chosen or not isinstance(entries, list)
                or len(entries) < len(chosen)
                or any(entry.get('persona') is not True for entry in entries[:-len(chosen)])):
            return None
        numbered = entries[-len(chosen):]
        framed = []
        for candidate, entry in zip(chosen, numbered):
            layer, own = _LAYERS[candidate['layer']], candidate['entry']
            identity = (own['id'] if layer == 'insight' else own['document_id']
                if layer in {'summary', 'note'} else own.get('item_id') or own['source_id'])
            persona = candidate['persona']
            title = entry['title']
            if (not isinstance(identity, str) or not identity or type(persona) is not bool
                    or (entry.get('layer'), entry.get('id'), entry.get('persona')) != (layer, identity, persona)
                    or candidate['project_id'] != ('me' if persona else project)
                    or not isinstance(title, str) or not title or '\n' in title or '\r' in title):
                return None
            framed.append({**candidate, 'id': identity, 'title': title, 'excerpt': '',
                'supplemented': entry.get('supplemented', False)})
        compose_version = group['request']['policy_versions']['compose']
        headers = get('compose', version=compose_version)(_source_texts, framed, operation='sources')
        markers = list(re.finditer(r'^\[(\d+)\][^\n]*', sources, re.MULTILINE))
        if (len(markers) != len(chosen) or [int(marker[1]) for marker in markers] != list(range(1, len(chosen) + 1))
                or len(headers) != len(chosen) or any(sources.count(header) != 1 for header in headers)):
            return None
        positions = [sources.index(header) for header in headers]
        if positions[0] != 0 or positions != sorted(positions):
            return None
        excerpts = []
        for index, header in enumerate(headers):
            end = positions[index + 1] - 2 if index + 1 < len(headers) else len(sources)
            if index + 1 < len(headers) and sources[end:positions[index + 1]] != '\n\n':
                return None
            excerpt = sources[positions[index] + len(header):end]
            if not excerpt.strip():
                return None
            excerpts.append(excerpt)
        if '\n\n'.join(header + excerpt for header, excerpt in zip(headers, excerpts)) != sources:
            return None
        seen = set()
        for citation in answer['citations']:
            number = citation['n']
            if type(number) is not int or not 1 <= number <= len(chosen) or number in seen:
                return None
            seen.add(number)
            entry = numbered[number - 1]
            if (any(citation.get(key) != entry[key] for key in ('layer', 'id', 'title', 'persona'))
                    or citation.get('quote') != excerpts[number - 1]):
                return None
        condensed = trace['condensed_question']
        if condensed is not None and (not isinstance(condensed, str) or not condensed.strip()
                or trace.get('condense_status') != 'completed'):
            return None
        wordings = [condensed or question]
        rewrite = trace['rewrite']
        if (type(rewrite.get('used')) is not bool or not isinstance(rewrite.get('queries'), list)
                or len(rewrite['queries']) > 3 or any(not isinstance(value, str) or not value.strip()
                    for value in rewrite['queries']) or not rewrite['used'] and rewrite['queries']):
            return None
        if rewrite['used']:
            if trace.get('rewrite_status') != 'completed':
                return None
            wordings.extend(rewrite['queries'])
        evidence = '\n'.join(excerpt for excerpt, candidate in zip(excerpts, chosen) if not candidate['persona'])
        return policy.coverage(wordings, evidence, query_terms)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return None


class Gaps:
    def __init__(self, records, *, runtime_root, turn_store,
                 now=lambda: datetime.now(timezone.utc), query=None):
        self.records, self.runtime_root, self.turn_store, self.now = records, runtime_root, turn_store, now
        self.query = query

    def _facts(self, project, clock, cutoff):
        store = self.turn_store()
        groups = {group['turn_id']: group for group in kernel_call_groups(
            self.runtime_root, project=project, remote_only=False, records=self.records,
            include_answer_input=True, include_no_wire_answers=True)}
        requests = {}
        for row in self.records.list('v2_turn_requests'):
            value = row.payload
            result = value.get('result')
            turn = result.get('turn') if isinstance(result, dict) else None
            if not isinstance(turn, dict):
                continue
            if value.get('state') == 'completed' and isinstance(turn.get('id'), str):
                requests.setdefault(turn['id'], []).append(row)
        facts, guards = {}, []
        for row in self.records.list('v2_turns'):
            value = row.payload
            at = _fact_time(value.get('created_at'))
            if (value.get('project_id') != project or value.get('intent') != 'ask'
                    or value.get('by') == 'admin' or at is None or at > clock
                    or cutoff is not None and at <= cutoff):
                continue
            group = groups.get(row.object_id, {})
            if not group.get('terminal'):
                continue
            request = store.get_request(row.object_id) if store else group.get('request')
            completed = ([event for event in store.events_after(row.object_id)
                if event.get('type') in {'turn.completed', 'turn.failed', 'turn.cancelled'}] if store
                else [group['terminal']] if group.get('terminal') else [])
            result = (store.get_immutable_payload(row.object_id, 'product-answer-result-v2') if store
                else (None, group['answer']) if group.get('answer') else None)
            if (not isinstance(request, dict) or not isinstance(request.get('scope'), dict)
                    or not isinstance(request.get('input'), dict) or request.get('turn_id') != row.object_id
                    or request.get('scope', {}).get('project_id') != project
                    or request.get('desired_outcome') != 'project.answer'
                    or request.get('operation_id') != 'answer-' + row.object_id
                    or not completed or completed[-1]['type'] != 'turn.completed' or result is None):
                continue
            if (group.get('request') != request or group.get('answer') != result[1]
                    or group.get('terminal') != completed[-1]):
                continue
            question = request.get('input', {}).get('text')
            receipt = result[1].get('receipt') if isinstance(result[1], dict) else None
            answer = receipt.get('ask') if isinstance(receipt, dict) else None
            if (not isinstance(question, str) or not question.strip() or question != value.get('user_text')
                    or not isinstance(answer, dict) or not isinstance(answer.get('answer'), str)):
                continue
            if answer.get('no_match') is not True:
                group = groups.get(row.object_id)
                if (group is None or group.get('request') != request or group.get('answer') != result[1]
                        or not group.get('answer_input')
                        or not any(call.get('status') == 'completed' and call.get('model_call_purpose') == 'primary'
                            and call.get('turn_id') == row.object_id for call in group['calls'])):
                    continue
            scene = None
            bound = requests.get(row.object_id, [])
            if len(bound) == 1:
                body = bound[0].payload.get('body', {})
                raw = body.get('text') if isinstance(body, dict) else None
                saved = bound[0].payload['result']
                if isinstance(raw, str) and saved['turn'].get('thread_id') == value.get('thread_id'):
                    _, captured_scene, cleaned = parse_scope_tag(raw)
                    if cleaned == question and saved['turn'].get('user_text') == question:
                        scene = captured_scene
                        guards.append(('v2_turn_requests', bound[0]))
            policy = get('gap')
            coverage = _frozen_coverage(group, answer, question, project, policy) if answer.get('citations') else None
            facts[row.object_id] = {'id': row.object_id, 'text': question, 'scene': scene,
                'at': at.isoformat(), 'insufficient': policy.insufficient(answer, coverage=coverage),
                'anchor_guard': {'revision': row.revision, 'created_at': value['created_at'],
                    'by': value.get('by', 'user')}}
            guards.append(('v2_turns', row))
        return facts, guards

    def current(self, project):
        project = _project(project)
        clock = self.now().astimezone(timezone.utc)
        initial, settings_revision = SignalService._settings(self.records)
        if not initial['enabled']:
            return {'items': []}
        facts, guards = self._facts(project, clock, _fact_time(initial['cleared_at']))
        policy = get('gap')
        with self.records.begin() as tx:
            if SignalService._settings(tx) != (initial, settings_revision):
                raise SQLiteUnitOfWorkConflict('gap_settings_changed')
            for collection, row in guards:
                if tx.read(collection, row.object_id) != row:
                    raise SQLiteUnitOfWorkConflict('gap_history_changed')
            previous = tx.read(GAPS, project)
            groups = previous.payload['items'] if previous else []
            # 隐藏和已丢弃的分组仍保留稳定身份；文字按需读取原 Turn，不复制到决定事实。
            groups = [dict(item, turn_ids=list(item['turn_ids'])) for item in groups]
            anchors = {}
            for group in groups:
                anchor = facts.get(group['anchor'])
                if anchor:
                    anchors[group['id']] = anchor['text']
                    group['anchor_guard'] = anchor['anchor_guard']
                else:
                    # 内核证明暂不可用时保留已有结论，但必须仍是之前核验过的同一公开对象。
                    row = tx.read('v2_turns', group['anchor'])
                    cutoff = _fact_time(initial['cleared_at'])
                    at = _fact_time(row.payload.get('created_at')) if row else None
                    if (row is not None and row.payload.get('project_id') == project
                            and row.payload.get('intent') == 'ask' and row.payload.get('by') != 'admin'
                            and at is not None and at <= clock and (cutoff is None or at > cutoff)
                            and group.get('anchor_guard') == {'revision': row.revision,
                                'created_at': row.payload['created_at'], 'by': row.payload.get('by', 'user')}
                            and isinstance(row.payload.get('user_text'), str)):
                        anchors[group['id']] = row.payload['user_text']
            for fact in sorted(facts.values(), key=lambda item: (item['at'], item['id'])):
                if fact['insufficient'] is None:
                    continue
                matching = next((group for group in groups if group['scene'] == fact['scene']
                    and group['id'] in anchors and policy.related(anchors[group['id']], fact['text'])), None)
                if matching is None and fact['insufficient']:
                    matching = {'id': 'gap-' + fact['id'], 'anchor': fact['id'], 'scene': fact['scene'],
                        'turn_ids': [], 'last_at': fact['at'], 'unresolved': True,
                        'anchor_guard': fact['anchor_guard'], 'state_at': fact['at'], 'state_turn_id': fact['id']}
                    groups.append(matching)
                    anchors[matching['id']] = fact['text']
                if matching is not None:
                    if (fact['at'], fact['id']) >= (matching.get('state_at', matching['last_at']),
                                                   matching.get('state_turn_id', matching['anchor'])):
                        matching.update(unresolved=fact['insufficient'], state_at=fact['at'], state_turn_id=fact['id'])
                    if fact['insufficient']:
                        if fact['id'] not in matching['turn_ids']:
                            matching['turn_ids'].append(fact['id'])
                        matching['last_at'] = max(matching['last_at'], fact['at'])
            # 清除前的锚点始终排除，即使旧投影仍然存在。
            eligible = [group for group in groups if group['id'] in anchors]
            dismissed = {row.payload['gap_id'] for row in tx.list(DISMISSALS)
                if row.payload.get('project_id') == project}
            selected = policy(eligible, now=clock, dismissed=dismissed)
            payload = {'items': groups, 'policy': version('gap')}
            if previous is None or previous.payload != payload:
                tx.put(GAPS, project, payload, expected_revision=previous.revision if previous else 0)
            tx.commit()
        return {'items': [{'id': item['id'], 'scene': item['scene'], 'text': anchors[item['id']],
            'count': len(item['turn_ids']), 'last_at': item['last_at']} for item in selected]}

    def refresh(self):
        """每日复查的结论以原资料完成验证的时刻为准。"""
        if self.query is None or not SignalService._settings(self.records)[0]['enabled']:
            return
        from backend.recognition import RecognitionError
        local = None
        projects = sorted({row.payload['project_id'] for row in self.records.list('v2_turns')
            if row.payload.get('intent') == 'ask' and isinstance(row.payload.get('project_id'), str)})
        for project in projects:
            for item in self.current(project)['items']:
                settings = SignalService._settings(self.records)
                if not settings[0]['enabled']:
                    return
                prior = self.records.read(GAPS, project)
                if prior is None:
                    continue
                if local is None:
                    local = self.query.local_reader()
                result = local.local_coverage(project, item['text'], scene=item['scene'])
                if not get('gap').covered(result) or not callable(result.get('validate_current')):
                    continue
                try:
                    # 原资料验证使用独立的短事务，先完成验证再写投影，避免嵌套写锁。
                    result['validate_current']()
                except RecognitionError:
                    continue
                at = self.now().astimezone(timezone.utc).isoformat()
                with self.records.begin() as tx:
                    if SignalService._settings(tx) != settings or tx.read(GAPS, project) != prior:
                        raise SQLiteUnitOfWorkConflict('gap_recheck_changed')
                    groups = [dict(group) for group in prior.payload['items']]
                    group = next((group for group in groups if group['id'] == item['id']), None)
                    anchor = tx.read('v2_turns', group['anchor']) if group else None
                    if (anchor is None or group.get('anchor_guard') != {
                            'revision': anchor.revision, 'created_at': anchor.payload.get('created_at'),
                            'by': anchor.payload.get('by', 'user')}):
                        raise SQLiteUnitOfWorkConflict('gap_history_changed')
                    group.update(unresolved=False, state_at=at)
                    tx.put(GAPS, project, {**prior.payload, 'items': groups}, expected_revision=prior.revision)
                    tx.commit()

    def dismiss(self, project, identity, *, by='user'):
        project = _project(project)
        if by not in {'user', 'admin'}:
            raise HTTPException(400, 'invalid_gap_actor')
        initial = SignalService._settings(self.records)
        self.current(project)
        with self.records.begin() as tx:
            settings, _ = SignalService._settings(tx)
            if not settings['enabled'] or SignalService._settings(tx) != initial:
                raise HTTPException(409, 'gap_settings_changed')
            row = tx.read(GAPS, project)
            item = next((item for item in row.payload['items'] if item['id'] == identity), None) if row else None
            if item is None:
                raise HTTPException(404, 'gap_not_found')
            anchor = tx.read('v2_turns', item['anchor'])
            cutoff = _fact_time(settings['cleared_at'])
            if cutoff is not None and (anchor is None or (_fact_time(anchor.payload.get('created_at')) or cutoff) <= cutoff):
                raise HTTPException(409, 'gap_settings_changed')
            old = tx.read(DISMISSALS, identity)
            if old is not None and old.payload.get('project_id') != project:
                raise HTTPException(404, 'gap_not_found')
            if old is None:
                tx.put(DISMISSALS, identity, {'project_id': project, 'gap_id': identity,
                    'turn_ids': item['turn_ids'], 'at': self.now().isoformat(), 'by': by}, expected_revision=0)
            tx.commit()


def install_gap_routes(application, *, records, runtime_root, owner=None, query=None):
    owner = owner or Gaps(records, runtime_root=runtime_root,
        turn_store=lambda: getattr(application.state, 'ai_turn_store', None), query=query)
    application.state.gaps = owner
    router = APIRouter(prefix='/api/v2/library/gaps')

    @router.get('')
    def current(project_id: str):
        try:
            return owner.current(project_id)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'gaps_changed') from None

    @router.post('/{identity}/dismiss', status_code=204)
    async def dismiss(identity: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id'}:
            raise HTTPException(400, 'invalid_gap_fields')
        try:
            owner.dismiss(body['project_id'], identity)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'gaps_changed') from None
        return Response(status_code=204)

    application.include_router(router)
    return owner
