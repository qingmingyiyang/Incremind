import { useId, useRef, useState } from 'react';
import { Icon } from './Icon';
import './shell.css';
export function ProjectSwitcher({ projects = [], value, onChange }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const trigger = useRef(null);
  const current = projects.find(project => project.id === value);
  function close() { setOpen(false); trigger.current?.focus(); }
  return <div className="ui-project-switcher" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <button type="button" ref={trigger} aria-label="切换项目" aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(!open)} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); } }}><span className="ui-project-dot"/><span className="ui-project-name">{current?.name || value || '收件箱'}</span><Icon name="chevron-down"/></button>
    {open && <div role="listbox" aria-label="项目" id={id} className="ui-project-options" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); close(); } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); const options = [...event.currentTarget.querySelectorAll('[role=option]')]; const index = options.indexOf(document.activeElement); options[(index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length]?.focus(); } }}>
      {projects.map(project => <button type="button" role="option" aria-selected={project.id === value} key={project.id} onClick={() => { onChange?.(project.id); close(); }}>{project.name}</button>)}
    </div>}
  </div>;
}
export default ProjectSwitcher;
