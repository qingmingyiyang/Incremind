"""明确词表指令的授权规则；不选择工具，不推断用户未给出的词。"""
from . import register


class AgentDataPolicy:
    def __call__(self, user_text, text, format):
        return self.authorized_import(user_text, text, format)

    def authorized_import(self, user_text, text, format):
        if not isinstance(user_text, str) or not isinstance(text, str):
            return False
        return self.parse_instruction(user_text) == {'text': text, 'format': format}

    def parse_instruction(self, user_text):
        if not isinstance(user_text, str):
            return None
        header, separator, body = user_text.strip().partition('\n')
        if not separator:
            return None
        header = header.strip()
        if not header.endswith(('：', ':')):
            return None
        header = header.removesuffix('：').removesuffix(':').strip()
        if header not in {'添加热词', '更新热词表', '导入热词表'}:
            return None
        body = body.strip()
        # 文件内容、引文、额外说明不在这份初版明确授权内。
        if body.startswith('```'):
            lines = body.splitlines()
            if lines[-1] != '```' or lines[0] not in {'```txt', '```csv', '```json'}:
                return None
            text = '\n'.join(lines[1:-1])
            return {'format': lines[0][3:], 'text': text} if text else None
        return {'format': 'txt', 'text': body} if body else None

    def catalog(self, description):
        return ('内置方法 asr-lexicon：' + description + '\n'
            '需要操作热词表时，先调用 asr.lexicon.read {"view":"skill"} 读取完整方法。'
            'asr.lexicon.read 参数为 {"view":"terms"}，返回词表及revision；'
            'asr.lexicon.preview 参数为 {"text":"词表原文","format":"txt|csv|json",'
            '"expected_revision":非负整数}，返回preview_id、计数和authorized；'
            'asr.lexicon.import 只接受 {"preview_id":"已返回的ID"}。'
            'authorized为false时只能展示预览并结束，说明需要完整明确词表指令；'
            '不得调用import、伪造确认、直接操作数据库或通过其他工具修改词表。')

    def model_events(self, events):
        """模型只读取任务事实，原内核事件和机器执行证据保持完整。"""
        if not any(event.get('type') in {'tool.started', 'tool.completed', 'tool.failed'}
                   and str(event.get('data', {}).get('capability_id', '')).startswith('asr.lexicon.')
                   for event in events):
            return events
        contexts = [event for event in events if event.get('type') == 'context.resolved']
        latest_context = contexts[-1] if contexts else None
        return [event for event in events if event is latest_context or event.get('type') in {
            'tool.completed', 'tool.failed', 'tool.cancelled', 'approval.required',
            'approval.resolved', 'hook.continuation'}]


v1 = AgentDataPolicy()
register('agent_data', '@1')(v1)
