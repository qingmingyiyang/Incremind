import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { TaskDivision } from '@src/features/workbench/TaskDivision';
afterEach(cleanup);
const items = [{goal:'检查证据', deliverable:'整理稿', capabilities:['memory.recall'], depends_on:[]}];
it('expands actual recall and tool states', () => {
 render(<TaskDivision project="alpha" turnId="turn-a" task={{state:'running',division:[{...items[0],state:'running',
  tools:[{id:'call',capability_id:'document.draft.propose',state:'done'}],
  recalled:[{title:'本项目资料',text:'核对过的内容'}]}]}}/>);
 fireEvent.click(screen.getByRole('button',{name:'检查证据'}));
 expect(screen.getByText('起草整理稿')).toBeInTheDocument();
 expect(screen.getByText('本项目资料')).toBeInTheDocument();
 expect(screen.getByText('核对过的内容')).toBeInTheDocument();
});
it('shows goals, states and individual usage without approval', () => {
 render(<TaskDivision project="alpha" turnId="turn-a" task={{state:'running',division:[{...items[0],state:'running',model_usage:{input_tokens:12,output_tokens:4}}]}}/>);
 expect(screen.getByText('检查证据')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'检查证据'}));
 expect(screen.getByText('12 / 4')).toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'批准'})).not.toBeInTheDocument();
});
it('saves adjusted work and reruns against the returned revision', async () => {
 const api = {division:vi.fn().mockResolvedValue({items,revision:1}),
  saveDivision:vi.fn().mockResolvedValue({items,revision:2,adjusted:true}),
  redo:vi.fn().mockResolvedValue({thread_id:'thread-a',turn:{id:'turn-b'}})};
 const onRedo=vi.fn();
 render(<TaskDivision api={api} project="alpha" turnId="turn-a" task={{state:'done',division:[]}} onRedo={onRedo}/>);
 fireEvent.click(screen.getByText('分工')); await act(async()=>{});
 fireEvent.change(await screen.findByRole('textbox',{name:'目标 1'}),{target:{value:'复核证据'}});
 fireEvent.click(screen.getByRole('button',{name:'保存'})); await act(async()=>{});
 expect(api.saveDivision).toHaveBeenCalledWith('alpha','turn-a',[{...items[0],goal:'复核证据'}],1);
 fireEvent.click(screen.getByRole('button',{name:'按此分工重做'})); await act(async()=>{});
 expect(api.redo).toHaveBeenCalledWith('alpha','turn-a',2);
 expect(onRedo).toHaveBeenCalled();
});

it('does not reload a deleted sample when reopened', async () => {
 const api={division:vi.fn().mockResolvedValue({items,revision:1}),deleteDivision:vi.fn().mockResolvedValue({deleted:true})};
 render(<TaskDivision api={api} project="alpha" turnId="turn-a" task={{state:'done'}}/>);
 fireEvent.click(screen.getByText('分工'));
 fireEvent.click(await screen.findByRole('button',{name:'删除分工样例'})); await act(async()=>{});
 const details=screen.getByText('分工').closest('details');
 details.open=false; fireEvent(details,new Event('toggle')); await act(async()=>{});
 details.open=true; fireEvent(details,new Event('toggle')); await act(async()=>{});
 expect(api.division).toHaveBeenCalledTimes(1);
 expect(screen.queryByRole('textbox',{name:'目标 1'})).not.toBeInTheDocument();
});

it('does not apply a late sample from the preceding project', async () => {
 let resolve; const api={division:vi.fn().mockImplementation(()=>new Promise(done=>{resolve=done;}))};
 const view=render(<TaskDivision api={api} project="alpha" turnId="turn-a" task={{state:'done'}}/>);
 fireEvent.click(screen.getByText('分工')); await act(async()=>{});
 await waitFor(()=>expect(api.division).toHaveBeenCalledTimes(1));
 view.rerender(<TaskDivision api={api} project="beta" turnId="turn-b" task={{state:'running'}}/>);
 await act(async()=>{resolve({items,revision:1});});
 expect(screen.queryByRole('textbox',{name:'目标 1'})).not.toBeInTheDocument();
});
