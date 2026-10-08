import { render,screen } from '@testing-library/react';
import { expect,it } from 'vitest';
import { Row } from '@src/shared/ui/Row';
it('renders a reading-only row without a misleading empty action',()=>{
  render(<Row readOnly title="已归档整理稿" trailing={<button>恢复</button>}/>);
  expect(screen.getByText('已归档整理稿')).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'已归档整理稿'})).not.toBeInTheDocument();
  expect(screen.getByRole('button',{name:'恢复'})).toBeInTheDocument();
});
