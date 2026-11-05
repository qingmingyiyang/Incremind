import { useState } from 'react';
import { Icon } from '../../shared/ui';

export function SettingsQuestion({ projectId, scenes = [], label = '试问', model }) {
  const [text, setText] = useState(''), [scene, setScene] = useState('');
  return <section className="settings-question" aria-label={label}><h2 className="settings-caption">{label}{model && <span className="settings-question-model">{model}</span>}</h2><form className="settings-card" onSubmit={event => {
    event.preventDefault(); if (!text.trim()) return;
    const params = new URLSearchParams({ view: 'workbench', project_id: projectId, compose_text: text.trim() });
    if (scene) params.set('compose_scene', scene);
    window.location.hash = params.toString();
  }}><label><Icon name="pattern" size={16}/><input aria-label={label === '试一句' ? '试一句' : '试问一句'} placeholder="上次讨论的重点是什么？" value={text} onChange={event => setText(event.target.value)}/></label><div>{scenes.length > 0 && <select aria-label="试问场景" value={scene} onChange={event => setScene(event.target.value)}><option value="">全部场景</option>{scenes.map(value => <option key={value} value={value}>{value}</option>)}</select>}<button type="submit" title="在工作台预览问题，确认后发送" disabled={!text.trim()}>试问</button></div></form></section>;
}
