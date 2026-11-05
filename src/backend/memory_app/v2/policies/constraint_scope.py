from collections.abc import Sequence
import unicodedata


class ConstraintScopePolicy:
    version = 'constraint_scope@1'
    max_keywords = 32
    max_keyword_chars = 128
    instruction_prefix = '本次项目约束（用户明确配置的要求，不作为资料事实）：\n'

    def normalize(self, *, mode, scene=None, keywords=()):
        if not isinstance(mode, str) or mode not in {'always', 'scene', 'mention'}:
            raise ValueError('constraint_mode_invalid')
        if not isinstance(keywords, Sequence) or isinstance(keywords, (str, bytes)):
            raise ValueError('constraint_keywords_invalid')
        if len(keywords) > self.max_keywords:
            raise ValueError('constraint_keywords_too_many')
        if scene is not None and (not isinstance(scene, str) or not scene.strip()):
            raise ValueError('constraint_scene_invalid')
        selected = []
        seen = set()
        for keyword in keywords:
            if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > self.max_keyword_chars:
                raise ValueError('constraint_keyword_invalid')
            text = unicodedata.normalize('NFC', keyword.strip())
            key = text.casefold()
            if key in seen:
                raise ValueError('constraint_keyword_duplicate')
            seen.add(key)
            selected.append(text)
        if ((mode == 'scene' and (scene is None or selected))
                or (mode == 'mention' and (not selected or scene is not None))
                or (mode == 'always' and (scene is not None or selected))):
            raise ValueError('constraint_mode_fields_invalid')
        return {'mode': mode, 'scene': scene.strip() if scene is not None else None, 'keywords': selected}

    def matches(self, setting, *, scene, text, available_scenes):
        setting = self.normalize(**setting)
        if not isinstance(text, str):
            raise ValueError('constraint_input_invalid')
        if setting['mode'] == 'always':
            return True
        if setting['mode'] == 'scene':
            return setting['scene'] in available_scenes and setting['scene'] == scene
        folded = unicodedata.normalize('NFC', text).casefold()
        return any(word.casefold() in folded for word in setting['keywords'])

    def prompt(self, selected):
        return self.instruction_prefix + '\n'.join(item['content'] for item in selected) if selected else ''


def v1():
    return ConstraintScopePolicy()
