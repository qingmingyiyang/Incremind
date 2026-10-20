import { useId, useLayoutEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import './shell.css';
export function ProjectSwitcher({ projects = [], value, onChange }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const trigger = useRef(null);
  const menu = useRef(null);
  const current = projects.find(project => project.id === value);
  const name = current?.name || value || '收件箱';
  useLayoutEffect(() => {
    if (!open) return;
    const button = trigger.current, panel = menu.current;
    const header = button.closest('.ui-shell-header');
    const space = header?.querySelector('.ui-shell-space-context');
    const navigation = header?.closest('.ui-shell').querySelector('.ui-shell-navigation');
    const viewport = window.visualViewport;
    const clippingAncestors = [];
    let clippedX = false, clippedY = false;
    for (let element = button.parentElement.parentElement; element; element = element.parentElement) {
      const style = getComputedStyle(element);
      const clipX = !clippedX && style.overflowX !== 'visible', clipY = !clippedY && style.overflowY !== 'visible';
      if (clipX || clipY) {
        clippingAncestors.push({ element, clipX, clipY });
        clippedX = clippedX || clipX;
        clippedY = clippedY || clipY;
      }
      if (clippedX && clippedY) break;
    }
    const position = () => {
      const anchor = button.parentElement.getBoundingClientRect();
      const mobile = window.matchMedia('(max-width:759px)').matches;
      let leftEdge = (viewport?.offsetLeft || 0) + 12;
      let rightEdge = (viewport?.offsetLeft || 0) + (viewport?.width || window.innerWidth) - 12;
      let topEdge = (viewport?.offsetTop || 0) + 12;
      const visibleBottom = (viewport?.offsetTop || 0) + (viewport?.height || window.innerHeight);
      let bottom = Math.min(visibleBottom, mobile && navigation ? navigation.getBoundingClientRect().top : visibleBottom) - 12;
      // 滚动容器会裁切菜单，边界使用其排除边框与滚动条后的实际内框。
      for (const { element, clipX, clipY } of clippingAncestors) {
        const rect = element.getBoundingClientRect();
        const left = rect.left + element.clientLeft, top = rect.top + element.clientTop;
        if (clipX) {
          leftEdge = Math.max(leftEdge, left + 12);
          rightEdge = Math.min(rightEdge, left + element.clientWidth - 12);
        }
        if (clipY) {
          topEdge = Math.max(topEdge, top + 12);
          bottom = Math.min(bottom, top + element.clientHeight - 12);
        }
      }
      const top = Math.max(topEdge, anchor.bottom, header?.getBoundingClientRect().bottom || 0, space?.getBoundingClientRect().bottom || 0);
      panel.style.maxWidth = `${Math.max(0, rightEdge - leftEdge)}px`;
      panel.style.minWidth = `${Math.min(180, Math.max(0, rightEdge - leftEdge))}px`;
      const width = panel.getBoundingClientRect().width;
      const preferred = mobile ? anchor.left + (anchor.width - width) / 2 : anchor.left;
      panel.style.left = `${Math.max(leftEdge, Math.min(preferred, rightEdge - width)) - anchor.left}px`;
      panel.style.top = `${top - anchor.top}px`;
      panel.style.maxHeight = `${Math.max(0, bottom - top)}px`;
    };
    const resize = () => {
      position();
      // 尺寸变化时保持焦点可见，鼠标滚动仍可独立浏览其他项目。
      if (panel.contains(document.activeElement)) document.activeElement.scrollIntoView({ block: 'nearest' });
    };
    position();
    const selected = panel.querySelector('[aria-selected=true]') || panel.querySelector('[role=option]');
    selected?.focus({ preventScroll: true });
    selected?.scrollIntoView({ block: 'nearest' });
    const observer = new ResizeObserver(resize);
    observer.observe(button);
    if (header) observer.observe(header);
    if (space) observer.observe(space);
    clippingAncestors.forEach(({ element }) => observer.observe(element));
    window.addEventListener('resize', resize);
    window.addEventListener('scroll', position, true);
    viewport?.addEventListener('resize', resize);
    viewport?.addEventListener('scroll', position);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', resize);
      window.removeEventListener('scroll', position, true);
      viewport?.removeEventListener('resize', resize);
      viewport?.removeEventListener('scroll', position);
    };
  }, [open]);
  function close() { setOpen(false); trigger.current?.focus(); }
  return <div className="ui-project-switcher" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <button type="button" ref={trigger} title={name} aria-label="切换项目" aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(!open)} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); } }}><span className="ui-project-dot"/><span className="ui-project-name">{name}</span><Icon name="chevron-down"/></button>
    {open && <div ref={menu} role="listbox" aria-label="项目" id={id} className="ui-project-options" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); close(); } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); const options = [...event.currentTarget.querySelectorAll('[role=option]')]; const index = options.indexOf(document.activeElement); options[(index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length]?.focus(); } }}>
      {projects.map(project => <button type="button" role="option" aria-selected={project.id === value} key={project.id} onClick={() => { onChange?.(project.id); close(); }}>{project.name}</button>)}
    </div>}
  </div>;
}
export default ProjectSwitcher;
