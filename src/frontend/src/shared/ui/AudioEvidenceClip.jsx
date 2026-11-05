import { useCallback, useEffect, useMemo, useState } from 'react';
import { deviceIdentity, isDeviceIdentity } from '../api/deviceTransport';
import { useRequestScope } from '../lib/useRequestScope';
import { libraryApi } from '../../features/library/libraryApi';
import { Icon } from './Icon';
import { StatusDot } from './StatusDot';
import './audioEvidenceClip.css';

function stopAudio(audio) {
  if (!audio) return;
  audio.pause(); audio.removeAttribute('src'); audio.load();
}

function releaseClip(session) {
  session.controller?.abort(); session.controller = null;
  stopAudio(session.audio);
  if (session.url) URL.revokeObjectURL(session.url);
  session.url = '';
}

function timestamp(seconds) {
  const whole = Math.floor(seconds);
  return `${String(Math.floor(whole / 60)).padStart(2, '0')}:${String(whole % 60).padStart(2, '0')}`;
}

export function AudioEvidenceClip({ projectId, sourceId, seconds, scopeKey }) {
  const start = Array.isArray(seconds) && seconds.length === 2 ? seconds[0] : null;
  const end = Array.isArray(seconds) && seconds.length === 2 ? seconds[1] : null;
  const valid = Boolean(projectId && sourceId) && Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start;
  const session = useMemo(() => ({ scopeKey, projectId, sourceId, start, end, controller: null, audio: null, url: '' }),
    [scopeKey, projectId, sourceId, start, end]);
  const gate = useRequestScope(session), scope = gate.scope;
  const [result, setResult] = useState({ session, phase: 'idle', url: '' });
  const state = result.session === session ? result : { phase: 'idle', url: '' };
  const bindAudio = useCallback(audio => {
    if (session.audio !== audio) stopAudio(session.audio);
    session.audio = audio;
  }, [session]);
  useEffect(() => {
    const changed = () => {
      releaseClip(session);
      if (gate.isScopeCurrent(scope)) {
        gate.invalidate('clip'); setResult({ session, phase: 'idle', url: '' });
      }
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
      releaseClip(session);
    };
  }, [gate, scope, session]);
  async function play() {
    if (!valid || state.phase === 'loading' || session.controller) return;
    const request = gate.issue('clip', scope);
    if (!request) return;
    const identity = deviceIdentity(), controller = new AbortController();
    releaseClip(session); session.controller = controller;
    setResult({ session, phase: 'loading', url: '' });
    try {
      const blob = await libraryApi.audioClip(projectId, sourceId, start, end, { signal: controller.signal });
      // 用户与窗口仍有效时才创建可播放地址，迟到响应直接丢弃。
      if (controller.signal.aborted || !gate.isCurrent(request) || !isDeviceIdentity(identity)) return;
      if (!blob.size) throw new Error('audio_clip_empty');
      const url = URL.createObjectURL(blob);
      if (controller.signal.aborted || !gate.isCurrent(request) || !isDeviceIdentity(identity)) { URL.revokeObjectURL(url); return; }
      session.url = url; setResult({ session, phase: 'ready', url });
    } catch {
      if (!controller.signal.aborted && gate.isCurrent(request) && isDeviceIdentity(identity))
        setResult({ session, phase: 'error', url: '' });
    } finally { if (session.controller === controller) session.controller = null; }
  }
  function failed(event) {
    if (!gate.isScopeCurrent(scope) || !session.url || event.currentTarget.getAttribute('src') !== session.url) return;
    releaseClip(session); setResult({ session, phase: 'error', url: '' });
  }
  if (!valid) return null;
  return <span className="ui-audio-evidence">
    <button type="button" disabled={state.phase === 'loading'} onClick={play}
      aria-label={state.phase === 'error' ? `重试播放原句 ${timestamp(start)}` : `播放原句 ${timestamp(start)}`} title="播放原句">
      {state.phase === 'loading' ? <StatusDot state="processing"/> : <Icon name="play" size={14}/>}
      <span className="ui-audio-evidence-time">{timestamp(start)}</span>{state.phase === 'error' && <span role="alert">重试</span>}
    </button>
    {state.url && <audio key={state.url} ref={bindAudio} src={state.url} controls autoPlay preload="metadata" aria-label="原句音频" onError={failed}/>}
  </span>;
}
