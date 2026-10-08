import { render, screen, within, fireEvent } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Shell } from '@src/shared/ui/Shell';
import { MarkdownBody } from '@src/shared/ui/MarkdownBody';
import { CompanionPage } from '@src/features/companion/CompanionPage';
import SettingsPage from '@src/features/settings/SettingsPage';
it('gives navigation buttons explicit accessible names', () => {
 render(<Shell/>);
 for (const name of ['工作台','资料库','设置']) expect(screen.getByRole('button', {name,exact:true})).toHaveAttribute('aria-label',name);
});
it('renders original prose while omitting the observed empty note artifact', () => {
 const {container}=render(<MarkdownBody omitEmptyArtifacts>{'有意义的正文。\n\n：。。\n\n标点：。。仍是正文。'}</MarkdownBody>);
 expect(container.querySelectorAll('p')).toHaveLength(2);
 expect(screen.getByText('有意义的正文。')).toBeInTheDocument();
 expect(screen.getByText('标点：。。仍是正文。')).toBeInTheDocument();
});
it('keeps review counts once and explicitly names todo checkboxes', async () => {
 const api={getWeekStats:vi.fn().mockResolvedValue({remember:12,confirm:8,forget:1}),listTodos:vi.fn().mockResolvedValue({items:[{id:'td',text:'写提纲',done:false,project_name:'选题'}]})};
 render(<CompanionPage api={api} initialMode="review"/>);
 await screen.findByText('12');
 expect(screen.queryByText('本周记住 12，确认 8，遗忘 1。')).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'安排'}));
 expect(await screen.findByRole('checkbox',{name:'写提纲'})).toHaveAttribute('aria-label','写提纲');
});
it('formats receipt dates locally and keeps duration out of the token column', async () => {
 const at='2026-10-01T18:03:04.123456Z'; const date=new Date(at); const pad=n=>String(n).padStart(2,'0');
 const api={load:vi.fn().mockResolvedValue({privacy:{revision:1,private_projects:[]},model:{}}),projects:vi.fn().mockResolvedValue({items:[]}),receipts:vi.fn().mockResolvedValue([{at,purpose:'生成',model:'m',items:1,duration:12,usage:null},{at,purpose:'生成',model:'m',items:2,duration:8,usage:{input:0,output:20}}])};
 render(<SettingsPage api={api} initialSection="privacy"/>);fireEvent.click(await screen.findByRole('button',{name:'外发记录'}));
 const table=await screen.findByRole('table'); const rows=within(table).getAllByRole('row').slice(1);
 expect(within(rows[0]).getAllByRole('cell')[0]).toHaveTextContent(`${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`);
 expect(within(rows[0]).getAllByRole('cell')[4]).toHaveTextContent('—');
 expect(within(rows[1]).getAllByRole('cell')[4]).toHaveTextContent('0 / 20');
 expect(table).not.toHaveTextContent(at);expect(table).not.toHaveTextContent('12s');
});
