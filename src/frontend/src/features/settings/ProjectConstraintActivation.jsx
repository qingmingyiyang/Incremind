import { useEffect, useId, useRef, useState } from 'react';
import { Icon } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { constraintActivationFailure, previewConstraintActivation } from './projectConstraintApi';
import './projectConstraintActivation.css';

export function ProjectConstraintActivation({ value, scenes, disabled = false, scopeKey, projectId, error = '', onChange,
  previewActivation = previewConstraintActivation }) {
  const group = useId();
  const [keyword, setKeyword] = useState(''), [failure, setFailure] = useState(''), [checking, setChecking] = useState(false);
  const identity = userStorageKey(projectId);
  const gate = useRequestScope(`${identity}:${scopeKey}`), scope = gate.scope;
  const latest = useRef({ value, keyword }), controller = useRef(null);
  latest.current = { value, keyword };
  useEffect(() => {
    setKeyword(''); setFailure(''); setChecking(false);
    return () => { gate.invalidate('activation'); controller.current?.abort(); };
  }, [scope]);
  function modeChanged(mode) {
    gate.invalidate('activation'); controller.current?.abort();
    setKeyword(''); setFailure(''); setChecking(false);
    onChange({ mode, scene: mode === 'scene' ? scenes[0] || null : null, keywords: [] });
  }
  async function addKeyword() {
    const next = keyword.trim().normalize('NFC');
    if (checking || !next || value.keywords.includes(next)) return;
    const token = gate.issue('activation', scope);
    if (!token) return;
    const submitted = keyword;
    controller.current = new AbortController();
    setChecking(true); setFailure('');
    const current = () => gate.isCurrent(token) && userStorageKey(projectId) === identity;
    try {
      const validated = await previewActivation({ projectId, activation: { ...value, keywords: [...value.keywords, next] }, signal: controller.current.signal });
      if (!current() || latest.current.value !== value) return;
      onChange(validated.activation);
      if (latest.current.keyword === submitted) setKeyword('');
    } catch (reason) {
      if (current() && latest.current.value === value && latest.current.keyword === submitted)
        setFailure(constraintActivationFailure(reason));
    } finally { if (current()) setChecking(false); }
  }
  return <fieldset className="settings-constraint-activation" disabled={disabled}>
    <legend>生效方式</legend>
    {(error || failure) && <p role="alert">{error || failure}</p>}
    <div className="settings-constraint-modes">{[['always', '总是'], ['scene', '场景'], ['mention', '点名']].map(([mode, label]) =>
      <label key={mode}><input type="radio" name={group} value={mode} checked={value.mode === mode}
        onChange={() => modeChanged(mode)}/>{label}</label>)}</div>
    {value.mode === 'scene' && <label>指定场景<select aria-label="约束生效场景" value={value.scene || ''}
      onChange={event => onChange({ ...value, scene: event.target.value || null })}>
      <option value="">选择场景</option>{value.scene && !scenes.includes(value.scene) && <option value={value.scene} disabled>已移除 · {value.scene}</option>}
      {scenes.map(scene => <option key={scene} value={scene}>{scene}</option>)}
    </select>{scenes.length === 0 && <span role="status">先添加场景</span>}</label>}
    {value.mode === 'mention' && <>
      <div className="settings-tags">{value.keywords.map(word => <button type="button" key={word} aria-label={`移除关键词${word}`}
        onClick={() => onChange({ ...value, keywords: value.keywords.filter(item => item !== word) })}>{word} <Icon name="close" size={12}/></button>)}</div>
      <div className="settings-constraint-keyword"><label>关键词<input aria-label="约束关键词" value={keyword}
        onChange={event => { setKeyword(event.target.value); setFailure(''); }} onKeyDown={event => {
          if (event.key === 'Enter') {
            event.preventDefault();
            if (!event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) addKeyword();
          }
        }}/></label><button type="button" disabled={checking || !keyword.trim() || value.keywords.includes(keyword.trim().normalize('NFC'))}
          onClick={addKeyword}>{checking ? '校验中' : '添加'}</button></div>
    </>}
  </fieldset>;
}
