"""只复核已冻结的双组答案；承接原调用预算，不运行回答或向量。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import rag_pilot as pilot
from tools.rag_compare import CompareGateway, MODES
from tools.rag_judge_validation import schema_instruction


def freeze_inputs(source, output, formal):
    # 双向祖先检查必须早于创建目录，保护旧结果与正式资料。
    for protected in (source, formal):
        if protected == output or protected in output.parents or output in protected.parents:
            raise ValueError('compare_validation_output_overlaps_input')
    manifest = pilot.read(source / 'manifest.json')
    if not manifest or len(manifest['questions']) != 12:
        raise ValueError('compare_validation_requires_frozen_twelve_questions')
    names = ['manifest.json', 'run-config.json', 'wires.json']
    for item in manifest['questions']:
        identity = item['qa_index']
        names.extend([f'{mode}-{identity}.json' for mode in MODES])
        names.append(f'judge-{identity}.json')
    if any(not (source / name).is_file() for name in names):
        raise ValueError('compare_validation_frozen_input_missing')
    # 数据结构校验也先于模型配置和任何外发。
    for item in manifest['questions']:
        identity = item['qa_index']
        grades = pilot.read(source / f'judge-{identity}.json')['grades']
        if set(grades) != set(MODES) or any(type(grade.get(key)) is not bool
                for grade in grades.values() for key in ('correct', 'citation_supported')):
            raise ValueError('compare_validation_original_judge_invalid')
        for mode in MODES:
            row = pilot.read(source / f'{mode}-{identity}.json')
            if (row.get('mode') != mode or row.get('qa_index') != identity
                    or not row.get('result', {}).get('model_used') or not row['result'].get('answer')):
                raise ValueError('compare_validation_original_answer_invalid')
    frozen = output / 'frozen-inputs'
    frozen.mkdir(parents=True, exist_ok=True)
    for name in names:
        before, after = source / name, frozen / name
        if after.exists() and after.read_bytes() != before.read_bytes():
            raise ValueError('compare_validation_frozen_input_changed:' + name)
        if not after.exists():
            shutil.copyfile(before, after)
    return manifest, frozen


class ValidationBudget(pilot.Budget):
    def __init__(self, output, limit, prior_count, prior_run):
        if type(prior_count) is not int or prior_count < 0 or prior_count >= limit:
            raise ValueError('compare_validation_prior_budget_invalid')
        super().__init__(output, limit - prior_count)
        self.prior_count, self.prior_run = prior_count, str(prior_run)

    def complete(self, **request):
        if len(self.rows) >= self.limit:
            raise RuntimeError('compare_validation_global_budget_exhausted')
        number = self.prior_count + len(self.rows) + 1
        pilot.save(self.output / f'prompt-global-{number}.json', {'messages': request.get('messages', []),
            'label': self.label, 'global_number': number, 'prior_run': self.prior_run})
        before = len(self.rows)
        try:
            return super().complete(**request)
        finally:
            for row in self.rows[before:]:
                row.update(global_number=self.prior_count + row['number'], prior_run=self.prior_run)
            pilot.save(self.output / 'wires.json', self.rows)


def summary(output, manifest, budget):
    rows = {mode: [pilot.read(output / 'frozen-inputs' / f"{mode}-{item['qa_index']}.json")
                   for item in manifest['questions']] for mode in MODES}
    value = {'contract': pilot.JUDGE_VALIDATION_CONTRACT, 'prior_run': budget.prior_run,
        'prior_calls': budget.prior_count, 'new_calls': len(budget.rows),
        'global_calls': budget.prior_count + len(budget.rows),
        'network_succeeded': sum(row['status'] == 'succeeded' for row in budget.rows),
        'token_usage': {key: sum(row.get('usage', {}).get(key, 0) or 0 for row in budget.rows)
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                        'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')},
        'modes': {mode: pilot.validation_summary(output, rows[mode], mode) for mode in MODES},
        'boundary': 'New validation only, original answers/judges/invalid records unchanged. Same-model checks and atom decomposition still require independent human review.'}
    pilot.save(output / 'summary.json', value)
    return value


def run(args):
    source, output, formal = args.source.resolve(), args.output.resolve(), args.config_root.resolve()
    manifest, frozen = freeze_inputs(source, output, formal)
    original_configuration = pilot.read(frozen / 'run-config.json')
    if (original_configuration.get('contract') != 'full-corpus-direct-vs-project@1'
            or original_configuration.get('seed') != args.seed):
        raise ValueError('compare_validation_original_configuration_invalid')
    prior_wires = pilot.read(frozen / 'wires.json')
    if not isinstance(prior_wires, list) or any(row.get('number') != index
            for index, row in enumerate(prior_wires, 1)):
        raise ValueError('compare_validation_prior_ledger_invalid')
    budget = ValidationBudget(output, args.max_calls, len(prior_wires), source)
    generation = pilot.read_generation(formal, args.config_db)
    if {key: generation[key] for key in ('provider', 'base_url', 'model')} != original_configuration['generation']:
        raise ValueError('compare_validation_generation_changed')
    if (generation['model'] != 'deepseek-flash' or not generation['enabled'] or not generation['allow_remote']
            or urlsplit(generation['base_url']).hostname != 'api.deepseek.com'
            or urlsplit(generation['base_url']).scheme != 'https'):
        raise ValueError('compare_validation_requires_verified_deepseek_flash')
    configuration = {'contract': pilot.JUDGE_VALIDATION_CONTRACT, 'modes': list(MODES),
        'source': str(source), 'prior_calls': len(prior_wires), 'max_global_calls': args.max_calls,
        'sourcecode_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        'sourcecode_dirty': bool(subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()),
        'generation': {key: generation[key] for key in ('provider', 'base_url', 'model')},
        'atoms_instruction': schema_instruction(pilot.DECOMPOSE_INSTRUCTION, pilot.ReferenceAtoms),
        'checks_instruction': schema_instruction(pilot.CHECK_INSTRUCTION, pilot.AnswerChecks),
        'audit_instruction': schema_instruction(pilot.AUDIT_INSTRUCTION, pilot.AnswerAudits),
        'audit_schema': pilot.AnswerAudits.model_json_schema(),
        'atoms_schema': pilot.ReferenceAtoms.model_json_schema(), 'checks_schema': pilot.AnswerChecks.model_json_schema(),
        'seed': args.seed, 'temperature': 0, 'max_tokens': {'atoms': 1800, 'checks': 5000, 'audit': 6500},
        'timeout_seconds': 60, 'wire_retries': 0, 'wire_limit_per_call': 1,
        'actual_wire_window': 1_048_576, 'context_source': 'https://api-docs.deepseek.com/quick_start/pricing/',
        'context_verified_date': '2026-10-10'}
    previous = pilot.read(output / 'run-config.json')
    if previous is not None and previous != configuration:
        raise ValueError('compare_validation_configuration_changed')
    pilot.save(output / 'run-config.json', configuration)
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_LOG='ERROR')
    (output / 'model-config').mkdir(exist_ok=True)
    records = pilot.SQLiteStructuredRecordStore(output / 'model-config' / 'records.sqlite3')
    models = pilot.PilotModels(records, output / 'model-config', pilot.InMemorySecretStore(),
        gateway_factory=CompareGateway, completion_fn=budget.complete, local_models_root=args.models_root)
    try:
        old = records.read('recognition_model_config', 'generation')
        models.update('generation', {key: generation[key] for key in ('provider', 'base_url', 'model', 'api_key')}
            | {'enabled': True, 'allow_remote': True, 'expected_revision': old.revision if old else 0})
        for item in manifest['questions']:
            rows = {mode: pilot.read(frozen / f"{mode}-{item['qa_index']}.json") for mode in MODES}
            original = pilot.read(frozen / f"judge-{item['qa_index']}.json")
            pilot.phase(output, 'validating_frozen_answers', qa_index=item['qa_index'],
                        global_calls=budget.prior_count + len(budget.rows))
            pilot.validate_judge(models, item, rows, original, output, budget, args.seed, modes=MODES)
            summary(output, manifest, budget)
        pilot.phase(output, 'completed', summary=summary(output, manifest, budget))
    finally:
        models.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'config-root', 'config-db', 'models-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--max-calls', type=int, choices=range(1, 101), default=100)
    parser.add_argument('--seed', type=int, default=20261010)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
