import { useContext, useEffect, useState } from 'react';
import { UserSpaceHeader } from './UserSpaceHeader';
import { Icon } from './Icon';
import { StatusDot } from './StatusDot';
import { ProjectSwitcher } from './ProjectSwitcher';
import { applyTheme } from '../../features/rebuild/themeBootstrap';
import './shell.css';
const navigation = [['workbench', '工作台', 'workspace'], ['library', '资料库', 'library'], ['settings', '设置', 'settings']];
export function Shell({ active = 'workbench', project, projectScopeKind = 'personal', projects = [], onProjectChange, blockedViews = {}, bearState = 'ready', reminderNotice = false, children, tray, onNavigate, onCompanion, headerStart, date = new Date() }) {
  const userSpaceHeader = useContext(UserSpaceHeader);
  const start = headerStart === undefined ? userSpaceHeader : headerStart;
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || 'light');
  const [online, setOnline] = useState(() => navigator.onLine !== false);
  useEffect(() => {
    const connected = () => setOnline(true), disconnected = () => setOnline(false);
    window.addEventListener('online', connected); window.addEventListener('offline', disconnected);
    return () => { window.removeEventListener('online', connected); window.removeEventListener('offline', disconnected); };
  }, []);
  const state = ['ready', 'working', 'attention'].includes(bearState) ? bearState : 'ready';
  const list = projects.length ? projects : project ? [typeof project === 'string' ? { id: project, name: project } : project] : [];
  const value = typeof project === 'object' ? project?.id : project;
  const day = `${date.getMonth() + 1}·${date.getDate()} 周${'日一二三四五六'[date.getDay()]}`;
  return <div className="ui-shell">
    <button type="button" className="ui-shell-avatar" aria-label={reminderNotice ? '伙伴 · 有到期提醒' : '伙伴'} aria-current={active === 'companion' ? 'page' : undefined} title={reminderNotice ? '有到期提醒' : '伙伴'} onClick={() => onCompanion ? onCompanion() : onNavigate?.('companion')}><span className="ui-shell-avatar-face"><img src={`${import.meta.env.BASE_URL}mascots/bear-head-${state}.webp`} alt=""/>{reminderNotice && <StatusDot state="pending" className="ui-shell-avatar-notice" aria-hidden="true"/>}</span></button>
    <nav className="ui-shell-navigation" aria-label="主导航">{navigation.map(([key,label,icon]) => <button type="button" key={key} aria-label={label} disabled={Boolean(blockedViews[key])} title={blockedViews[key]} aria-current={active === key ? 'page' : undefined} onClick={() => onNavigate?.(key)}><Icon name={icon}/><span>{label}</span></button>)}</nav>
    <button type="button" className="ui-shell-theme" aria-label="切换明暗" title="明暗" onClick={() => { const next = (document.documentElement.dataset.theme || theme) === 'dark' ? 'light' : 'dark'; applyTheme(next); setTheme(next); }}><Icon name="moon"/></button>
    <header className="ui-shell-header">{start && <div className="ui-shell-header-start">{start}</div>}<ProjectSwitcher projects={list} value={value} scopeKind={projectScopeKind} onChange={onProjectChange}/><div className="ui-shell-header-status"><time className="ui-shell-date" dateTime={`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`}><Icon name="calendar" size={15}/><span>{day}</span></time><StatusDot state={online ? 'online' : 'offline'}/></div></header>
    <main className="ui-shell-main">{children}</main>
    <div className="ui-shell-tray">{tray}</div>
  </div>;
}
export default Shell;
