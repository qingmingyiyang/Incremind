"""审阅草稿的 Agent Skills 格式校验与内存打包，不安装任何客户端。"""
from copy import deepcopy
import io
import re
import zipfile

import yaml
from core.external_extension_runtime.secure_archive import _safe_member_path


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and '\x00' not in value


def validate_document(document, *, source_count):
    required = {'name', 'description', 'trigger', 'steps', 'validation'}
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError('invalid_skill_document')
    name = document['name']
    if (not isinstance(name, str) or len(name) > 64
            or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
            or not _text(document['description'], 1024) or not _text(document['trigger'], 8192)):
        raise ValueError('invalid_skill_document')
    try:
        if _safe_member_path(name, directory=False) != name:
            raise ValueError('invalid_skill_document')
    except ValueError:
        raise ValueError('invalid_skill_document') from None
    steps, checks = document['steps'], document['validation']
    if (not isinstance(steps, list) or not 1 <= len(steps) <= 100
            or not isinstance(checks, list) or not 1 <= len(checks) <= 100
            or not all(_text(check, 8192) for check in checks)):
        raise ValueError('invalid_skill_document')
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'text', 'sources'} or not _text(step['text'], 8192):
            raise ValueError('invalid_skill_document')
        refs = step['sources']
        if (not isinstance(refs, list) or not refs
                or any(type(ref) is not int or not 1 <= ref <= source_count for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError('invalid_skill_document')
    return deepcopy(document)


def package_files(document, *, sources, version):
    document = validate_document(document, source_count=len(sources))
    header = yaml.safe_dump({'name': document['name'], 'description': document['description'],
        'metadata': {'version': str(version)}}, allow_unicode=True, sort_keys=False,
        default_style='"').rstrip()
    lines = ['---', header, '---', '', '# ' + document['name'], '', '## 触发边界',
        document['trigger'], '', '## 执行步骤']
    for index, step in enumerate(document['steps'], 1):
        marks = ' '.join('[' + str(ref) + ']' for ref in step['sources'])
        lines.append(str(index) + '. ' + step['text'] + ' ' + marks)
    lines.extend(['', '## 验证规则', *('- ' + check for check in document['validation']),
        '', '## 版本', str(version), '', '来源编号见 [方法认识](references/methods.md)。', ''])
    evidence = ['# 方法认识', '']
    for source in sources:
        evidence.extend(['## [' + str(source['number']) + ']',
            '认识：' + source['id'], '修订：' + str(source['revision']),
            '适用条件：' + '；'.join(source['conditions']), source['text'], ''])
    prefix = document['name'] + '/'
    return {prefix + 'SKILL.md': '\n'.join(lines).encode('utf-8'),
        prefix + 'references/methods.md': '\n'.join(evidence).encode('utf-8')}


def zip_package(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return output.getvalue()
