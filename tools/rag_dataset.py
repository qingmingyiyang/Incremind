from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import random
import re
from urllib.request import urlopen


DATA_URL = 'https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json'
LICENSE = 'CC BY-NC 4.0 (upstream LICENSE.txt)'
CATEGORIES = (1, 2, 3, 4)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def freeze_json(path, value):
    # 已有同名资料只核对，不覆盖原字节和时间。
    if path.exists():
        if read_json(path) != value:
            raise ValueError('pilot_manifest_changed')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))


def conversation_at(data, index):
    if type(index) is not int or not 0 <= index < len(data):
        raise ValueError('pilot_conversation_index_invalid')
    return data[index]


def make_manifest(conversation, index, seed):
    rng, chosen = random.Random(seed), []
    for category in CATEGORIES:
        choices = [position for position, qa in enumerate(conversation['qa']) if qa['category'] == category]
        if len(choices) < 3:
            raise ValueError('pilot_category_has_fewer_than_three_questions:' + str(category))
        chosen.extend(rng.sample(choices, 3))
    return {'source': DATA_URL, 'license': LICENSE, 'seed': seed, 'conversation_index': index,
            'sample_id': conversation['sample_id'],
            'questions': [{'qa_index': position, **conversation['qa'][position]} for position in sorted(chosen)]}


def validate_manifest(manifest, data):
    index = manifest['conversation_index']
    conversation = conversation_at(data, index)
    if (set(manifest) != {'source', 'license', 'seed', 'conversation_index', 'sample_id', 'questions'}
            or manifest['source'] != DATA_URL or manifest['license'] != LICENSE
            or type(manifest['seed']) is not int or manifest['sample_id'] != conversation['sample_id']):
        raise ValueError('pilot_frozen_manifest_identity_invalid')
    questions = manifest['questions']
    if not isinstance(questions, list) or not questions:
        raise ValueError('pilot_frozen_manifest_questions_invalid')
    indices = [item['qa_index'] for item in questions]
    if any(type(index) is not int or not 0 <= index < len(conversation['qa']) for index in indices):
        raise ValueError('pilot_frozen_manifest_qa_index_invalid')
    if indices != sorted(set(indices)):
        raise ValueError('pilot_frozen_manifest_qa_order_invalid')
    for item in questions:
        if item != {'qa_index': item['qa_index'], **conversation['qa'][item['qa_index']]}:
            raise ValueError('pilot_frozen_manifest_annotation_changed')
        if item.get('category') not in CATEGORIES or 'answer' not in item:
            raise ValueError('pilot_frozen_manifest_category_unsupported')
    return conversation


def session_inputs(conversation):
    # 生成与召回只消费原始会话正文，QA、答案、题型和上游摘要均不进入此投影。
    sessions = []
    for key, value in conversation['conversation'].items():
        if not re.fullmatch(r'session_\d+', key):
            continue
        date = conversation['conversation'].get(key + '_date_time', '')
        lines = [f'Session date: {date}']
        for turn in value:
            caption = turn.get('blip_caption', '')
            lines.append(f"[{turn['dia_id']}] {turn['speaker']}: {turn['text']}"
                         + (f' [Image caption: {caption}]' if caption else ''))
        sessions.append({'id': key.replace('_', '-'),
                         'title': f"Conversation {conversation['sample_id']} {key} {date}",
                         'content': '\n'.join(lines)})
    return sessions


def prepare_data(output, seed, *, raw_data=None, conversation_index=None, manifest_path=None):
    path = Path(raw_data) if raw_data is not None else output / 'locomo10.json'
    if raw_data is None and not path.exists():
        with urlopen(DATA_URL, timeout=60) as response:
            with path.open('xb') as stream:
                stream.write(response.read(8 * 1024 * 1024))
    data = read_json(path)
    if manifest_path is not None:
        manifest = read_json(Path(manifest_path))
        conversation = validate_manifest(manifest, data)
        if manifest['seed'] != seed or (conversation_index is not None and manifest['conversation_index'] != conversation_index):
            raise ValueError('pilot_frozen_manifest_selection_conflicted')
    else:
        conversation_index = 0 if conversation_index is None else conversation_index
        conversation = conversation_at(data, conversation_index)
        manifest = make_manifest(conversation, conversation_index, seed)
    freeze_json(output / 'manifest.json', manifest)
    return manifest, session_inputs(conversation)


def evidence_quality(conversation, questions):
    turns = [turn for key, values in conversation['conversation'].items()
             if re.fullmatch(r'session_\d+', key) for turn in values]
    identities = [turn['dia_id'] for turn in turns]
    if len(set(identities)) != len(identities):
        raise ValueError('pilot_duplicate_dialogue_id:' + conversation['sample_id'])
    available = set(identities)
    rows = []
    for qa in questions:
        refs = []
        for annotation in qa.get('evidence', []):
            atomic = bool(re.fullmatch(r'D\d+:\d+', annotation))
            compound = bool(re.fullmatch(r'D\d+:\d+(?:[;\s]+D\d+:\d+)+', annotation))
            parsed = re.findall(r'D\d+:\d+', annotation) if atomic or compound else []
            refs.append({'annotation': annotation, 'format': 'atomic' if atomic else 'compound' if compound else 'invalid',
                'resolved_ids': [identity for identity in parsed if identity in available],
                'missing_ids': [identity for identity in parsed if identity not in available]})
        rows.append({'qa_index': qa['qa_index'], 'category': qa['category'],
            'answer_present': 'answer' in qa, 'evidence_present': bool(refs), 'references': refs,
            'original_literal_recall_eligible': bool(refs) and all(ref['format'] == 'atomic'
                and not ref['missing_ids'] for ref in refs)})
    return {'sessions': len(session_inputs(conversation)), 'turns': len(turns),
        'input_chars': sum(len(session['content']) for session in session_inputs(conversation)),
        'questions': rows, 'missing_evidence_questions': sum(not row['evidence_present'] for row in rows),
        'non_atomic_references': sum(ref['format'] != 'atomic' for row in rows for ref in row['references']),
        'invalid_references': sum(ref['format'] == 'invalid' for row in rows for ref in row['references']),
        'missing_ids': sum(len(ref['missing_ids']) for row in rows for ref in row['references'])}


def evidence_metrics(selected, expected, annotation=None):
    if annotation is not None and not annotation['eligible']:
        return {'evidence_recall': None, 'all_evidence_found': None}
    expected, selected = set(expected), set(selected)
    return {'evidence_recall': len(selected & expected) / len(expected) if expected else None,
            'all_evidence_found': bool(expected) and expected <= selected}


def prepare_dataset(output, raw_data, development_manifest, license_path, *, seed=20261021, count=4, frozen_index=None):
    raw_data, development_manifest, license_path = (path.resolve() for path in
        (raw_data, development_manifest, license_path))
    data, development = read_json(raw_data), read_json(development_manifest)
    validate_manifest(development, data)
    if not license_path.is_file() or 'Attribution-NonCommercial 4.0 International' not in license_path.read_text(encoding='utf-8'):
        raise ValueError('pilot_upstream_license_required')
    candidates, eligible = [], []
    for position, conversation in enumerate(data):
        counts = Counter(qa['category'] for qa in conversation['qa'])
        excluded = conversation['sample_id'] == development['sample_id']
        qualifies = not excluded and all(counts[category] >= 3 for category in CATEGORIES)
        candidates.append({'conversation_index': position, 'sample_id': conversation['sample_id'],
            'category_counts': {str(key): value for key, value in sorted(counts.items())},
            'development_conversation': excluded, 'eligible': qualifies,
            'missing_answer_by_category': {str(key): value for key, value in sorted(
                Counter(qa['category'] for qa in conversation['qa'] if 'answer' not in qa).items())},
            'annotation_quality': evidence_quality(conversation,
                [{'qa_index': index, **qa} for index, qa in enumerate(conversation['qa'])])})
        if qualifies:
            eligible.append(position)
    if not 1 <= count <= len(eligible):
        raise ValueError('pilot_heldout_conversation_count_invalid')
    chosen = sorted(random.Random(seed).sample(eligible, count))
    outputs, entries, quality = {}, [], []
    development_questions = {qa['question'] for qa in development['questions']}
    overlap = []
    for position in chosen:
        conversation = data[position]
        manifest = make_manifest(conversation, position, seed)
        directory = f'conversation-{position}'
        outputs[directory + '/manifest.json'] = manifest
        outputs[directory + '/sessions.json'] = session_inputs(conversation)
        entries.append({'conversation_index': position, 'sample_id': conversation['sample_id'],
            'manifest': directory + '/manifest.json', 'sessions': directory + '/sessions.json',
            'qa_indices': [qa['qa_index'] for qa in manifest['questions']]})
        report = evidence_quality(conversation, manifest['questions'])
        quality.append({'sample_id': conversation['sample_id'], 'conversation_index': position,
                        'category_counts': {str(key): value for key, value in sorted(
                            Counter(qa['category'] for qa in manifest['questions']).items())}, **report})
        overlap.extend({'sample_id': conversation['sample_id'], 'qa_index': qa['qa_index']}
                       for qa in manifest['questions'] if qa['question'] in development_questions)
    index = {'contract': 'locomo-heldout@1', 'source': DATA_URL, 'license': LICENSE,
        'raw_data': str(raw_data), 'raw_bytes': raw_data.stat().st_size, 'license_path': str(license_path),
        'development_manifest': str(development_manifest), 'excluded_sample_ids': [development['sample_id']],
        'seed': seed, 'selection': 'seeded conversation sampling after category-count eligibility; three questions per category 1-4',
        'conversations': entries}
    if frozen_index is not None:
        if read_json(frozen_index) != index:
            raise ValueError('pilot_heldout_index_changed')
        # 逐值核对冻结题目和原话，复现不以文件大小代替内容一致性。
        for relative, value in outputs.items():
            if read_json(frozen_index.parent / relative) != value:
                raise ValueError('pilot_heldout_input_changed:' + relative)
    outputs['index.json'] = index
    outputs['quality.json'] = {'contract': index['contract'], 'candidates': candidates, 'selected': quality,
        'total_questions': 12 * count, 'category_counts': {str(category): 3 * count for category in CATEGORIES},
        'development_conversation_overlap': sorted(set(entry['sample_id'] for entry in entries) & {development['sample_id']}),
        'development_question_text_overlap': overlap,
        'annotation_boundary': 'Only categories 1-4 use the existing answer contract. Category 5 and missing annotations are not repaired or awarded credit. Compound IDs are resolved for audit only; original manifests and literal recall scoring remain unchanged.',
        'input_boundary': 'sessions.json derives only from conversation session turns in the unique raw_data file; QA, gold answers, categories, upstream summaries and observations stay outside retrieval and generation input.',
        'score_status': 'not_run_no_model_calls'}
    # 先核所有已有文件，冲突时不留下部分更新。
    for relative, value in outputs.items():
        target = output / relative
        if target.exists() and read_json(target) != value:
            raise ValueError('pilot_dataset_output_conflicted:' + str(target))
    for relative, value in outputs.items():
        freeze_json(output / relative, value)
    return index, outputs['quality.json']


def main():
    parser = argparse.ArgumentParser(description='Prepare a frozen LoCoMo holdout without model calls.')
    for name in ('output', 'raw-data', 'development-manifest', 'license'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20261021)
    parser.add_argument('--conversations', type=int, default=4)
    parser.add_argument('--index', type=Path, help='核对已冻结 index 后复现，不改写原目录')
    args = parser.parse_args()
    index, quality = prepare_dataset(args.output.resolve(), args.raw_data, args.development_manifest, args.license,
        seed=args.seed, count=args.conversations, frozen_index=args.index)
    print(json.dumps({'index': str(args.output / 'index.json'), 'conversations': len(index['conversations']),
                      'questions': quality['total_questions'], 'category_counts': quality['category_counts'],
                      'model_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
