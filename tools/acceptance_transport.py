import json
import os
from pathlib import Path
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit

from backend.memory_app.model_config import _load_litellm_completion
from backend.shared.llm.message_metadata import _extract_normalized_usage
from backend.shared.llm.model_transport import _OwnedModelStream
from tools.rag_holdout import CALL, HttpObserver, _capture_response_stream


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.next')
    with temporary.open('w', encoding='utf-8') as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


class AcceptanceStopped(RuntimeError):
    pass


class AcceptanceHttpObserver(HttpObserver):
    def __init__(self, meter, *, paid):
        super().__init__(meter.directory, 'https://api.deepseek.com',
            meter.manifest['limit'] - meter.manifest['carryover_reserved'], paid=paid)
        self.meter = meter

    def request(self, request):
        remote = request.url.host not in {'testserver', '127.0.0.1', 'localhost', '::1'}
        if remote and self.meter.halted:
            raise AcceptanceStopped('acceptance_stopped_before_send')
        super().request(request)
        if remote:
            # 原观察器在发送前计数，再同步持久化该计数，进程中断也保留预算归属。
            with self.lock, self.path.open('r+b') as persisted:
                os.fsync(persisted.fileno())


class StreamUsageCapture:
    def __init__(self, meter, row, stream):
        self.meter, self.row, self.facts = meter, row, stream.provider_facts
        self.finished = False

    def observe(self, chunk):
        with self.meter.lock:
            self.row['chunks'] = self.row.get('chunks', 0) + 1

    def finish(self, state, error=None):
        with self.meter.lock:
            if self.finished:
                return
            self.finished = True
            usage = _extract_normalized_usage({'usage': self.facts.usage})
            complete = state == 'eof' and self.facts.terminal
            self.meter.finish(self.row, usage, complete=complete, state=state, error=error)


class AcceptanceTransport:
    def __init__(self, data_root, prior_ledger, *, prior_native_unknown, limit=1200):
        self.directory = Path(data_root).parent / 'workbench-lexicon-acceptance-budget'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock, self.label, self.halted = RLock(), 'preflight', False
        prior = json.loads(Path(prior_ledger).read_text(encoding='utf-8'))
        if (limit != 1200 or not isinstance(prior, list) or type(prior_native_unknown) is not int
                or prior_native_unknown < 0 or len(prior) + prior_native_unknown > limit):
            raise ValueError('authorized_wire_baseline_invalid')
        known = [row for row in prior if row.get('status') == 'succeeded' and all(
            type(row.get(name)) is int and row[name] >= 0 for name in ('input_tokens', 'output_tokens'))]
        manifest = {'schema_version': 1, 'limit': limit,
            'carryover_reserved': len(prior) + prior_native_unknown,
            'carryover_known_physical': len(known), 'carryover_unknown_physical': len(prior) - len(known),
            'carryover_input_tokens': sum(row['input_tokens'] for row in known),
            'carryover_output_tokens': sum(row['output_tokens'] for row in known),
            'carryover_native_unknown': prior_native_unknown,
            'prior_physical_summary': [{name: row.get(name) for name in
                ('number', 'status', 'input_tokens', 'output_tokens')} for row in prior]}
        path = self.directory / 'manifest.json'
        if path.exists():
            existing = json.loads(path.read_text(encoding='utf-8'))
            initial_fields = {'schema_version', 'limit', 'carryover_reserved', 'carryover_known_physical',
                'carryover_input_tokens', 'carryover_output_tokens', 'carryover_native_unknown'}
            if existing != manifest and (set(existing) != initial_fields
                    or any(existing[name] != manifest[name] for name in existing)):
                raise ValueError('acceptance_budget_manifest_conflict')
        save(path, manifest)
        self.manifest = manifest
        self.path = self.directory / 'completions.json'
        self.rows = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []
        self.http = None

    def counts(self):
        physical = len(self.http.rows) if self.http is not None else 0
        known = [row for row in self.rows if row.get('usage_status') == 'known']
        reserved = self.manifest['carryover_reserved']
        return {'limit': self.manifest['limit'], 'carryover_reserved': reserved,
            'carryover_known_physical': self.manifest['carryover_known_physical'],
            'carryover_native_unknown': self.manifest['carryover_native_unknown'],
            'carryover_unknown_physical': self.manifest['carryover_unknown_physical'],
            'new_physical': physical, 'budget_consumed': reserved + physical,
            'remaining': self.manifest['limit'] - reserved - physical,
            'known_input_tokens': self.manifest['carryover_input_tokens'] + sum(row['usage']['input_tokens'] for row in known),
            'known_output_tokens': self.manifest['carryover_output_tokens'] + sum(row['usage']['output_tokens'] for row in known),
            'new_unknown_completions': sum(row.get('usage_status') != 'known' for row in self.rows)}

    def observe_http(self, *, paid):
        self.http = AcceptanceHttpObserver(self, paid=paid)
        if not self.http.path.exists():
            save(self.http.path, self.http.rows)
        with self.http.path.open('r+b') as persisted:
            os.fsync(persisted.fileno())
        return self.http

    def finish(self, row, usage, *, complete, state, error=None):
        with self.lock:
            requests = [value for value in self.http.rows
                if value.get('completion', {}).get('number') == row['number']]
            known = complete and len(requests) == 1 and all(
                type(usage.get(name)) is int and usage[name] >= 0 for name in ('input_tokens', 'output_tokens'))
            row.update(state=state, usage_status='known' if known else 'unavailable',
                usage=usage if known else None, observed_usage=usage,
                http_numbers=[value['number'] for value in requests],
                elapsed_seconds=round(monotonic() - row.pop('_started'), 3))
            if error is not None:
                row['error_type'] = type(error).__name__
            if not known:
                self.halted = True
            save(self.path, self.rows)

    def complete(self, **request):
        if (self.halted or 'deepseek' not in str(request.get('model', '')).lower()
                or urlsplit(str(request.get('api_base') or '')).hostname != 'api.deepseek.com'):
            raise AcceptanceStopped('acceptance_invalid_or_stopped_transport')
        with self.lock:
            row = {'number': len(self.rows) + 1, 'label': self.label, 'state': 'started',
                'stream': request.get('stream') is True, 'usage_status': 'unavailable',
                'usage': None, '_started': monotonic()}
            self.rows.append(row)
            save(self.path, self.rows)
        token = CALL.set({'number': row['number'], 'label': self.label})
        try:
            result = _load_litellm_completion(**{**request, 'num_retries': 0, 'max_retries': 0})
            if request.get('stream'):
                if not isinstance(result, _OwnedModelStream) or result.provider_facts is None:
                    result.close()
                    raise AcceptanceStopped('production_provider_stream_observer_required')
                # 原包装逐个转交分片，同时保留生产关闭方式和流式类型判断。
                return _capture_response_stream(result, StreamUsageCapture(self, row, result), dict(CALL.get()))
            self.finish(row, _extract_normalized_usage(result), complete=True, state='returned')
            if self.halted:
                raise AcceptanceStopped('acceptance_usage_unavailable')
            return result
        except BaseException as error:
            if '_started' in row:
                self.finish(row, {}, complete=False, state='failed', error=error)
            raise
        finally:
            CALL.reset(token)
