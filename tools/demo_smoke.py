import argparse
from contextlib import closing
from datetime import datetime
import mimetypes
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


class SmokeFailure(RuntimeError):
    pass


class StopRun(SmokeFailure):
    pass


def require(condition, reason):
    if not condition:
        raise SmokeFailure(reason)


class AttemptMeter:
    # 只读已提交的物理尝试，包含尚未完成的尝试，避免遗漏后台调用。
    def __init__(self, root, limit):
        self.paths = [path for path in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3')
                      if path.is_file()]
        require(self.paths, 'meter_unavailable: 隔离根没有 ai-turns.sqlite3')
        self.limit = limit
        existing = self.read()
        self.baseline_pending = sum(row[0] != 'terminal' for row in existing.values())
        self.baseline = set(existing)
        self.count = 0
        self.pending = 0

    def read(self):
        found = {}
        for path in self.paths:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                for identity, status, provider, model in db.execute(
                    "SELECT r.attempt_id,r.status,json_extract(p.payload_json,'$.provider_id'),"
                    "json_extract(p.payload_json,'$.model_id') FROM ai_model_attempt_reservations r "
                    'JOIN ai_turn_payloads p ON p.payload_ref=r.dispatch_payload_ref'
                ):
                    record = (status, provider, model)
                    require(identity not in found or found[identity] == record, 'meter_conflicting_attempt')
                    found[identity] = record
        return found

    def check(self, *, submitting=False, final=False):
        rows = {identity: row for identity, row in self.read().items() if identity not in self.baseline}
        self.count = len(rows)
        self.pending = sum(row[0] != 'terminal' for row in rows.values())
        for _, provider, model in rows.values():
            if not isinstance(model, str) or 'deepseek' not in model.casefold():
                raise StopRun('model_not_deepseek: 停止提交，检查隔离后端模型配置')
            require(isinstance(provider, str) and provider, 'meter_provider_missing')
        if self.count > self.limit or submitting and self.count >= self.limit:
            raise StopRun(f'model_limit: dispatched={self.count} pending={self.pending} limit={self.limit}')
        if final and self.pending:
            raise StopRun(f'meter_incomplete: dispatched={self.count} pending={self.pending}')
        return self.count

    def project_observed(self, project):
        for path in self.paths:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                if db.execute("SELECT 1 FROM ai_turns WHERE json_extract(request_json,'$.scope.project_id')=? LIMIT 1",
                              (project,)).fetchone():
                    return True
        return False


class Smoke:
    def __init__(self, args):
        self.args = args
        self.deadline = time.monotonic() + args.timeout
        self.meter = None
        self.projects = {}
        self.documents = {}
        self.client = httpx.Client(base_url=args.base.rstrip('/'), trust_env=False,
                                   headers={'Accept': 'application/json'})

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise StopRun('run_timeout: 后台任务可能仍在执行，停止新提交')
        return left

    def settle_attempts(self):
        self.meter.check()
        while self.meter.pending:
            time.sleep(min(1, self.remaining()))
            self.meter.check()
        self.meter.check(final=True)

    def request(self, method, path, **kwargs):
        self.remaining()
        if self.meter:
            self.meter.check(submitting=method != 'GET')
        response = self.client.request(method, path, timeout=httpx.Timeout(self.remaining(), connect=5), **kwargs)
        # 错误响应只记录状态，避免把配置、原文或服务诊断写入终端。
        require(response.is_success, f'http_status={response.status_code} path={path}')
        value = response.json()
        require(isinstance(value, dict), f'invalid_json_object path={path}')
        if self.meter:
            self.meter.check()
        return value

    def preflight(self):
        base = urlsplit(self.args.base)
        require(base.scheme == 'http' and base.hostname in {'127.0.0.1', 'localhost', '::1'}, 'base_must_be_loopback_http')
        require(base.port == 8011, 'base_port_must_be_8011')
        root = self.args.app_root.resolve()
        require('work' in {part.casefold() for part in root.parts} and root.name.casefold() != 'runtime',
                'app_root_must_be_isolated_work_directory')
        require(root.is_dir(), 'app_root_missing')
        self.study = self.material('01-间隔重复入门.md')
        self.travel = self.material('03-成都周末行程.md')
        require('20 张' in self.study and '7:30' in self.travel, 'materials_expected_facts_missing')
        require(self.args.file.is_file(), 'upload_file_missing: 使用 --file 指定现有 PDF')
        require(self.args.file.suffix.casefold() == '.pdf', 'upload_requires_pdf')
        self.request('GET', '/api/v2/projects')
        # 此接口仅返回公开配置和是否已配置密钥，不读取密钥内容。
        public = self.request('GET', '/api/v2/settings')['model']
        generation = public['generation']
        require('deepseek' in str(generation.get('model', '')).casefold(), 'generation_must_be_deepseek')
        require(generation.get('allow_remote') is True and generation.get('has_api_key') is True,
                'deepseek_not_ready')
        fast = generation.get('fast_model', {})
        require(not isinstance(fast, dict) or not fast.get('model') or
                'deepseek' in str(fast['model']).casefold(), 'fast_model_must_be_deepseek')
        for purpose in ('embedding', 'rerank', 'vision', 'search'):
            config = public.get(purpose, {})
            remote = urlsplit(config.get('base_url') or '').hostname not in {'127.0.0.1', 'localhost', '::1'}
            if config.get('enabled') and config.get('allow_remote') and remote:
                require('deepseek' in str(config.get('model', '')).casefold(), f'remote_model_must_be_deepseek={purpose}')
        self.meter = AttemptMeter(root, self.args.max_calls)
        print(f'INFO baseline_attempts={len(self.meter.baseline)} baseline_pending={self.meter.baseline_pending}', flush=True)

    def material(self, name):
        path = self.args.materials / name
        require(path.is_file(), f'material_missing={name}')
        return path.read_text(encoding='utf-8-sig')

    def project(self, kind):
        if kind not in self.projects:
            stamp = datetime.now().strftime('%m%d-%H%M%S') + '-' + uuid4().hex[:8]
            self.projects[kind] = self.request('POST', '/api/v2/projects', json={'name': f'冒烟-{kind}-{stamp}'})['id']
            print(f'INFO project_{kind}={self.projects[kind]}', flush=True)
        return self.projects[kind]

    @staticmethod
    def complete(turn):
        intent, receipt = turn['intent'], turn['receipt']
        if intent == 'multi':
            parts = receipt.get('parts', [])
            require(parts, 'multi_parts_missing')
            for part in parts:
                require(part.get('state') not in {'failed', 'interrupted', 'partial', 'cancelled', 'waiting_approval'},
                        f'multi_part_state={part.get("state")}')
                if part.get('state') == 'done':
                    require(Smoke.complete({'intent': part['intent'], 'receipt': part['receipt']}),
                            'multi_child_incomplete')
            return all(part.get('state') == 'done' for part in parts)
        value = receipt.get(intent, {})
        state = value.get('state')
        require(state not in {'failed', 'interrupted', 'partial', 'cancelled', 'waiting_approval'}, f'turn_state={state}')
        require('interruption' not in value and 'partial' not in value, 'turn_interrupted')
        return state == 'done' if intent in {'remember', 'do'} else state in {None, 'done'}

    def turn(self, project, text, intent, *, item_id=None):
        body = {'project_id': project, 'text': text}
        if item_id:
            body['item_id'] = item_id
        # 与工作台一致，完全由后端判断意图。
        created = self.request('POST', '/api/v2/workbench/turns', json=body,
                               headers={'Idempotency-Key': 'demo-' + uuid4().hex})
        turn = created['turn']
        if turn.get('intent') != intent:
            raise StopRun(f'intent_expected={intent} actual={turn.get("intent")} turn={turn.get("id")}: 停止新提交并核对已接受任务')
        thread = created.get('thread_id') or turn.get('thread_id')
        require(thread and turn.get('id'), 'turn_identity_missing')
        print(f'INFO turn={turn["id"]} thread={thread} intent={intent}', flush=True)
        end = min(self.deadline, time.monotonic() + (300 if intent == 'do' else 180))
        while not self.complete(turn):
            if time.monotonic() >= end:
                raise StopRun(f'turn_timeout intent={intent} turn={turn["id"]}: 后台状态未完成')
            time.sleep(min(1, self.remaining()))
            rows = self.request('GET', f'/api/v2/workbench/threads/{quote(thread, safe="")}',
                                params={'project_id': project})['turns']
            matches = [row for row in rows if row.get('id') == turn['id']]
            require(len(matches) == 1, 'turn_missing_in_thread')
            turn = matches[0]
        if not self.meter.project_observed(project):
            raise StopRun('meter_backend_root_mismatch: 计量根没有本项目的真实内核记录')
        return turn

    def layer(self, project, layer):
        return self.request('GET', '/api/v2/library/' + layer, params={'project_id': project, 'q': ''})['items']

    def remembered(self, turn, project):
        receipt = turn['receipt']['remember']
        require(receipt.get('state') == 'done' and receipt.get('document_id'), 'remember_document_missing')
        self.documents.setdefault(project, set()).add(receipt['document_id'])

    def cited(self, turn, project, answer_pattern, evidence_token):
        receipt = turn['receipt']['ask']
        answer = receipt.get('answer')
        require(isinstance(answer, str) and re.search(answer_pattern, answer), 'answer_fact_missing')
        require(receipt.get('no_match') is False and receipt.get('citations'), 'answer_citations_missing')
        layers = {'source': 'sources', 'note': 'notes', 'summary': 'summaries', 'insight': 'insights'}
        catalog = {kind: {row.get('id') or row.get('document_id') for row in self.layer(project, layer)}
                   for kind, layer in layers.items()}
        supported = False
        for citation in receipt['citations']:
            kind, identity = citation.get('layer'), citation.get('id')
            require(kind in catalog and identity in catalog[kind], 'citation_outside_project')
            require(not citation.get('persona'), 'citation_outside_project_persona')
            drill = self.request('GET', '/api/v2/library/drill',
                                 params={'project_id': project, 'from': kind, 'id': identity})
            require(drill.get('source_project_id', project) == project, 'citation_foreign_origin')
            if kind == 'source':
                original = self.request('GET', f'/api/v2/library/sources/{quote(identity, safe="")}/text',
                                        params={'project_id': project})
                content = original.get('text')
            elif kind == 'note':
                content = (drill.get('note') or {}).get('markdown')
            else:
                content = (drill.get(kind) or {}).get('text')
            require(isinstance(content, str) and content.strip(), 'citation_content_missing')
            cited_quote = citation.get('quote')
            require(isinstance(cited_quote, str) and cited_quote.strip(), 'citation_quote_missing')
            require(all(piece in content for piece in cited_quote.split('\n…\n') if piece),
                    'citation_quote_unreadable')
            supported = supported or evidence_token in cited_quote
        require(supported, 'citation_expected_fact_missing')

    def step(self, number):
        project = self.project('学习')
        self.project('旅行')
        if number == 3:
            self.remembered(self.turn(project, self.study, 'remember'), project)
        elif number == 4:
            with self.args.file.open('rb') as stream:
                uploaded = self.request('POST', '/api/v2/workbench/files', data={'project_id': project},
                                        files={'file': (self.args.file.name, stream,
                                                       mimetypes.guess_type(self.args.file.name)[0] or 'application/pdf')})
            self.remembered(self.turn(project, '', 'remember', item_id=uploaded['id']), project)
        elif number == 5:
            for layer in ('sources', 'notes', 'summaries', 'insights'):
                require(self.layer(project, layer), f'empty_layer={layer}')
            pending = [row for row in self.layer(project, 'insights') if row.get('state') == 'pending']
            require(pending, 'pending_insight_missing')
            insight = pending[0]
            result = self.request('POST', f'/api/v2/library/insights/{quote(insight["id"], safe="")}/confirm',
                                  json={'project_id': project, 'expected_revision': insight['revision']})
            require(result.get('state') == 'active', 'insight_not_active')
        elif number == 6:
            self.cited(self.turn(project, '根据间隔重复入门笔记，每天新卡片最多多少张？', 'ask'),
                       project, r'(?:20|二十)\s*张', '20')
        elif number == 7:
            result = self.turn(project, '帮我根据间隔重复入门笔记写一份三条要点的学习周报', 'do')['receipt']['do']
            require(result.get('state') == 'done' and result.get('document_id'), 'outcome_document_missing')
            drill = self.request('GET', '/api/v2/library/documents/' + quote(result['document_id'], safe=''),
                                 params={'project_id': project})
            require(drill.get('markdown'), 'outcome_content_missing')
            require(re.search(r'(?:20|二十)\s*张', drill['markdown']) and '复习' in drill['markdown'],
                    'outcome_source_fact_missing')
        elif number == 8:
            turn = self.turn(project, '记住：学习复盘会议定于2031年11月15日。然后问：根据间隔重复入门笔记，每天新卡片最多多少张？', 'multi')
            receipt = turn['receipt']
            require(receipt.get('route', {}).get('mode') != 'rules', 'route_rules')
            parts = receipt['parts']
            require(sorted(part['intent'] for part in parts) == ['ask', 'remember'], 'multi_parts_mismatch')
            require(all(part.get('project_id') == project for part in parts), 'multi_project_changed')
            asked = next(part for part in parts if part['intent'] == 'ask')
            self.cited({'receipt': asked['receipt']}, project, r'(?:20|二十)\s*张', '20')
        elif number == 9:
            travel = self.project('旅行')
            self.remembered(self.turn(travel, self.travel, 'remember'), travel)
            self.cited(self.turn(travel, '根据成都周末行程，熊猫基地计划早上几点入园？', 'ask'),
                       travel, r'(?:7[:：]30|七点半)', '7:30')
            # 两个项目的原件身份必须保持分离。
            own = {row['id'] for row in self.layer(travel, 'sources')}
            foreign = {row['id'] for row in self.layer(project, 'sources')}
            require(own and not own.intersection(foreign), 'project_sources_overlap')


def main():
    parser = argparse.ArgumentParser(description='通过真实 HTTP 验证基本功能，所有写入使用已启动的隔离后端。')
    parser.add_argument('--base', default='http://127.0.0.1:8011')
    parser.add_argument('--materials', type=Path, default=Path('samples'))
    parser.add_argument('--file', type=Path, default=Path('output/pdf/第二大脑-项目介绍-2026.10.2.pdf'))
    parser.add_argument('--app-root', type=Path, default=Path('work/smoke-root'))
    parser.add_argument('--steps', nargs='+', type=int, choices=range(3, 10), default=list(range(3, 10)),
                        help='选择步骤；5、6、7、8自动先运行材料入库步骤3')
    parser.add_argument('--timeout', type=int, default=300, metavar='1..300')
    parser.add_argument('--max-calls', type=int, default=40, metavar='1..40')
    parser.add_argument('--check', action='store_true', help='只验证素材、连接、公开配置和计量，不写入项目或调用模型')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300 or not 1 <= args.max_calls <= 40:
        parser.error('--timeout 必须为1..300，--max-calls 必须为1..40')
    steps = set(args.steps)
    if steps.intersection({5, 6, 7, 8}):
        steps.add(3)
    smoke = Smoke(args)
    failures = 0
    try:
        ready = False
        try:
            smoke.preflight()
        except (SmokeFailure, httpx.HTTPError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
            reason = str(error) if isinstance(error, SmokeFailure) else type(error).__name__
            for number in ([0] if args.check else sorted(steps)):
                print(f'FAIL {number} preflight={reason}', flush=True)
                failures += 1
        else:
            ready = True
        if ready and args.check:
            print('PASS 0 素材、HTTP、DeepSeek公开配置与只读计量可用；模型调用0', flush=True)
        stopped = None
        for number in (sorted(steps) if ready and not args.check else []):
            try:
                if stopped:
                    raise StopRun(stopped)
                smoke.step(number)
                smoke.settle_attempts()
                print(f'PASS {number} dispatched={smoke.meter.count} pending={smoke.meter.pending}', flush=True)
            except (SmokeFailure, httpx.HTTPError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
                reason = str(error) if isinstance(error, SmokeFailure) else type(error).__name__
                failures += 1
                print(f'FAIL {number} {reason}', flush=True)
                if isinstance(error, (StopRun, httpx.TimeoutException, sqlite3.Error)):
                    stopped = reason
    finally:
        smoke.client.close()
        if smoke.meter:
            # 无论 HTTP、解析或等待如何结束，最终计量都重新读取真实数据库。
            try:
                smoke.meter.check(final=True)
            except (SmokeFailure, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
                reason = str(error) if isinstance(error, SmokeFailure) else type(error).__name__
                print(f'FAIL 0 final_meter={reason}; 最终计量未通过，不输出上次采样值', flush=True)
                failures += 1
            else:
                print(f'INFO dispatched={smoke.meter.count} pending={smoke.meter.pending} limit={args.max_calls}; '
                      '计数包含已提交但未证实发送的尝试；上限监测停止新提交，后台任务由原后端继续管理', flush=True)
    return failures


if __name__ == '__main__':
    raise SystemExit(main())
