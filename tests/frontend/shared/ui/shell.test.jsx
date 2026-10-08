import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Shell } from '@src/shared/ui/Shell';
import { ProjectSwitcher } from '@src/shared/ui/ProjectSwitcher';
import { Tray } from '@src/shared/ui/Tray';
import { expectNoSevereA11yViolations } from '../../a11y/axeTestUtils';
const projects=[{id:'one',name:'创作助手'},{id:'two',name:'我'}];
describe('Shell',()=>{
 it('uses the historical solid four-point workbench star',()=>{render(<Shell project={projects[0]}/>);const glyph=screen.getByRole('button',{name:'工作台'}).querySelector('[data-icon=workspace]');expect(glyph).toHaveTextContent('✦');expect(glyph).toHaveAttribute('aria-hidden','true');expect(glyph.style.color).toBe('inherit');expect(glyph.style.fontSize).toBe('20px');});
 it('renders three navigation entries and the avatar',()=>{render(<Shell project={projects[0]} projects={projects}><h1>工作台</h1></Shell>);expect(screen.getByRole('navigation',{name:'主导航'}).querySelectorAll('button')).toHaveLength(3);expect(screen.getByRole('button',{name:'伙伴'}).querySelector('img').src).toContain('bear-head-ready.webp');});
 it('delegates navigation and changes theme',()=>{const navigate=vi.fn();render(<Shell project={projects[0]} onNavigate={navigate}/>);fireEvent.click(screen.getByRole('button',{name:'资料库'}));expect(navigate).toHaveBeenCalledWith('library');fireEvent.click(screen.getByRole('button',{name:'切换明暗'}));expect(document.documentElement.dataset.theme).toMatch(/light|dark/);});
 it('has no serious accessibility violations',async()=>{const {container}=render(<Shell project={projects[0]}><h1>工作台</h1></Shell>);await expectNoSevereA11yViolations(container);});
});
describe('ProjectSwitcher',()=>{
 it('renders current project',()=>{render(<ProjectSwitcher projects={projects} value="one"/>);expect(screen.getByRole('button',{name:'切换项目'})).toHaveTextContent('创作助手');});
 it('selects and closes on Escape',()=>{const change=vi.fn();render(<ProjectSwitcher projects={projects} value="one" onChange={change}/>);fireEvent.click(screen.getByRole('button',{name:'切换项目'}));fireEvent.click(screen.getByRole('option',{name:'我'}));expect(change).toHaveBeenCalledWith('two');fireEvent.click(screen.getByRole('button',{name:'切换项目'}));fireEvent.keyDown(screen.getByRole('listbox'),{key:'Escape'});expect(screen.queryByRole('listbox')).not.toBeInTheDocument();});
 it('has no serious accessibility violations expanded',async()=>{const {container}=render(<ProjectSwitcher projects={projects} value="one"/>);fireEvent.click(screen.getByRole('button',{name:'切换项目'}));await expectNoSevereA11yViolations(container);});
});
describe('Tray',()=>{
 const jobs=[{id:'a',title:'录音',state:'processing',progress:62},{id:'b',title:'资料',state:'failed'}];
 it('renders counts collapsed',()=>{render(<Tray jobs={jobs}/>);expect(screen.getByRole('button',{name:'进度'})).toHaveTextContent('1');expect(screen.queryByRole('region',{name:'进度列表'})).not.toBeInTheDocument();});
 it('renders canonical progress and pending count',()=>{render(<Tray jobs={[{id:'p',title:'原件',state:'processing',progress:{done:2,total:4}},{id:'n',title:'认识',state:'pending',pending_count:2}]}/>);fireEvent.click(screen.getByRole('button',{name:'进度'}));expect(screen.getByText('50%')).toBeInTheDocument();expect(screen.getByRole('region',{name:'进度列表'})).toHaveTextContent('认识2');});
 it('opens jobs and retries failures',()=>{const open=vi.fn(),retry=vi.fn();render(<Tray jobs={jobs} onOpen={open} onRetry={retry}/>);fireEvent.click(screen.getByRole('button',{name:'进度'}));fireEvent.click(screen.getByRole('button',{name:'录音'}));expect(open).toHaveBeenCalledWith(jobs[0]);fireEvent.click(screen.getByRole('button',{name:'进度'}));fireEvent.click(screen.getByRole('button',{name:'重试'}));expect(retry).toHaveBeenCalledWith(jobs[1]);});
 it('closes the tray after opening a job so its receipt is visible',()=>{const open=vi.fn();render(<Tray jobs={jobs} onOpen={open}/>);fireEvent.click(screen.getByRole('button',{name:'进度'}));fireEvent.click(screen.getByRole('button',{name:'录音'}));expect(open).toHaveBeenCalledWith(jobs[0]);expect(screen.getByRole('button',{name:'进度'})).toHaveAttribute('aria-expanded','false');expect(screen.queryByRole('region',{name:'进度列表'})).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'进度'}));expect(screen.getByRole('region',{name:'进度列表'})).toBeInTheDocument();});
 it('has no serious accessibility violations expanded',async()=>{const {container}=render(<Tray jobs={jobs}/>);fireEvent.click(screen.getByRole('button',{name:'进度'}));await expectNoSevereA11yViolations(container);});
});
