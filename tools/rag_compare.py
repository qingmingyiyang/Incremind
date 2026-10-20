"""同一公开语料的完整原文直接问答与项目实际检索链路对照。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import rag_pilot as pilot
from backend.memory_app.structured_generation import AskOutput
from backend.memory_app.v2.budget import _INSTRUCTION
from backend.shared.llm.message_metadata import _estimate_input_tokens

MODES = ('direct', 'project')


class CompareGateway(pilot.SingleWireGateway):
    def __init__(self, **kwargs):
        # 仅评测适配已核验模型窗口，两组相同；项目原选材预算仍由生产策略控制。
        self._selection_window = kwargs.get('context_window_tokens', 1_000_000)
        kwargs['context_window_tokens'] = 1_048_576
        super().__init__(**kwargs)

    def input_budget_limits(self, *, max_tokens=None):
        # 选材读取原工厂预算，实际 wire 校验独立使用已核验的大窗口，避免扩大项目证据预算。
        return {**super().input_budget_limits(max_tokens=max_tokens), 'window': self._selection_window}


class CompareBudget(pilot.Budget):
    def complete(self, **request):
        # 公开试料的真实提示用于审计；只取消息，不保存认证头或请求配置。
        if self.label.startswith(('direct:', 'project:')):
            messages = request.get('messages', [])
            pilot.save(self.output / f"prompt-{self.label.replace(':', '-')}-wire{len(self.rows) + 1}.json", {
                'messages': messages,
                'input_chars': sum(len(str(message.get('content', ''))) for message in messages),
            })
        return super().complete(**request)


def corpus_sources(sessions):
    return [{'number': index, 'type': 'source', 'id': session['id'],
             'title': session['title'], 'excerpt': session['content']}
            for index, session in enumerate(sessions, 1)]


def direct_messages(question, sources):
    return [{'role': 'system', 'content': _INSTRUCTION},
            {'role': 'user', 'content': 'Question: ' + question + '\n' + pilot.INSTRUCTION + '\n\nSources:\n' + '\n\n'.join(
                f"[{source['number']}] {source['title']}\n{source['excerpt']}" for source in sources)}]


def direct_question(models, item, sources, output, budget):
    path = output / f"direct-{item['qa_index']}.json"
    if path.exists():
        return pilot.read(path)
    budget.label = f"direct:{item['qa_index']}:answer"
    messages = direct_messages(item['question'], sources)
    started = time.perf_counter()
    answer, meta = models.complete_structured(messages, response_model=AskOutput,
                                             max_tokens=768, timeout_seconds=90)
    if any(number not in {source['number'] for source in sources} for number in answer.citations):
        raise ValueError('compare_direct_unknown_citation')
    result = {'answer': answer.answer, 'sources': [source for source in sources if source['number'] in answer.citations],
              'model_used': True, 'usage': meta['usage'], 'context_budget': meta.get('context_budget')}
    row = {'mode': 'direct', 'qa_index': item['qa_index'], 'question': item['question'],
           'category': item['category'], 'result': result, 'total_seconds': time.perf_counter() - started,
           'input_chars': sum(len(message['content']) for message in messages),
           'source_count': len(sources), 'corpus_chars': sum(len(source['excerpt']) for source in sources),
           'evidence_recall': None, 'recall_status': 'not_applicable_full_corpus_input',
           'answer_f1': pilot.answer_f1(answer.answer, item['answer'])}
    pilot.save(path, row)
    return row


def project_question(query, item, output, budget):
    # 两组独立标识，项目组仍须通过同一向量与完整重排验证。
    original = pilot.run_question(query, item, 'project', output, budget)
    row = {**original, 'mode': 'project', 'recall_status': 'diagnostic_only_not_comparable_to_full_corpus'}
    prefix = 'attempt-' if row.get('evaluation_status') == 'rerank_failed_fallback' else ''
    pilot.save(output / f"{prefix}project-{item['qa_index']}.json", row)
    return row


def summarize(output, manifest, budget):
    summary = {'contract': 'full-corpus-direct-vs-project@1', 'questions_planned': len(manifest['questions']),
               'external_calls': len(budget.rows), 'modes': {},
               'recall_comparison': 'not_applicable: direct receives the entire corpus; project retrieval recall is diagnostic only',
               'validation_boundary': 'Same-model independent calls can share errors; atom decomposition and semantic judgments require human audit.',
               'token_usage': {key: sum(row.get('usage', {}).get(key, 0) or 0 for row in budget.rows)
                               for key in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                                           'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')},
               'per_question': []}
    for mode in MODES:
        rows = [pilot.read(output / f"{mode}-{item['qa_index']}.json")
                or pilot.read(output / f"attempt-{mode}-{item['qa_index']}.json") for item in manifest['questions']]
        rows = [row for row in rows if row]
        grades = [pilot.read(output / f"judge-{row['qa_index']}.json") for row in rows]
        wires = [row for row in budget.rows if row['label'].startswith(mode + ':')]
        summary['modes'][mode] = {'answers': len(rows), 'judged': sum(bool(grade) for grade in grades),
            'raw_semantic_correct': sum(grade['grades'][mode]['correct'] for grade in grades if grade),
            'citation_supported': sum(grade['grades'][mode]['citation_supported'] for grade in grades if grade),
            'rerank_fallback': sum(row.get('evaluation_status') == 'rerank_failed_fallback' for row in rows),
            'mean_answer_f1': statistics.mean(row['answer_f1'] for row in rows) if rows else None,
            'median_seconds': statistics.median(row['total_seconds'] for row in rows) if rows else None,
            'first_question_seconds': rows[0]['total_seconds'] if rows else None,
            'remaining_median_seconds': statistics.median(row['total_seconds'] for row in rows[1:]) if len(rows) > 1 else None,
            'judge_validation': pilot.validation_summary(output, rows, mode),
            'answer_and_retrieval_token_usage': {key: sum(row.get('usage', {}).get(key, 0) or 0 for row in wires)
                for key in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                            'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')}}
        if mode == 'project' and any('evidence_annotation' in row for row in rows):
            summary['modes'][mode].update(pilot.annotation_summary(rows))
            eligible = [row for row in rows if row['evidence_recall'] is not None]
            summary['modes'][mode]['diagnostic_evidence_questions'] = len(eligible)
            summary['modes'][mode]['diagnostic_mean_evidence_recall'] = statistics.mean(
                row['evidence_recall'] for row in eligible) if eligible else None
    for item in manifest['questions']:
        identity = item['qa_index']
        grade = pilot.read(output / f'judge-{identity}.json')
        check = pilot.read(output / f'judge-validation-{identity}.json')
        summary['per_question'].append({'qa_index': identity, 'question': item['question'],
            'grades': grade.get('grades') if grade else None,
            'validation': check.get('decisions') if check else None,
            'validation_status': check.get('status') if check else 'not_available'})
    summary['model_load'] = pilot.read(output / 'load.json')
    summary['index'] = pilot.read(output / 'index-project.json')
    pilot.save(output / 'summary.json', summary)
    return summary


def prepare_corpus(output, baseline, formal, *, copy_baseline=True):
    if formal is not None and (output == formal or formal in output.parents or output in formal.parents):
        raise ValueError('compare_output_overlaps_formal_root')
    if baseline is not None and (output == baseline or baseline in output.parents or output in baseline.parents):
        raise ValueError('compare_output_overlaps_baseline')
    output.mkdir(parents=True, exist_ok=True)
    if not copy_baseline:
        return
    if baseline is None:
        raise ValueError('compare_baseline_or_raw_data_required')
    for name in ('locomo10.json', 'manifest.json', 'LICENSE.locomo.txt', 'README.locomo.md'):
        source, target = baseline / name, output / name
        if not source.is_file():
            raise ValueError('compare_baseline_file_missing:' + name)
        if target.exists() and source.read_bytes() != target.read_bytes():
            raise ValueError('compare_baseline_file_changed:' + name)
        if not target.exists():
            shutil.copyfile(source, target)


def run(args):
    output = args.output.resolve()
    prepare_corpus(output, args.baseline.resolve() if args.baseline else None,
        args.config_root.resolve() if args.config_root else None, copy_baseline=args.raw_data is None)
    manifest, sessions = pilot.prepare_data(output, args.seed, raw_data=args.raw_data,
        conversation_index=args.conversation_index, manifest_path=args.manifest)
    if args.baseline and pilot.read(args.baseline.resolve() / 'manifest.json') != manifest:
        raise ValueError('compare_baseline_manifest_changed')
    questions = pilot.prepared_questions(output, manifest, sessions, args.raw_data or output / 'locomo10.json',
        explicit=args.raw_data is not None or args.manifest is not None or args.conversation_index is not None)
    if args.prepare_only:
        return pilot.prepared_only(output, manifest, sessions)
    sources = corpus_sources(sessions)
    estimates = [_estimate_input_tokens(direct_messages(item['question'], sources)) for item in manifest['questions']]
    if max(estimates) + 768 >= 1_048_576:
        raise ValueError('compare_full_corpus_exceeds_verified_context')
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_LOG='ERROR')
    generation = pilot.read_generation(args.config_root.resolve(), args.config_db)
    if 'deepseek' not in generation['base_url'] or not generation['enabled'] or not generation['allow_remote']:
        raise ValueError('compare_requires_authorized_deepseek')
    if (generation['model'] != 'deepseek-flash'
            or urlsplit(generation['base_url']).hostname != 'api.deepseek.com'
            or urlsplit(generation['base_url']).scheme != 'https'):
        raise ValueError('compare_context_window_requires_verified_deepseek_flash')
    budget = CompareBudget(output, args.max_calls)
    selections = pilot.parse_overrides(args.policy)
    with pilot.override(**selections):
        configuration = {'contract': 'full-corpus-direct-vs-project@1', 'modes': MODES,
            'sourcecode_commit': subprocess.check_output(['git', '-C', str(pilot.ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
            'sourcecode_dirty': bool(subprocess.check_output(['git', '-C', str(pilot.ROOT), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()),
            'policies': pilot.versions_for_turn('project.answer'), 'policy_overrides': selections,
            'generation': {key: generation[key] for key in ('provider', 'model', 'base_url')},
            'seed': args.seed, 'temperature': 0, 'max_answer_tokens': 768, 'history': 'empty',
            'comparison_gateway_window_tokens': 1_048_576,
            'project_selection_budget': 'unchanged original factory input_budget_limits (16000 window); wire validation uses 1M',
            'context_window_source': 'https://api-docs.deepseek.com/quick_start/pricing/',
            'context_window_verified_date': '2026-10-10',
            'common_instruction': pilot.INSTRUCTION, 'direct_system': _INSTRUCTION,
            'project_system': 'actual compose instruction and source-date annotations; actual messages saved per wire',
            'direct_template_chars': sum(len(message['content']) for message in direct_messages('', sources)),
            'session_count': len(sources), 'corpus_chars': sum(len(source['excerpt']) for source in sources),
            'direct_estimated_input_tokens_range': [min(estimates), max(estimates)],
            'rerank_schema': pilot.RerankScores.model_json_schema(), 'rerank_instruction': pilot.RERANK_INSTRUCTION,
            'judge_contract': pilot.JUDGE_VALIDATION_CONTRACT,
            'judge_instruction': pilot.judge_instruction(2),
            'judge_max_tokens': 1200, 'judge_timeout_seconds': 60,
            'validation_max_tokens': {'atoms': 1800, 'checks': 5000, 'audit': 6500}, 'validation_timeout_seconds': 60,
            'answer_timeout_seconds': 90, 'rerank_timeout_seconds': 30,
            'answer_schema': AskOutput.model_json_schema(),
            'decompose_instruction': pilot.schema_instruction(pilot.DECOMPOSE_INSTRUCTION, pilot.ReferenceAtoms),
            'check_instruction': pilot.schema_instruction(pilot.CHECK_INSTRUCTION, pilot.AnswerChecks),
            'audit_instruction': pilot.schema_instruction(pilot.AUDIT_INSTRUCTION, pilot.AnswerAudits),
            'audit_schema': pilot.AnswerAudits.model_json_schema(),
            'atoms_schema': pilot.ReferenceAtoms.model_json_schema(), 'checks_schema': pilot.AnswerChecks.model_json_schema(),
            'baseline': str(args.baseline.resolve()) if args.baseline else None,
            'input_budget_comparison': 'unequal: direct full corpus versus bounded project retrieval'}
        if args.raw_data is not None or args.manifest is not None or args.conversation_index is not None:
            configuration['dataset'] = {'conversation_index': manifest['conversation_index'],
                                        'sample_id': manifest['sample_id'], 'manifest': manifest}
        previous = pilot.read(output / 'run-config.json')
        if previous is not None and previous != json.loads(json.dumps(configuration)):
            raise ValueError('compare_configuration_changed')
        pilot.save(output / 'run-config.json', configuration)
        query, models, owner = pilot.configure(output / 'project', args.models_root, generation, budget, 'rerank',
                                              gateway_factory=CompareGateway)
        directory = pilot.local_vectors.model_directory(args.models_root)
        original_encode = pilot.local_vectors.SentenceEncoder.encode
        original_scores = pilot.workspace_query.chunk_vector_scores

        def observed_encode(encoder, texts, input_type, *, policy):
            started = time.perf_counter()
            value = original_encode(encoder, texts, input_type, policy=policy)
            budget.encodes.append({'label': budget.label, 'role': input_type, 'count': len(texts),
                                   'seconds': time.perf_counter() - started})
            pilot.save(output / 'encodes.json', budget.encodes)
            return value

        def observed_scores(*values, **options):
            value = original_scores(*values, **options)
            budget.vector_scores.append({'label': budget.label, 'scores': value})
            pilot.save(output / 'vector-scores.json', budget.vector_scores)
            return value

        pilot.local_vectors.SentenceEncoder.encode = observed_encode
        pilot.workspace_query.chunk_vector_scores = observed_scores
        try:
            # 先全量输入真实预检，拒绝上下文过长而不截断语料，再占用本机向量内存。
            items = questions[:args.questions]
            direct_question(models, items[0], sources, output, budget)
            pilot.phase(output, 'loading_vector')
            started = time.perf_counter()
            pilot.local_vectors.load(directory, policy=pilot.vector_policy())
            while pilot.local_vectors.runtime_status(directory)['runtime_status'] == 'loading':
                time.sleep(1)
                if time.perf_counter() - started > 300:
                    raise TimeoutError('compare_vector_load_timeout')
            if not pilot.local_vectors.loaded(directory):
                raise RuntimeError('compare_vector_load_failed')
            pilot.save(output / 'load.json', {'seconds': time.perf_counter() - started,
                                             'state': pilot.local_vectors.runtime_status(directory)})
            pilot.seed_sources(query, sessions)
            budget.label = 'index:project'
            started = time.perf_counter()
            computed = pilot.EmbeddingIndex(query).run_scoped()
            verification = pilot.verify_index(query)
            pilot.save(output / 'index-project.json', {'computed': computed, 'verification': verification,
                                                      'seconds': time.perf_counter() - started})
            if verification['missing'] or verification['oversized'] or owner.local()['index_reason_code']:
                raise RuntimeError('compare_index_incomplete')
            with ExitStack() as stack:
                endpoint = stack.enter_context(pilot.RerankEndpoint(models, budget, output, selections))
                old = models.records.read('recognition_model_config', 'rerank')
                models.update('rerank', {'provider': 'openai', 'base_url': f'http://127.0.0.1:{endpoint.server.server_port}',
                    'model': 'deepseek-semantic-pilot', 'api_key': 'pilot-loopback', 'enabled': True,
                    'allow_remote': True, 'expected_revision': old.revision if old else 0})
                # 全部答案先冻结，再向评分器展示标准答案，评分永不回流到回答。
                for item in items:
                    pilot.phase(output, 'comparing', qa_index=item['qa_index'])
                    direct_question(models, item, sources, output, budget)
                    project_question(query, item, output, budget)
                for item in items:
                    rows = {mode: pilot.read(output / f"{mode}-{item['qa_index']}.json")
                            or pilot.read(output / f"attempt-{mode}-{item['qa_index']}.json") for mode in MODES}
                    original = pilot.judge(models, item, rows, output, budget, args.seed, modes=MODES)
                    pilot.validate_judge(models, item, rows, original, output, budget, args.seed, modes=MODES)
                    summarize(output, manifest, budget)
                pilot.phase(output, 'completed', summary=summarize(output, manifest, budget))
        except BaseException as error:
            pilot.phase(output, 'failed', error_type=type(error).__name__)
            raise
        finally:
            pilot.local_vectors.unload(directory, wait=True)
            pilot.local_vectors.SentenceEncoder.encode = original_encode
            pilot.workspace_query.chunk_vector_scores = original_scores
            pilot.save(output / 'unloaded.json', pilot.local_vectors.runtime_status(directory))
            owner.close()
            models.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    for name in ('baseline', 'config-root', 'config-db', 'models-root'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--seed', type=int, default=20261010)
    parser.add_argument('--questions', type=int, default=12, choices=range(1, 13))
    parser.add_argument('--max-calls', type=int, default=100, choices=range(1, 101))
    parser.add_argument('--policy', action='append', default=[])
    parser.add_argument('--raw-data', type=Path, help='只读权威原始 JSON，不复制到运行目录')
    parser.add_argument('--conversation-index', type=int)
    parser.add_argument('--manifest', type=Path, help='读取已冻结题目；seed 必须与清单一致')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.raw_data is None and args.baseline is None:
        parser.error('--baseline or --raw-data is required')
    if not args.prepare_only and any(getattr(args, name) is None for name in ('config_root', 'config_db', 'models_root')):
        parser.error('--config-root, --config-db and --models-root are required for model execution')
    run(args)


if __name__ == '__main__':
    main()
